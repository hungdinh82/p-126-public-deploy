from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient

from src.vivi.api.app import create_app
from src.vivi.config import Settings
from src.vivi.orchestration import LangGraphOrchestrator
from src.vivi.persistence.event_store import EventStore
from tests.support.core import Harness, action


def application(harness, tmp_path):
    config = Settings(
        _env_file=None,
        data_dir=tmp_path,
        llm_provider="google",
        rag_local_only=False,
        jetson_ai_policy="disabled",
        stt_provider="off",
        tts_provider="off",
    )
    store = EventStore(config)
    orchestrator = LangGraphOrchestrator(harness.graph, harness.gateway, store, ledger=harness.ledger)
    orchestrator.semantic_providers = {"google"}
    runtime = SimpleNamespace(settings=config, orchestrator=orchestrator, vehicle=harness.vehicle, store=store)
    return create_app(config=config, runtime=runtime)


@pytest.mark.asyncio
async def test_typed_action_and_question_answer_share_one_v2_envelope(tmp_path):
    h = Harness(tmp_path)
    try:
        app = application(h, tmp_path)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            first = await client.post(
                "/api/v2/turn",
                json={
                    "session_id": "driver",
                    "turn_id": "one",
                    "input": {
                        "kind": "action",
                        "action": {
                            "capability": "window.set_position",
                            "arguments": {"zone": "driver", "position_percent": 50},
                        },
                    },
                },
            )
            assert first.status_code == 200
            quote = first.json()
            assert quote["contract_version"] == 2
            assert "trip_plan" not in quote and "navigation" not in quote
            assert quote["diagnostics"]["semantic_source"] == "typed_event"
            snapshot = await client.get("/api/v2/navigation/snapshot?session_id=driver")
            assert snapshot.json()["pending_question"]["id"] == quote["pending_question"]["id"]
            answer = {
                "kind": "question_reply",
                "answer": {"question_id": quote["pending_question"]["id"], "option_id": "yes"},
            }
            done = await client.post("/api/v2/turn", json={"session_id": "driver", "turn_id": "two", "input": answer})
            assert done.status_code == 200 and done.json()["vehicle_state"]["window_positions"]["driver"] == 50
            stale = await client.post(
                "/api/v2/turn", json={"session_id": "driver", "turn_id": "three", "input": answer}
            )
            assert stale.json()["status"] == "clarify"
            assert not h.model.calls
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_utterance_and_stream_have_same_state_and_effect_contract(tmp_path):
    h = Harness(tmp_path, [action("climate.set_temperature", {"value_celsius": 25})])
    try:
        async with AsyncClient(transport=ASGITransport(app=application(h, tmp_path)), base_url="http://test") as client:
            # An omitted observation time must not become a new payload on retry.
            body = {
                "session_id": "driver",
                "turn_id": "same",
                "input": {"kind": "utterance", "text": "đặt 25 độ"},
                "position": {"lat": 21, "lng": 105.85},
            }
            first = await client.post("/api/v2/turn/stream", json=body)
            import json

            events = [json.loads(line) for line in first.text.splitlines()]
            final = next(e["response"] for e in events if e["type"] == "final")
            version = h.vehicle.state_for("driver").state_version
            replay = await client.post("/api/v2/turn", json=body)
            assert replay.json() == final
            assert h.vehicle.state_for("driver").state_version == version
            assert len(h.model.calls) == 1
            collision = await client.post(
                "/api/v2/turn", json={**body, "input": {"kind": "utterance", "text": "đặt 26 độ"}}
            )
            assert collision.status_code == 409
    finally:
        await h.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        {"input": {"kind": "utterance", "text": ""}},
        {"input": {"kind": "action", "text": "bật nhạc", "action": {"capability": "media.play"}}},
        {"input": {"kind": "question_reply", "answer": {"question_id": "q", "option_id": "yes", "value": "x"}}},
        {"input": {"kind": "utterance", "text": "xin chào"}, "provider": "rules"},
    ],
)
async def test_v2_rejects_ambiguous_or_invalid_wire_input(tmp_path, payload):
    h = Harness(tmp_path)
    try:
        async with AsyncClient(transport=ASGITransport(app=application(h, tmp_path)), base_url="http://test") as client:
            result = await client.post("/api/v2/turn", json={"session_id": "driver", "turn_id": "bad", **payload})
            assert result.status_code == 422
            assert not h.model.calls
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_navigation_snapshot_and_events_do_not_call_provider(tmp_path):
    h = Harness(tmp_path)
    try:
        async with AsyncClient(transport=ASGITransport(app=application(h, tmp_path)), base_url="http://test") as client:
            snapshot = await client.get("/api/v2/navigation/snapshot?session_id=driver")
            assert snapshot.status_code == 200 and snapshot.json()["contract_version"] == 2
            response = await client.post(
                "/api/v2/navigation/events",
                json={"session_id": "driver", "event": {"id": "park", "kind": "gear", "gear": "P", "revision": 0}},
            )
            assert response.status_code == 200 and response.json()["receipt"]["status"] == "completed"
            assert not h.model.calls
            assert (await client.get("/assistant-client.js")).status_code == 200
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_cancelled_stream_before_dispatch_cannot_execute_late_model_result(tmp_path):
    import asyncio
    import json

    from src.vivi.domain.models import TurnRequest

    entered, release = asyncio.Event(), asyncio.Event()
    h = Harness(tmp_path)
    pending = None
    try:

        async def slow(**kwargs):
            entered.set()
            await release.wait()
            return json.dumps(action("media.play"))

        h.model.agenerate_json = slow
        pipeline = application(h, tmp_path).state.runtime.orchestrator
        request = TurnRequest(session_id="driver", turn_id="slow", transcript="bật nhạc")

        async def consume():
            return [event async for event in pipeline.run_stream(request, "google")]

        pending = asyncio.create_task(consume())
        await asyncio.wait_for(entered.wait(), 1)
        assert pipeline.cancel("driver", "slow") == {"cancelled": True, "dispatched": False}
        result = await asyncio.gather(pending, return_exceptions=True)
        assert isinstance(result[0], asyncio.CancelledError)
        release.set()
        assert not h.vehicle.state_for("driver").media_playing
        with h.ledger.connect() as db:
            assert db.execute("SELECT COUNT(*) FROM command_ledger").fetchone()[0] == 0
        followup = await pipeline.run(
            TurnRequest(
                session_id="driver", turn_id="next", transcript="status", action={"capability": "vehicle.get_status"}
            ),
            "google",
        )
        assert followup.vehicle_state.state_version == 0
        with h.ledger.connect() as db:
            assert db.execute("SELECT COUNT(*) FROM command_ledger").fetchone()[0] == 1  # status read only
    finally:
        release.set()
        if pending:
            await asyncio.gather(pending, return_exceptions=True)
        await h.close()
