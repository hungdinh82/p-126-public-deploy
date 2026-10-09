from __future__ import annotations

import json

import httpx

from src.vivi.config import Settings
from src.vivi.dialogue.contracts import SemanticTurn
from src.vivi.providers import configured_llm_providers
from src.vivi.structured_llm import openrouter_client


def _settings(**overrides) -> Settings:
    return Settings(openrouter_api_key="or-test", rag_local_only=False, **overrides)


def test_openrouter_is_configured_only_with_key():
    assert "openrouter" in configured_llm_providers(_settings())
    assert "openrouter" not in configured_llm_providers(Settings(openrouter_api_key=""))


def test_openrouter_request_disables_reasoning_and_spells_out_schema():
    captured = {}
    decision = {"act":"reply", "text":"Chào bạn"}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["headers"] = request.headers
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(decision)}}]})

    client = openrouter_client(_settings())
    client.transport = httpx.MockTransport(handler)
    value = client.generate_json(
        system="Bạn là ViVi.",
        user="Xin chào",
        schema=SemanticTurn.model_json_schema(),
        schema_name="vivi_semantic_turn",
    )

    assert SemanticTurn.model_validate_json(value).act == "reply"
    body = captured["body"]
    assert captured["url"] == "https://openrouter.ai/api/v1/chat/completions"
    assert captured["headers"]["authorization"] == "Bearer or-test"
    assert body["model"] == "qwen/qwen3.7-flash"
    assert body["reasoning"] == {"enabled": False}
    assert body["max_tokens"] == 1536
    assert '"capability"' in body["messages"][0]["content"]
