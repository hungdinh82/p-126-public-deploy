"""Remaining journey, origins and SOC isolation after revision changes."""

from dataclasses import replace

import pytest

from src.vivi.dialogue.contracts import NavigationState
from src.vivi.navigation.energy import EnergyPlanner
from src.vivi.navigation.vietmap import Route
from src.vivi.navigation.workflow import NavigationWorkflow, current_origin
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
