"""Sequential spoken guidance uses the persisted energy plan and charge curve."""

import math
from dataclasses import replace
from types import SimpleNamespace

import pytest

from src.vivi.dialogue.contracts import NavigationState, SessionState
from src.vivi.navigation.energy import EnergyPlanner
from src.vivi.navigation.presentation import next_leg_message, route_summary
from src.vivi.navigation.transitions import NavigationTransitions
from src.vivi.navigation.vietmap import Route
from src.vivi.navigation.workflow import NavigationWorkflow
from tests.test_long_term_memory import services as services
from tests.test_navigation_workflow import profile, station


@pytest.mark.asyncio
async def test_three_chargers_announce_arrival_and_next_leg_once(services):
    stations = [
        station(identifier=f"s{i}", lat=21 - i * 0.75, lng=106).model_copy(
            update={"name": f"Trạm Sạc TEST {i}", "max_soc": 80}
        )
        for i in range(1, 4)
    ]
    nodes = ["origin", *(s.id for s in stations), "destination"]
    points = [(21 - i * 0.75, 106) for i in range(5)]
    edges = {
        (a, b): Route(
            distance_m=150000,
            duration_s=7500,
            coordinates=[[points[i][1], points[i][0]], [points[i + 1][1], points[i + 1][0]]],
            steps=[],
        ).model_dump()
        for i, (a, b) in enumerate(zip(nodes, nodes[1:]))
    }
    planner = EnergyPlanner(profile(), stations)
    plan = planner.solve(points[0], points[-1], 82, edges, stations)[0]
    assert len(plan["stops"]) == 3
    workflow = NavigationWorkflow(replace(services, energy_planner=planner))
    nav = NavigationState(
        status="running",
        revision=1,
        route_id="route:test",
        initial_soc=82,
        origin={"lat": 21, "lng": 106},
        selected={"name": "Điểm đến TEST", "lat": 18, "lng": 106},
        route=workflow.compose_route(plan),
        energy={"selected_plan": plan},
    )
    summary = route_summary(nav)
    assert "600 km" in summary["speech"] and "dừng sạc 3 lần" in summary["speech"]
    assert "TEST 1" not in summary["speech"] and len(summary["stops"]) == 3
    assert "trạm sạc thứ 1" in next_leg_message(nav)
    services.action_gateway.vehicle.set_energy("driver", 82, 246)
    nav.status, nav.resume_status = "paused", "running"
    resumed, _ = await NavigationTransitions(services.action_gateway.vehicle, planner).apply(
        SimpleNamespace(session_id="driver"), SessionState(navigation=nav), {"kind": "gear", "gear": "D", "id": "drive"}
    )
    nav = NavigationState.model_validate(resumed["navigation_update"])
    assert nav.status == "running" and "trạm sạc thứ 1" in resumed["message"]
    assert resumed["ui_events"][-1]["type"] == "navigation.next_leg"

    async def send(identifier, **values):
        nonlocal nav
        event = SimpleNamespace(
            session_id="driver", turn_id=identifier, navigation_event={"id": identifier, "revision": 1, **values}
        )
        result = await workflow.event(event, SessionState(navigation=nav))
        nav = NavigationState.model_validate(result["navigation_update"])
        return result

    for i, stop in enumerate(plan["stops"]):
        arrived = await send(f"arrive-{i}", kind="progress", distance_m=600000)
        assert nav.status == "charging" and nav.progress_m == (i + 1) * 150000
        assert f"trạm sạc thứ {i + 1}" in arrived["message"]
        assert f"khoảng {math.ceil(stop['charge_minutes'])} phút" in arrived["message"]
        assert arrived["ui_events"][0]["type"] == "navigation.charging"
        assert arrived["ui_events"][0]["id"] == f"arrive-{i}"
        replay = await send(f"arrive-{i}", kind="progress", distance_m=600000)
        assert replay["message"] == "" and nav.stop_index == i
        tick = 0
        while nav.status == "charging":
            charged = await send(f"charge-{i}-{tick}", kind="charge", elapsed_s=30)
            if nav.status == "charging":
                assert charged["message"] == ""
            tick += 1
            assert tick < 80
        assert nav.stop_index == i + 1 and nav.charge_elapsed_s == 0
        assert charged["ui_events"][0]["type"] == "navigation.next_leg"
        if i < 2:
            assert f"trạm sạc thứ {i + 2}" in charged["message"]
        else:
            assert "Điểm đến TEST" in charged["message"]
    await send("finish", kind="progress", distance_m=600000)
    assert nav.status == "completed" and nav.simulated_soc == pytest.approx(plan["arrival_soc"], abs=0.02)
    assert not (await services.action_gateway.vehicle.get_state("driver")).driving
