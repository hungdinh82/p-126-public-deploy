"""Oct 11: spoken result selection and the interrupted Sầm Sơn return plan."""

import asyncio
import json
from dataclasses import replace

import pytest

from eval.legacy.grammar import OfflineParser
from src.vivi.dialogue.contracts import NavigationState, SemanticTurn, SessionState, TripPlanState
from src.vivi.dialogue.planner import ModelProvider, SemanticResolver
from src.vivi.navigation.energy import EnergyPlanner
from tests.support.dialogue_v2 import build
from tests.support.long_term_memory import services as services
from tests.support.navigation_workflow import profile
from tests.support.trip_plans import ORIGIN, TripMaps


def selecting(trip=False):
    places = [
        dict(handle="place:one", name="AEON Mall Long Biên Đường Cổ Linh", lat=21, lng=105.9),
        dict(handle="place:two", name="Aeon Mall Long Biên", lat=21.01, lng=105.91),
    ]
    nav = NavigationState(status="selecting", revision=4, candidates=places)
    return SessionState(
        navigation_workspace={"draft": nav.model_dump()},
        trip_plan=TripPlanState(**nav.model_dump()) if trip else None,
        navigation_focus="trip" if trip else "navigation",
    )


@pytest.mark.parametrize("trip", [False, True])
@pytest.mark.parametrize("text,index", [("cái thứ 2", 1), ("cái thứ hai", 1), ("chọn số 1", 0), ("thứ hai", 1)])
def test_literal_ordinal_repairs_wrong_handle_type_and_model_index(trip, text, index):
    state = selecting(trip)
    prefix = "trip" if trip else "navigation"
    args = {"choice_handle": "place:two"}
    if trip:
        args.update(plan_id=state.trip_plan.id, route_revision=4)
    model = SemanticTurn(act="request", actions=[{"capability": prefix + ".refine", "arguments": args}])
    result = OfflineParser().guard(text, model, state)
    assert result.actions[0].capability == prefix + ".select"
    assert result.actions[0].arguments.place_handle == state.navigation.candidates[index]["handle"]
    assert result.actions[0].arguments.choice_handle is None


@pytest.mark.asyncio
async def test_literal_selection_does_not_wait_for_model_or_choose_unavailable_ordinal():
    class NeverModel:
        async def agenerate_json(self, **kwargs):
            raise AssertionError("A supplied ordinal needs no model")

    resolver = SemanticResolver({"google": ModelProvider("google", NeverModel(), 1)})
    value, provider, attempts = await resolver.resolve("cái thứ hai", selecting(), requested="google")
    assert value.actions[0].arguments.place_handle == "place:two"
    assert provider == "rules" and attempts[0]["source"] == "candidate_reference"
    value, _, _ = await resolver.resolve("chọn số 9", selecting(), requested="google")
    assert value.act == "clarify" and not value.drafts()


@pytest.mark.asyncio
async def test_large_round_trip_keeps_geometry_out_of_dialogue_checkpoint(services, tmp_path):
    from src.vivi.navigation.vietmap import Route

    class LargeMaps(TripMaps):
        async def route(self, a, b):
            route = await super().route(a, b)
            data = route.model_dump()
            data["distance_m"] = 80000
            data["coordinates"] = [
                [a[1] + (b[1] - a[1]) * i / 9999, a[0] + (b[0] - a[0]) * i / 9999]
                for i in range(10000)
            ]
            return Route.model_validate(data)

    graph, _, gateway = build(replace(services, navigation=LargeMaps(), energy_planner=EnergyPlanner(profile())), tmp_path)
    gateway.vehicle.set_energy("driver", 100, 400)
    try:
        result = await graph.ainvoke({
            "session_id": "driver", "turn_id": "return", "input_text": "lập lộ trình tới Sầm Sơn và quay lại",
            "position": {"lat": ORIGIN[0], "lng": ORIGIN[1]},
        })
        # Rules first ask to accept the single POI; selection computes both legs.
        if result["trip_plan"]["status"] == "selecting":
            result = await graph.ainvoke({"session_id": "driver", "turn_id": "select", "input_text": "chọn số 1"})
        assert result["trip_plan"]["trip_kind"] == "round_trip"
        assert len(result["trip_plan"]["route"]["coordinates"]) >= 10000
        assert not gateway.vehicle.state_for("driver").driving
        config = graph.config(graph.event({"session_id": "driver", "turn_id": "inspect", "input_text": "x"}))
        state = (await graph.compiled.aget_state(config)).values
        encoded = json.dumps(state, ensure_ascii=False)
        assert '"coordinates"' not in encoded
        assert len(encoded) < 60000
        assert result["response_text"] and "bắt đầu" in result["response_text"]
    finally:
        await graph.aclose()


@pytest.mark.asyncio
async def test_logged_voice_selection_keeps_task_then_previews_without_driving(services, tmp_path):
    from src.vivi.navigation.vietmap import Place

    class Maps(TripMaps):
        async def search(self, *args, **kwargs):
            return [Place(ref_id="one", name="AEON Mall Long Biên", lat=20, lng=105.8),
                    Place(ref_id="two", name="Aeon Mall Long Biên Đường Cổ Linh", lat=20, lng=105.81)]

        async def route(self, a, b):
            from src.vivi.navigation.vietmap import Route
            self.routes.append((a, b))
            return Route(distance_m=7000, duration_s=1000, coordinates=[[a[1], a[0]], [b[1], b[0]]], steps=[])

    class Model:
        async def agenerate_json(self, **kwargs):
            if kwargs["schema_name"] == "destination_choice":
                return json.dumps(dict(mode="clarify", handle=None, reason="ambiguous"))
            prompt = json.loads(kwargs["user"])
            assert prompt["utterance"] == "tới aeon mall long biên"
            return json.dumps(dict(act="request", actions=[{
                "capability": "navigation.plan", "arguments": {"query": "aeon mall long biên"},
            }]))

    maps = Maps()
    graph, _, gateway = build(replace(services, navigation=maps, energy_planner=EnergyPlanner(profile())), tmp_path, Model())
    try:
        first = await graph.ainvoke({"session_id": "driver", "turn_id": "search", "input_text": "tới aeon mall long biên",
                                   "position": {"lat": ORIGIN[0], "lng": ORIGIN[1]}})
        selected = await graph.ainvoke({"session_id": "driver", "turn_id": "select", "input_text": "cái thứ hai"})
        assert selected["navigation"]["selected"]["handle"] == first["navigation"]["candidates"][1]["handle"]
        assert selected["session"]["active"]["id"] == first["session"]["active"]["id"]
        assert selected["navigation"]["status"] == "preview"
        assert not gateway.vehicle.state_for("driver").driving
    finally:
        await graph.aclose()


@pytest.mark.asyncio
async def test_ambiguous_plan_routes_only_user_selected_candidate_and_return(services, tmp_path):
    from src.vivi.navigation.vietmap import Place, Route

    class Maps(TripMaps):
        active = 0
        peak = 0

        async def search(self, *args, **kwargs):
            return [Place(ref_id=str(i), name=f"Sầm Sơn cổng {i}", lat=20 + i / 100, lng=105.8) for i in range(3)]

        async def route(self, a, b):
            self.active += 1
            self.peak = max(self.peak, self.active)
            self.routes.append((a, b))
            try:
                await asyncio.sleep(.03)
                return Route(distance_m=100000, duration_s=5000, coordinates=[[a[1], a[0]], [b[1], b[0]]], steps=[])
            finally:
                self.active -= 1

    class Model:
        async def agenerate_json(self, **kwargs):
            if kwargs["schema_name"] == "destination_choice":
                place = json.loads(kwargs["user"])["candidates"][0]
                return json.dumps(dict(mode="propose", handle=place["handle"], reason="exact_destination"))
            return json.dumps(dict(act="request", actions=[{
                "capability": "trip.plan", "arguments": {"query": "Sầm Sơn", "trip_kind": "round_trip"},
            }]))

    maps = Maps()
    graph, _, gateway = build(replace(services, navigation=maps, energy_planner=EnergyPlanner(profile())), tmp_path, Model())
    gateway.vehicle.set_energy("driver", 100, 400)
    try:
        result = await graph.ainvoke({"session_id": "driver", "turn_id": "return", "input_text": "đi Sầm Sơn và quay lại",
                                     "position": {"lat": ORIGIN[0], "lng": ORIGIN[1]}})
        assert result["trip_plan"]["calculation_status"] == "draft" and maps.routes == []
        result = await graph.ainvoke({"session_id": "driver", "turn_id": "select", "input_text": "chọn số 1"})
        assert result["trip_plan"]["calculation_status"] == "ready"
        assert maps.peak == 1
        assert len(maps.routes) == 2  # Only the chosen outbound and return legs.
    finally:
        await graph.aclose()


@pytest.mark.asyncio
async def test_timeout_is_logged_with_stage_and_turn_identity(services, tmp_path):
    from src.vivi.config import Settings
    from src.vivi.domain.models import TurnRequest
    from src.vivi.orchestration import LangGraphOrchestrator
    from src.vivi.persistence.event_store import EventStore

    class Model:
        started = False

        async def agenerate_json(self, **kwargs):
            self.started = True
            await asyncio.Future()

    model = Model()
    graph, ledger, gateway = build(services, tmp_path, model)
    # Allow SQLite/checkpoint setup to finish; the turn deadline must expire
    # while interpreting, before the provider's own fallback timeout.
    graph.navigation.workflow.resolver.providers["google"].budget_seconds = 5
    events = EventStore(Settings(data_dir=tmp_path))
    orchestrator = LangGraphOrchestrator({"google": graph}, gateway, events, ledger=ledger, timeout_seconds=1)
    try:
        with pytest.raises(TimeoutError):
            await orchestrator.run(TurnRequest(session_id="driver", turn_id="timeout", transcript="đi Sầm Sơn"), "google")
        error = events.last_event
        assert model.started
        assert error["event_type"] == "turn_error" and error["error_code"] == "turn_timeout"
        assert error["stage"] == "interpret" and error["turn_id"] == "timeout"
        assert ledger.turn_status("default:driver", "timeout")["status"] == "interrupted"
        assert not gateway.vehicle.state_for("driver").driving
    finally:
        await graph.aclose()


@pytest.mark.asyncio
async def test_charging_read_budget_returns_incomplete_before_turn_deadline(services):
    import time

    from src.vivi.navigation.workflow import NavigationWorkflow
    from src.vivi.turn_control import TurnControl, current_control

    cancelled = asyncio.Event()

    class SlowDiscovery:
        budget_seconds = 60

        async def discover(self, route):
            try:
                await asyncio.Future()
            finally:
                cancelled.set()

    nav = NavigationState(origin={"lat": 21, "lng": 105.8}, selected={"lat": 20, "lng": 105.8},
                          route={"distance_m": 120000, "duration_s": 6000, "coordinates": [[105.8, 21], [105.8, 20]]})
    workflow = NavigationWorkflow(
        replace(services, navigation=TripMaps(), energy_planner=EnergyPlanner(profile()), charging_discovery=SlowDiscovery())
    )
    control = TurnControl(time.monotonic() + 5.1)
    token = current_control.set(control)
    try:
        energy, alternatives = await asyncio.wait_for(workflow.plan_energy(nav, 30, "rules"), 1)
        assert energy["station_search"]["incomplete"] and energy.get("selected_plan") is None
        assert not alternatives and cancelled.is_set()
        assert time.monotonic() < control.deadline
    finally:
        current_control.reset(token)


@pytest.mark.asyncio
async def test_parking_preempts_candidate_batch_and_fences_all_results(services, tmp_path):
    from src.vivi.navigation.vietmap import Place, Route

    started, release = asyncio.Event(), asyncio.Event()

    class Maps(TripMaps):
        entered = 0

        async def search(self, *args, **kwargs):
            return [Place(ref_id=str(i), name=f"Sầm Sơn cổng {i}", lat=20 + i / 100, lng=105.8) for i in range(3)]

        async def route(self, a, b):
            self.entered += 1
            if self.entered == 3:
                started.set()
            await release.wait()
            return Route(distance_m=100000, duration_s=5000, coordinates=[[a[1], a[0]], [b[1], b[0]]], steps=[])

    graph, _, gateway = build(replace(services, navigation=Maps(), energy_planner=EnergyPlanner(profile())), tmp_path)
    pending = asyncio.create_task(graph.ainvoke({
        "session_id": "driver", "turn_id": "search", "input_text": "tính tuyến", "position": {"lat": 21, "lng": 105.8},
        "action": {"capability": "navigation.search", "arguments": {"query": "Sầm Sơn"}},
    }))
    try:
        await asyncio.wait_for(started.wait(), 2)
        parked = await asyncio.wait_for(graph.ainvoke({
            "session_id": "driver", "turn_id": "park", "input_text": "P",
            "navigation_event": {"id": "park", "kind": "gear", "gear": "P", "revision": 0},
        }), 2)
        assert parked["receipt"]["status"] == "completed" and not pending.done()
        release.set()
        result = await pending
        assert result["diagnostics"]["navigation_control"]["reason"] == "superseded_turn"
        assert not gateway.vehicle.state_for("driver").driving
        assert (await graph.snapshot("driver"))["trip_plan"] is None
    finally:
        release.set()
        if not pending.done():
            pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)
        await graph.aclose()


@pytest.mark.asyncio
async def test_stream_timeout_has_stable_code_and_turn_identity(monkeypatch):
    from types import SimpleNamespace

    from src.vivi.api.routes import turns
    from src.vivi.domain.models import TurnRequest

    async def provider(requested):
        return "rules"

    async def timeout(*args, **kwargs):
        yield {"type": "provider", "provider": "rules"}
        raise TimeoutError()

    monkeypatch.setattr(turns, "_provider", provider)
    monkeypatch.setattr(turns.runtime, "orchestrator", SimpleNamespace(run_stream=timeout))
    response = await turns.turn_stream(TurnRequest(session_id="driver", turn_id="timeout", transcript="đi Sầm Sơn"))
    events = [json.loads(chunk) async for chunk in response.body_iterator]
    error = events[-1]
    assert error["type"] == "error" and error["error_code"] == "turn_timeout"
    assert error["turn_id"] == "timeout" and "vượt thời gian" in error["detail"]
