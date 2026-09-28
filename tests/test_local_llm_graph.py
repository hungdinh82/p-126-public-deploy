from __future__ import annotations

import json

import httpx

from src.vivi.agents.classifier import StructuredAPIIntentClassifier
from src.vivi.agents.graph import build_graph
from src.vivi.config import Settings
from src.vivi.history.sqlite import SQLiteConversationHistory
from src.vivi.orchestration import create_langgraph_orchestrator
from src.vivi.persistence.event_store import EventStore
from src.vivi.rag.generator import StructuredAPIHandbookGenerator
from src.vivi.rag.runtime import HandbookServices
from src.vivi.rag.schemas import RetrievedChunk
from src.vivi.structured_llm import StructuredChatClient
from src.vivi.vehicle.memory import VehicleSimulator


def _retrieved_chunk() -> RetrievedChunk:
    return RetrievedChunk(
        source_id="vf-local-charge",
        document_id="VF8-2026-vi_vn-1",
        source_url="manual://vf8/2026/charge",
        vehicle_model="VF8",
        model_year=2026,
        locale="vi_vn",
        chapter_id=1,
        chapter="Sạc xe",
        section_path=["Pin", "Sạc AC"],
        content_type="paragraph",
        content="Dừng xe ở vị trí an toàn trước khi kết nối cáp sạc.",
        checksum="local-test",
        chunk_index=0,
    )


class _Retriever:
    def retrieve(self, query, vehicle_model, model_year, locale):
        assert query == "Sạc AC thế nào?"
        assert (vehicle_model, model_year, locale) == ("VF8", 2026, "vi_vn")
        return [_retrieved_chunk()]


def test_openai_compatible_local_graph_routes_retrieves_and_cites(tmp_path):
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        schema_name = payload["response_format"]["json_schema"]["name"]
        requests.append(schema_name)
        if schema_name == "vivi_intent_decision":
            content = {
                "route": "handbook",
                "intent": "manual.search",
                "arguments": {"query": "Sạc AC thế nào?"},
                "confidence": 0.99,
            }
        else:
            content = {
                "answer": "Hãy dừng xe ở vị trí an toàn trước khi kết nối cáp sạc.",
                "claims": [
                    {
                        "text": "Dừng xe an toàn trước khi sạc.",
                        "source_ids": ["vf-local-charge"],
                    }
                ],
                "citations": [
                    {
                        "source_id": "vf-local-charge",
                        "title": "Sạc xe",
                        "section_path": ["Pin", "Sạc AC"],
                        "source_url": "manual://vf8/2026/charge",
                    }
                ],
                "insufficient_evidence": False,
            }
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": json.dumps(content, ensure_ascii=False)}}]},
        )

    client = StructuredChatClient(
        base_url="http://llama.test/v1",
        api_key="local",
        model="qwen-test.gguf",
        timeout_seconds=1,
        transport=httpx.MockTransport(handler),
    )
    services = HandbookServices(
        retriever=_Retriever(),
        generator=StructuredAPIHandbookGenerator(client),
        history=SQLiteConversationHistory(tmp_path / "history.sqlite3"),
        classifier=StructuredAPIIntentClassifier(client),
    )
    result = build_graph(services).invoke(
        {
            "session_id": "local-rag",
            "turn_id": "turn-1",
            "input_text": "Sạc AC thế nào?",
        }
    )

    assert requests == ["vivi_intent_decision", "vivi_grounded_answer"]
    assert result["output"]["route"] == "handbook"
    assert result["output"]["status"] == "answered"
    assert result["output"]["grounding_status"] == "supported"
    assert result["output"]["citations"][0]["source_id"] == "vf-local-charge"


def test_configured_local_provider_is_registered_in_canonical_graph(tmp_path):
    config = Settings(
        llm_provider="local",
        local_llm_model="qwen-test.gguf",
        google_api_key="",
        openai_api_key="",
        data_dir=tmp_path / "data",
        rag_handbook_db=tmp_path / "handbook.sqlite3",
        rag_history_db=tmp_path / "history.sqlite3",
    )
    vehicle = VehicleSimulator()
    store = EventStore(config)
    orchestrator = create_langgraph_orchestrator(vehicle, store, config)

    assert orchestrator.graph_providers == {"rules", "local"}
