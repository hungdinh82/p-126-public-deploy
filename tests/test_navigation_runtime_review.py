"""Concurrency, event-loop responsiveness and service-vs-code error boundaries."""

import asyncio
import threading
from dataclasses import replace

import pytest

from src.vivi.dialogue.contracts import NavigationState
from src.vivi.navigation.energy import EnergyPlanner
from src.vivi.navigation.vietmap import VietMapError
from src.vivi.navigation.workflow import NavigationWorkflow, route_position
from tests.test_dialogue_v2 import build, invoke
from tests.test_long_term_memory import services as services
from tests.test_navigation_refinement import DestinationMaps
from tests.test_navigation_workflow import profile, station
from tests.test_trip_plans import CHARGER, DEST, ORIGIN, TripMaps


async def run_turn(graph, data, mode):
    if mode == "invoke":
        return await graph.ainvoke(data)
    result = None
    async for result in graph.astream(data, stream_mode="values"):
        pass
    return graph.project(result)


@pytest.mark.parametrize("first_mode,second_mode", [("invoke", "invoke"), ("stream", "invoke"), ("invoke", "stream")])
@pytest.mark.asyncio
async def test_same_checkpoint_serializes_search_and_selection_but_other_sessions_progress(
    services, tmp_path, first_mode, second_mode
):
    entered, release, second_started = asyncio.Event(), asyncio.Event(), asyncio.Event()

    class BlockingMaps(DestinationMaps):
        async def search(self, *args, **kwargs):
            entered.set()
            await release.wait()
            return await super().search(*args, **kwargs)

    graph, _, gateway = build(
        replace(services, navigation=BlockingMaps(), energy_planner=EnergyPlanner(profile())), tmp_path
    )
    gateway.vehicle.set_energy("driver", 82, 246)
    first = asyncio.create_task(
        run_turn(
            graph,
            {
                "session_id": "driver",
                "turn_id": "search",
                "input_text": "đi Tràng An",
                "position": {"lat": 21, "lng": 105.85},
            },
            first_mode,
        )
    )
    second = None
    try:
        await asyncio.wait_for(entered.wait(), 2)

        async def select():
            second_started.set()
            return await run_turn(
                graph, {"session_id": "driver", "turn_id": "select", "input_text": "chỗ đầu tiên"}, second_mode
            )

        second = asyncio.create_task(select())
        await second_started.wait()
        # This unrelated turn may complete while the first session is awaiting maps.
        other = await asyncio.wait_for(
            graph.ainvoke({"session_id": "other", "turn_id": "music", "input_text": "bật nhạc"}), 2
        )
        assert other["steps"][0]["status"] == "completed"
        assert not second.done()
        release.set()
        searched, selected = await asyncio.gather(first, second)
        assert selected["status"] == "answered"
        assert selected["navigation"]["selected"]["handle"] == searched["navigation"]["candidates"][0]["handle"]
        assert selected["session"]["active"]["id"] == searched["session"]["active"]["id"]
        assert not gateway.vehicle.state_for("driver").driving
    finally:
        release.set()
        for task in [first, second]:
            if task and not task.done():
                task.cancel()
        await asyncio.gather(*(t for t in [first, second] if t), return_exceptions=True)
        graph.close()


@pytest.mark.parametrize("mode", ["invoke", "stream"])
@pytest.mark.asyncio
async def test_cancelled_graph_turn_releases_checkpoint_lock(services, tmp_path, mode):
    entered = asyncio.Event()

    class BlockingMaps(DestinationMaps):
        async def search(self, *args, **kwargs):
            entered.set()
            await asyncio.Future()

    graph, _, _ = build(replace(services, navigation=BlockingMaps()), tmp_path)
    running = asyncio.create_task(
        run_turn(
            graph,
            {
                "session_id": "driver",
                "turn_id": "cancel",
                "input_text": "đi Tràng An",
                "position": {"lat": 21, "lng": 105.85},
            },
            mode,
        )
    )
    try:
        await asyncio.wait_for(entered.wait(), 2)
        running.cancel()
        with pytest.raises(asyncio.CancelledError):
            await running
        completed = await asyncio.wait_for(invoke(graph, "huỷ", "after-cancel"), 2)
        assert completed["turn_id"] == "after-cancel"
    finally:
        running.cancel()
        await asyncio.gather(running, return_exceptions=True)
        graph.close()


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


@pytest.mark.parametrize(
    "error,expected_status", [(VietMapError("API unavailable"), "blocked"), (TypeError("bad route adapter"), "unknown")]
)
@pytest.mark.asyncio
async def test_return_service_error_is_incomplete_but_code_error_is_diagnostic(
    services, tmp_path, caplog, error, expected_status
):
    maps = TripMaps()
    original = maps.route

    async def route(a, b):
        if a == DEST and b == ORIGIN:
            raise error
        return await original(a, b)

    maps.route = route
    graph, _, gateway = build(replace(services, navigation=maps, energy_planner=EnergyPlanner(profile())), tmp_path)
    gateway.vehicle.set_energy("driver", 82, 246)
    try:
        first = await graph.ainvoke(
            {
                "session_id": "driver",
                "turn_id": "first",
                "input_text": "lập kế hoạch tới Tràng An và quay lại",
                "position": {"lat": ORIGIN[0], "lng": ORIGIN[1]},
            }
        )
        assert first["trip_plan"]["status"] == "selecting"
        result = await invoke(graph, "chỗ đầu tiên", "pick")
        assert result["steps"][0]["status"] == expected_status
        assert not gateway.vehicle.state_for("driver").driving
        assert "TypeError" in caplog.text if isinstance(error, TypeError) else "Return route unavailable" in caplog.text
        if isinstance(error, TypeError):
            assert result["errors"][0]["error_type"] == "TypeError"
        else:
            assert result["trip_plan"]["energy"]["error_code"] == "return_route_unavailable"
    finally:
        graph.close()


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
        await workflow.plan_energy(nav, 25, "rules")
    assert any(isinstance(e, TypeError) for e in caught.value.exceptions)
    assert sibling_cancelled.is_set()
