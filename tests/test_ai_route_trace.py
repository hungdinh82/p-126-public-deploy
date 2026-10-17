import json

import httpx
import pytest

from src.vivi.config import Settings
from src.vivi.conversation.service import ConversationService
from src.vivi.dialogue.contracts import SessionState
from src.vivi.dialogue.planner import ModelProvider, SemanticResolver
from src.vivi.persistence.event_store import EventStore
from src.vivi.structured_llm import StructuredChatClient


@pytest.mark.asyncio
async def test_structured_client_records_model_confirmed_by_response():
    def handler(request):
        body = json.loads(request.content)
        assert body["model"] == "requested-model"
        return httpx.Response(
            200,
            json={
                "model": "served-model-revision-7",
                "choices": [{"message": {"content": '{"act":"reply","text":"Xin chào"}'}}],
            },
        )

    client = StructuredChatClient(
        base_url="http://model.test/v1",
        api_key="",
        model="requested-model",
        timeout_seconds=1,
        transport=httpx.MockTransport(handler),
    )
    await client.agenerate_json(
        system="test",
        user="test",
        schema={"type": "object"},
        schema_name="trace_test",
    )
    assert client.served_model == "served-model-revision-7"


@pytest.mark.asyncio
async def test_resolver_emits_calling_and_exact_served_model():
    events = []

    class Client:
        model = "requested-model"
        served_model = "served-model"

        async def agenerate_json(self, **_kwargs):
            return '{"act":"reply","text":"Xin chào bạn"}'

    resolver = SemanticResolver(
        {"google": ModelProvider("google", Client(), 1)},
        attempt_recorder=events.append,
        provider_details={"google": {"model": "requested-model", "location": "cloud"}},
    )
    _, served, attempts = await resolver.resolve(
        "xin chào vivi",
        SessionState(),
        requested="google",
        trace_context={"session_id": "trace-session", "turn_id": "trace-turn"},
    )

    assert served == "google"
    assert [event["status"] for event in events] == ["calling", "served"]
    assert events[-1]["requested_model"] == "requested-model"
    assert events[-1]["served_model"] == "served-model"
    assert attempts[-1]["served_model"] == "served-model"


@pytest.mark.asyncio
async def test_resolver_records_cloud_failure_and_local_fallback():
    store = EventStore(Settings(_env_file=None, store_transcripts=False))

    class Cloud:
        model = "cloud-requested"

        async def agenerate_json(self, **_kwargs):
            raise ConnectionError("cloud unavailable")

    class Local:
        model = "local-requested"
        served_model = "local-confirmed"
        execution_location = "jetson"

        async def agenerate_json(self, **_kwargs):
            return '{"act":"reply","text":"Xin chào bạn"}'

    resolver = SemanticResolver(
        {"google": ModelProvider("google", Cloud(), 1), "local": ModelProvider("local", Local(), 1)},
        attempt_recorder=store.record_ai_attempt,
    )
    _, served, attempts = await resolver.resolve(
        "xin chào vivi",
        SessionState(),
        requested="google",
        trace_context={"session_id": "s", "turn_id": "t"},
    )

    assert served == "local"
    assert [attempt["outcome"] for attempt in attempts] == ["fallback", "served"]
    recorded = store.ai_route_snapshot()[0]["llm_attempts"]
    assert [attempt["status"] for attempt in recorded] == ["fallback", "served"]
    assert recorded[0]["requested_model"] == "cloud-requested"
    assert recorded[0]["served_model"] is None
    assert recorded[1]["served_model"] == "local-confirmed"
    assert recorded[1]["location"] == "jetson"


@pytest.mark.asyncio
async def test_conversation_generation_retains_gateway_model_identity():
    class Client:
        model = "requested-answer-model"
        served_model = "confirmed-answer-model"
        execution_location = "jetson"

        async def agenerate_json(self, **_kwargs):
            return '{"text":"Chúc bạn một ngày tốt lành!","supports":[]}'

    service = ConversationService({"local": ModelProvider("local", Client(), 1)})
    result = await service.respond("chào bạn", provider="local", mode="chat")

    assert result["status"] == "completed"
    attempt = result["generation_attempts"][0]
    assert attempt["requested_model"] == "requested-answer-model"
    assert attempt["served_model"] == "confirmed-answer-model"
    assert attempt["location"] == "jetson"
    assert attempt["calls"] == 1


def test_event_store_keeps_fallback_attempt_and_served_attempt(tmp_path):
    store = EventStore(Settings(_env_file=None, data_dir=tmp_path, store_transcripts=False))
    store.start_ai_route("s", "t", transcript="Tìm trạm sạc", provider="google")
    store.record_ai_attempt(
        {
            "session_id": "s",
            "turn_id": "t",
            "stage": "llm",
            "attempt": 1,
            "provider": "google",
            "location": "cloud",
            "requested_model": "gemini",
            "served_model": None,
            "status": "fallback",
            "outcome": "fallback",
            "latency_ms": 500,
        }
    )
    store.record_ai_attempt(
        {
            "session_id": "s",
            "turn_id": "t",
            "stage": "llm",
            "attempt": 2,
            "provider": "local",
            "location": "jetson",
            "requested_model": "qwen",
            "served_model": "qwen-build-4",
            "status": "served",
            "outcome": "served",
            "latency_ms": 900,
        }
    )
    store.finish_ai_route("s", "t", status="verified", provider="local")

    route = store.ai_route_snapshot()[0]
    assert [attempt["status"] for attempt in route["llm_attempts"]] == ["fallback", "served"]
    assert route["llm_attempts"][0]["served_model"] is None
    assert route["llm_attempts"][1]["served_model"] == "qwen-build-4"
    assert route["effective_provider"] == "local"
