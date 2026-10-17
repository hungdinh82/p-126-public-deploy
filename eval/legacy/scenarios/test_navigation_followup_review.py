"""Remaining journey, origins and SOC isolation after revision changes."""

from dataclasses import replace

import pytest

from src.vivi.dialogue.contracts import ActionDraft, NavigationState, SessionState, TaskStep, TurnEvent
from src.vivi.navigation.energy import EnergyPlanner
from src.vivi.navigation.vietmap import Route
from src.vivi.navigation.workflow import NavigationWorkflow, current_origin
from tests.support.dialogue_v2 import build, invoke
from tests.support.long_term_memory import services as services
from tests.support.navigation_workflow import profile

START = (21, 105.8)
DEST = (20, 105.8)
NOW = (20.5, 105.8)


def road(a, b, distance):
    return Route(distance_m=distance, duration_s=distance / 20, coordinates=[[a[1], a[0]], [b[1], b[0]]], steps=[])


def journey():
    return NavigationState(
        status="running",
        trip_kind="round_trip",
        origin={"lat": 21, "lng": 105.8, "label": "Hà Nội"},
        selected={"lat": 20, "lng": 105.8, "name": "Điểm đến"},
        route={
            "coordinates": [[105.8, 21], [105.8, 20], [105.8, 21]],
            "distance_m": 270000,
            "duration_s": 13500,
            "steps": [],
        },
        waypoint_routes=[road(START, DEST, 120000).model_dump(), road(DEST, START, 150000).model_dump()],
        energy={
            "selected_plan": {
                "legs": [{"to": "destination", "distance_m": 130000}, {"to": "return", "distance_m": 150000}]
            }
        },
    )


@pytest.mark.asyncio
async def test_replan_outbound_refreshes_both_legs_without_modifying_running_route(services):
    old = journey()
    old.progress_m = 125000  # detoured outbound is 130 km, not old direct 120 km
    calls = []

    class Maps:
        async def route(self, a, b):
            calls.append((a, b))
            return road(a, b, 20000 if a == NOW else 140000)

    workflow = NavigationWorkflow(replace(services, navigation=Maps(), energy_planner=EnergyPlanner(profile())))
    candidate = old.model_copy(deep=True)
    candidate.origin = {"lat": NOW[0], "lng": NOW[1]}
    await workflow.refresh_remaining_route(candidate, old)
    assert calls == [(NOW, DEST), (DEST, START)]
    assert candidate.route["distance_m"] == 160000
    assert [r["distance_m"] for r in candidate.waypoint_routes] == [20000, 140000]
    assert old.route["distance_m"] == 270000 and old.origin["lat"] == 21


@pytest.mark.asyncio
async def test_replan_return_does_not_route_back_to_outward_destination(services):
    old = journey()
    old.progress_m = 140000
    calls = []

    class Maps:
        async def route(self, a, b):
            calls.append((a, b))
            return road(a, b, 50000)

    candidate = old.model_copy(deep=True)
    candidate.origin = {"lat": NOW[0], "lng": NOW[1]}
    workflow = NavigationWorkflow(replace(services, navigation=Maps(), energy_planner=EnergyPlanner(profile())))
    await workflow.refresh_remaining_route(candidate, old)
    assert calls == [(NOW, START)]
    assert candidate.trip_kind == "one_way"
    assert candidate.selected["name"] == "Hà Nội" and candidate.selected["lat"] == 21
    assert len(candidate.waypoint_routes) == 1 and candidate.route["distance_m"] == 50000


@pytest.mark.parametrize("status,progress,latitude", [("completed", 10000, 20), ("paused", 5000, 20.5)])
@pytest.mark.asyncio
async def test_checkpoint_current_origin_uses_last_position(status, progress, latitude):
    nav = NavigationState(
        status=status,
        progress_m=progress,
        origin={"lat": 21, "lng": 105.8},
        route=road(START, DEST, 10000).model_dump(),
    )
    assert (await current_origin(nav))["lat"] == pytest.approx(latitude)
    assert nav.origin["lat"] == 21  # stored departure remains available for round-trip return


@pytest.mark.asyncio
async def test_new_route_does_not_inherit_previous_soc_simulation(services, tmp_path):
    services.action_gateway.vehicle.set_energy("driver", 75, 225)

    class Maps:
        async def route(self, a, b):
            return road(a, b, 10000)

    graph, _, gateway = build(services, tmp_path)
    graph.close()
    workflow = NavigationWorkflow(
        replace(services, action_gateway=gateway, navigation=Maps(), energy_planner=EnergyPlanner(profile()))
    )
    old = NavigationState(
        status="paused",
        revision=3,
        origin={"lat": 21, "lng": 105.8},
        route=road(START, DEST, 10000).model_dump(),
        progress_m=5000,
        initial_soc=82,
        simulated_soc=40,
        charge_elapsed_s=12,
        candidates=[{"handle": "place:next", "name": "Điểm mới", "lat": 20, "lng": 105.8}],
    )
    session = SessionState(navigation=old)
    step = TaskStep(draft=ActionDraft(capability="navigation.select", arguments={"place_handle": "place:next"}))
    result = await workflow.execute(
        TurnEvent(session_id="driver", turn_id="select", kind="action", text=""),
        session,
        step,
        {"place_handle": "place:next"},
        "select:next",
    )
    state = NavigationState.model_validate(result["navigation_update"])
    assert state.initial_soc == 75 and state.simulated_soc is None and state.charge_elapsed_s == 0
    assert state.origin["lat"] == pytest.approx(20.5)
    state.status = "running"
    tick = await workflow.event(
        TurnEvent(
            session_id="driver",
            turn_id="tick",
            kind="navigation",
            navigation_event={"kind": "progress", "id": "tick", "revision": state.revision, "distance_m": 1000},
        ),
        SessionState(navigation=state),
    )
    assert tick["navigation_update"]["simulated_soc"] == pytest.approx(75 - workflow.energy.soc_cost(1000))


@pytest.mark.parametrize("soc,expected", [(75, "answered"), (20, "blocked")])
@pytest.mark.asyncio
async def test_navigation_consent_revalidates_changed_soc(services, tmp_path, soc, expected):
    from tests.support.navigation_refinement import DestinationMaps

    graph, _, gateway = build(
        replace(services, navigation=DestinationMaps(), energy_planner=EnergyPlanner(profile())), tmp_path
    )
    gateway.vehicle.set_energy("driver", 82, 246)
    try:
        await graph.ainvoke(
            {
                "session_id": "driver",
                "turn_id": "route",
                "input_text": "đi Tràng An",
                "position": {"lat": 21, "lng": 105.85},
            }
        )
        preview = await invoke(graph, "chỗ đầu tiên", "choose")
        gateway.vehicle.set_energy("driver", soc, soc * 3)
        changed = await invoke(graph, "đồng ý", "accept-old")
        assert changed["status"] == expected
        assert not gateway.vehicle.state_for("driver").driving
        assert changed["navigation"]["initial_soc"] == soc
        if expected == "confirmation_required":
            assert changed["confirmation"]["confirmation_id"] != preview["confirmation"]["confirmation_id"]
            assert changed["navigation"]["revision"] > preview["navigation"]["revision"]
            confirmed = await invoke(graph, "đồng ý", "accept-new")
            assert confirmed["navigation"]["status"] == "running"
    finally:
        graph.close()
