"""Replay default-point acknowledgments without consulting a model or maps again."""
import time

import pytest

from src.vivi.config import Settings
from src.vivi.dialogue.contracts import DialogueTask, NavigationState, SemanticTurn, SessionState, TaskStep
from eval.legacy.grammar import OfflineParser
from src.vivi.dialogue.reducer import reduce_turn
from src.vivi.domain.models import TurnRequest
from src.vivi.orchestration import LangGraphOrchestrator
from src.vivi.persistence.event_store import EventStore
from src.vivi.turn_control import TurnControl, current_control
from tests.support.default_destination_review import UnavailableModel as UnavailableModel
from tests.support.dialogue_v2 import build, invoke
from tests.support.long_term_memory import services as services
from tests.support.trip_plans import ORIGIN, runtime


@pytest.mark.asyncio
@pytest.mark.parametrize("answer", ["Ok,.", "Xác nhận.", "Giữ địa điểm.", "Đến trung tâm tỉnh Ninh Bình luôn."])
async def test_default_acknowledgment_keeps_calculated_point_without_search_or_start(services, tmp_path, answer):
    services, maps = runtime(services)
    model = UnavailableModel()
    graph, _, gateway = build(services, tmp_path, model, local=model)
    try:
        first = await graph.ainvoke({
            "session_id": "driver", "turn_id": "first",
            "input_text": "chỉ đường và lập lộ trình sạc tới Ninh Bình",
            "position": {"lat": ORIGIN[0], "lng": ORIGIN[1]},
        })
        assert first["trip_plan"]["clarification"]["phase"] == "confirm_destination"
        assert len(first["steps"]) == 1 and first["steps"][0]["capability"] == "trip.plan"
        counts = len(maps.queries), len(maps.routes)
        done = await invoke(graph, answer, "accept")
        assert done["trip_plan"]["selected"] == first["trip_plan"]["selected"]
        assert done["trip_plan"]["calculation_status"] == "ready"
        assert done["trip_plan"]["offer"]["status"] == "pending"
        assert counts == (len(maps.queries), len(maps.routes))
        assert model.calls == 0
        assert done["navigation"]["status"] == "idle" and not gateway.vehicle.state_for("driver").driving
    finally:
        await graph.aclose()


def place_state(phase="confirm_destination"):
    return SessionState(navigation=NavigationState(revision=4, status="selecting", clarification={
        "phase": phase, "region": "Quảng Ngãi", "revision": 4,
        "choices": [
            {"kind": "representative", "handle": "keep"}, {"kind": "specific", "handle": "change"},
        ],
    }), active=DialogueTask(steps=[TaskStep(draft={"capability": "navigation.plan"}, status="waiting_slot")]))


@pytest.mark.parametrize("phase,text,handle", [
    ("confirm_destination", "có", "keep"),
    ("confirm_destination", "không", "change"),
    ("confirm_destination", "đổi địa điểm", "change"),
    ("change_destination", "có", "change"),
    ("change_destination", "không", "keep"),
    ("change_destination", "OK", "keep"),
    ("change_destination", "Giữ địa điểm", "keep"),
])
def test_new_and_legacy_question_polarity(phase, text, handle):
    result = OfflineParser().place_reference(text, place_state(phase))
    assert result.actions[0].capability == "navigation.refine"
    assert result.actions[0].arguments.choice_handle == handle


@pytest.mark.parametrize("phase,act,handle", [
    ("confirm_destination", "confirm", "keep"), ("confirm_destination", "deny", "change"),
    ("change_destination", "confirm", "change"), ("change_destination", "deny", "keep"),
])
def test_typed_yes_no_answers_place_question_without_driving_consent(phase, act, handle):
    state, error = reduce_turn(place_state(phase), SemanticTurn(act=act))
    assert error is None
    draft = state.active.steps[0].draft
    assert draft.capability == "navigation.refine" and draft.arguments.choice_handle == handle


def test_stale_choice_other_region_and_other_task_are_not_acknowledged():
    parser = OfflineParser()
    state = place_state()
    assert parser.place_reference("Đến trung tâm tỉnh Quảng Ngãi luôn.", state).actions[0].arguments.choice_handle == "keep"
    assert parser.place_reference("Đến trung tâm Đà Nẵng", state) is None
    state.navigation_workspace.draft.clarification["revision"] = 3
    assert parser.place_reference("OK", state) is None
    state = place_state()
    state.active.steps[0].draft.capability = "window.set_position"
    assert parser.place_reference("OK", state) is None


@pytest.mark.parametrize("text", [
    "Đà Nẵng", "chỉ đường đến Đà Nẵng rồi bật nhạc", "nếu pin đủ thì đi Đà Nẵng",
    "không đi Đà Nẵng", "ngày mai đi Đà Nẵng", "chỉ đường tới quán cà phê",
])
def test_explicit_region_shortcut_does_not_handle_ambiguous_or_compound_requests(text):
    assert OfflineParser().explicit_navigation_reference(text, SessionState()) is None


@pytest.mark.asyncio
async def test_stream_hydrates_only_consumed_updates_but_preserves_public_geometry(services, tmp_path):
    counts = {}
    for optimized in (False, True):
        runtime_services, _ = runtime(services)
        graph, _, gateway = build(runtime_services, tmp_path / str(optimized))
        calls = 0
        original = graph.navigation.hydrate

        async def hydrate(*args, **kwargs):
            nonlocal calls
            calls += 1
            return await original(*args, **kwargs)

        graph.navigation.hydrate = hydrate
        try:
            for turn, text in [("first", "lập kế hoạch tới Ninh Bình"), ("keep", "OK")]:
                request = {"session_id": "driver", "turn_id": turn, "input_text": text,
                           "position": {"lat": ORIGIN[0], "lng": ORIGIN[1]}}
                stream = graph.astream_dialogue(request) if optimized else graph.astream(
                    request, stream_mode=["updates", "values"])
                frames = [item async for item in stream]
                updates = [chunk["capability"] for mode, chunk in frames
                           if mode == "updates" and "capability" in chunk]
                assert updates and all(u["session"]["trip_plan"]["route"]["coordinates"] for u in updates)
                final = [chunk for mode, chunk in frames if mode == "values"][-1]
                assert final["output"]["turn_id"] == turn
                assert final["trip_plan"]["route"]["coordinates"]
                if turn == "keep":
                    assert final["trip_plan"]["calculation_status"] == "ready"
                assert not gateway.vehicle.state_for("driver").driving
            counts[optimized] = calls
        finally:
            await graph.aclose()
    assert counts[True] < counts[False], counts


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_checkpoint_and_domain_waits_are_logged_separately_from_total(services, tmp_path, stream):
    services, _ = runtime(services)
    graph, _, _ = build(services, tmp_path)
    store = EventStore(Settings(data_dir=tmp_path))
    pipeline = LangGraphOrchestrator({"rules": graph}, None, store, default_provider="rules")
    request = TurnRequest(session_id="driver", turn_id="one", transcript="chỉ đường đến Ninh Bình",
                          position={"lat": ORIGIN[0], "lng": ORIGIN[1]})
    token = current_control.set(TurnControl(time.monotonic() + 30))
    try:
        if stream:
            response = [item async for item in pipeline.run_stream(request)][-1]["response"]
        else:
            response = (await pipeline.run(request)).model_dump(mode="json")
        io = response["diagnostics"]["io_timings"]
        assert io["checkpoint"]["calls"] > 0 and io["checkpoint"]["latency_ms"] >= 0
        assert io["navigation_store"]["calls"] > 0 and io["navigation_store"]["latency_ms"] >= 0
        assert response["latency_ms"] == store.last_event["latency_ms"]
    finally:
        current_control.reset(token)
        await graph.aclose()
