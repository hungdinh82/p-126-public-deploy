"""Concurrency, event-loop responsiveness and service-vs-code error boundaries."""

import asyncio
import threading
from dataclasses import replace

import pytest

from src.vivi.dialogue.contracts import NavigationState
from src.vivi.navigation.energy import EnergyPlanner
from src.vivi.navigation.workflow import NavigationWorkflow, route_position
from tests.support.long_term_memory import services as services
from tests.support.navigation_workflow import profile, station
from tests.support.trip_plans import CHARGER, DEST, ORIGIN


async def run_turn(graph, data, mode):
    if mode == "invoke":
        return await graph.ainvoke(data)
    result = None
    async for result in graph.astream(data, stream_mode="values"):
        pass
    return graph.project(result)


@pytest.mark.asyncio
async def test_geometry_calculation_runs_in_worker_and_does_not_block_loop(monkeypatch):
    import src.vivi.navigation.workflow as module

    loop_thread = threading.get_ident()
    entered, release = threading.Event(), threading.Event()
    original = module._route_position

    def blocked(route, distance):
        assert threading.get_ident() != loop_thread
        entered.set()
        if not release.wait(2):
            raise TimeoutError("worker was not released by event loop")
        return original(route, distance)

    monkeypatch.setattr(module, "_route_position", blocked)
    route = {"coordinates": [[105 + i / 100000, 21] for i in range(100001)], "distance_m": 100000}
    task = asyncio.create_task(route_position(route, 50000))
    try:
        assert await asyncio.to_thread(entered.wait, 1)
        # We reached this code while the worker was still blocked.
        assert not task.done()
        release.set()
        result = await asyncio.wait_for(task, 2)
        assert result["lng"] == pytest.approx(105.5, abs=1e-6)
        assert result["lat"] == 21
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_charging_edge_code_error_propagates_and_cancels_siblings(services):
    sibling_started, sibling_cancelled = asyncio.Event(), asyncio.Event()

    class BrokenMaps:
        async def route(self, a, b):
            if a == ORIGIN:
                sibling_started.set()
                try:
                    await asyncio.Future()
                finally:
                    sibling_cancelled.set()
            await sibling_started.wait()
            raise TypeError("invalid charging leg")

    energy = EnergyPlanner(profile(), [station(lat=CHARGER[0], lng=CHARGER[1])])
    nav = NavigationState(
        origin={"lat": ORIGIN[0], "lng": ORIGIN[1]},
        selected={"lat": DEST[0], "lng": DEST[1]},
        route={"coordinates": [[105.8, 21], [105.8, 20]], "distance_m": 120000, "duration_s": 6000},
    )
    workflow = NavigationWorkflow(replace(services, navigation=BrokenMaps(), energy_planner=energy))
    with pytest.raises(ExceptionGroup) as caught:
        await workflow.plan_energy(nav, 25, "google")
    assert any(isinstance(e, TypeError) for e in caught.value.exceptions)
    assert sibling_cancelled.is_set()
