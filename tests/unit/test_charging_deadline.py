"""Oct 11: spoken result selection and the interrupted Sầm Sơn return plan."""

import asyncio
from dataclasses import replace

import pytest

from src.vivi.dialogue.contracts import NavigationState, SessionState, TripPlanState
from src.vivi.navigation.energy import EnergyPlanner
from tests.support.long_term_memory import services as services
from tests.support.navigation_workflow import profile
from tests.support.trip_plans import TripMaps


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

    nav = NavigationState(
        origin={"lat": 21, "lng": 105.8},
        selected={"lat": 20, "lng": 105.8},
        route={"distance_m": 120000, "duration_s": 6000, "coordinates": [[105.8, 21], [105.8, 20]]},
    )
    workflow = NavigationWorkflow(
        replace(
            services, navigation=TripMaps(), energy_planner=EnergyPlanner(profile()), charging_discovery=SlowDiscovery()
        )
    )
    control = TurnControl(time.monotonic() + 5.1)
    token = current_control.set(control)
    try:
        energy, alternatives = await asyncio.wait_for(workflow.plan_energy(nav, 30, "google"), 1)
        assert energy["station_search"]["incomplete"] and energy.get("selected_plan") is None
        assert not alternatives and cancelled.is_set()
        assert time.monotonic() < control.deadline
    finally:
        current_control.reset(token)
