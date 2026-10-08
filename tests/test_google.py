import json
from dataclasses import replace

import httpx
import pytest

from src.vivi.agents.classifier import GoogleIntentClassifier
from src.vivi.agents.graph import build_graph
from src.vivi.config import Settings
from src.vivi.domain.models import ConfirmationDecisionRequest, TurnRequest
from src.vivi.providers import configured_llm_providers, llm_models
from src.vivi.rag.generator import GoogleHandbookGenerator
from src.vivi.rag.runtime import create_services
from src.vivi.structured_llm import google_client
from tests.test_local_llm_graph import _Retriever


def configuration(tmp_path, **overrides):
    defaults = dict(llm_provider="google", google_api_key="google-test",
                    google_model="gemini-3.5-flash-lite",
                    rag_local_only=False, rag_retrieval_mode="sqlite",
                    rag_handbook_db=tmp_path / "book.sqlite3",
                    rag_history_db=tmp_path / "history.sqlite3", memory_enabled=False)
    return Settings(**{**defaults, **overrides})


def test_google_provider_catalog_and_http_request_validation(tmp_path):
    config = configuration(tmp_path)
    assert llm_models(config)["google"] == "gemini-3.5-flash-lite"
    assert "google" in configured_llm_providers(config)
    assert "google" not in configured_llm_providers(configuration(tmp_path, google_api_key=""))
    assert "google" not in llm_models(configuration(tmp_path, rag_local_only=True))
    assert "google" not in configured_llm_providers(configuration(tmp_path, rag_local_only=True))
    assert TurnRequest(transcript="chào", session_id="s", turn_id="t", llm_provider="google").llm_provider == "google"
    assert ConfirmationDecisionRequest(session_id="s", turn_id="t", decision="approve", llm_provider="google").llm_provider == "google"


def test_google_credentials_accept_gemini_alias(monkeypatch):
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.setenv("GEMINI_API_KEY", "gemini-alias-test")
    assert Settings(_env_file=None).google_api_key == "gemini-alias-test"


def test_auto_services_respect_selected_google_provider(tmp_path):
    services = create_services(configuration(tmp_path))
    assert services.generation_provider == "google"
    assert isinstance(services.classifier, GoogleIntentClassifier)
    assert isinstance(services.generator, GoogleHandbookGenerator)


def test_google_rejects_missing_key_and_offline_mode(tmp_path):
    with pytest.raises(RuntimeError, match="GOOGLE_API_KEY"):
        create_services(configuration(tmp_path, google_api_key=""))
    with pytest.raises(RuntimeError, match="Offline RAG"):
        create_services(configuration(tmp_path, rag_local_only=True))


def test_google_has_independent_credentials_budget_and_endpoint(tmp_path):
    captured = {}
    def handler(request):
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content)
        assert request.headers["authorization"] == "Bearer google-test"
        return httpx.Response(200, json={"choices": [{"message": {"content": "{}"}}]})
    client = google_client(configuration(tmp_path, openai_api_key="different-key", google_max_tokens=2048))
    client.transport = httpx.MockTransport(handler)
    client.generate_json(system="ViVi", user="chào", schema={"type": "object"}, schema_name="test")
    assert captured["url"] == "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
    assert captured["body"]["model"] == "gemini-3.5-flash-lite"
    assert captured["body"]["max_tokens"] == 2048
    assert captured["body"]["reasoning_effort"] == "low"
    assert captured["body"]["response_format"]["type"] == "json_schema"


@pytest.mark.asyncio
async def test_google_graph_calls_intent_and_grounded_answer(tmp_path):
    requests = []
    def handler(request):
        body = json.loads(request.content)
        name = body["response_format"]["json_schema"]["name"]
        requests.append(name)
        if name == "vivi_intent_decision":
            result = {"route": "handbook", "intent": "manual.search",
                      "arguments": {"query": "Sạc AC thế nào?"}, "follow_up": None}
        else:
            result = {"answer": "Dừng xe ở vị trí an toàn trước khi kết nối cáp sạc.",
                      "claims": [{"text": "Dừng xe ở vị trí an toàn trước khi kết nối cáp sạc.", "source_ids": ["S1"]}]}
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(result)}}]})
    services = replace(create_services(configuration(tmp_path)), retriever=_Retriever())
    for model in [services.classifier, services.generator]:
        model.client.transport = httpx.MockTransport(handler)
    output = (await build_graph(services).ainvoke({
        "session_id": "google-rag", "turn_id": "one", "input_text": "Sạc AC thế nào?"
    }))["output"]
    assert requests == ["vivi_intent_decision", "vivi_grounded_answer"]
    assert output["route"] == "handbook" and output["grounding_status"] == "supported"
    assert output["citations"][0]["source_id"] == "vf-local-charge"
    assert not output["errors"]
