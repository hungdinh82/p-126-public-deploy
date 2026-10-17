"""Real checkpoint/vehicle integration for control latency and concurrent turns."""

import asyncio
import time
from dataclasses import replace

import pytest

from tests.support.dialogue_v2 import build
from tests.support.long_term_memory import services as services
from tests.support.navigation_lifecycle import event, runtime, start


@pytest.mark.asyncio
async def test_parking_preempts_model_without_executing_its_stale_proposal(services, tmp_path):
    class SlowModel:
        def __init__(self):
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def agenerate_json(self, **kwargs):
            self.started.set()
            await self.release.wait()
            return '{"act":"request","actions":[{"capability":"climate.set_temperature","arguments":{"value_celsius":27}}]}'

    model = SlowModel()
    graph, _, gateway = build(services, tmp_path, model=model)
    try:
        gateway.vehicle.set_driving("driver", True)
        answering = asyncio.create_task(
            graph.ainvoke({"session_id": "driver", "turn_id": "slow", "input_text": "tôi nóng", "provider": "google"})
        )
        await asyncio.wait_for(model.started.wait(), 2)
        before = time.perf_counter()
        parked = await graph.ainvoke(
            {
                "session_id": "driver",
                "turn_id": "park",
                "navigation_event": {"kind": "gear", "gear": "P", "id": "park", "revision": 0},
            }
        )
        assert time.perf_counter() - before < 0.5
        assert not parked["vehicle_state"]["driving"]
        model.release.set()
        stale = await answering
        assert stale["status"] == "blocked"
        assert stale["vehicle_state"]["temperature_celsius"] != 27
        assert not (await graph.snapshot("driver"))["vehicle_state"]["driving"]
    finally:
        model.release.set()
        await graph.aclose()


@pytest.mark.asyncio
async def test_parking_preempts_vietmap_and_does_not_commit_stale_draft(services, tmp_path):
    from tests.support.navigation_refinement import DestinationMaps

    class SlowMaps(DestinationMaps):
        def __init__(self):
            super().__init__()
            self.started, self.release = asyncio.Event(), asyncio.Event()

        async def search(self, *args, **kwargs):
            self.started.set()
            await self.release.wait()
            return await super().search(*args, **kwargs)

    maps = SlowMaps()
    graph, _, gateway = build(replace(services, navigation=maps), tmp_path)
    try:
        searching = asyncio.create_task(
            graph.ainvoke(
                {
                    "session_id": "driver",
                    "turn_id": "search",
                    "input_text": "đi Tràng An",
                    "position": {"lat": 21, "lng": 105.85},
                }
            )
        )
        await asyncio.wait_for(maps.started.wait(), 2)
        parked = await graph.ainvoke(
            {
                "session_id": "driver",
                "turn_id": "park",
                "navigation_event": {"kind": "gear", "gear": "P", "id": "park", "revision": 0},
            }
        )
        assert not parked["vehicle_state"]["driving"]
        maps.release.set()
        stale = await searching
        assert stale["status"] == "blocked"
        assert stale["navigation_workspace"]["draft"] is None
        assert not gateway.vehicle.state_for("driver").driving
    finally:
        maps.release.set()
        await graph.aclose()


@pytest.mark.asyncio
async def test_snapshot_gear_replay_and_restart_have_unambiguous_order(services, tmp_path):
    graph, store, gateway = runtime(services, tmp_path)
    try:
        running = await start(graph)
        paused = await event(graph, running, "gear", "park", gear="P")
        repeated = await event(graph, paused, "gear", "park", gear="P")
        assert repeated["vehicle_state"]["state_version"] == paused["vehicle_state"]["state_version"]
        snapshot = await graph.snapshot("driver")
        assert snapshot["control_receipts"][-1]["event_id"] == "park"
        assert snapshot["snapshot_sequence"] >= paused["snapshot_sequence"]
        assert snapshot["navigation"]["status"] == "paused"
        old_epoch = snapshot["runtime_epoch"]
        await graph.aclose()
        graph, _, _ = runtime(services, tmp_path, store=store)
        restored = await graph.snapshot("driver")
        assert restored["runtime_epoch"] != old_epoch
        assert restored["navigation"]["status"] == "paused"
        assert restored["snapshot_sequence"] >= snapshot["snapshot_sequence"]
    finally:
        await graph.aclose()


@pytest.mark.asyncio
async def test_telemetry_does_not_invalidate_model_or_restore_old_progress(services, tmp_path):
    class SlowModel:
        def __init__(self):
            self.started, self.release = asyncio.Event(), asyncio.Event()

        async def agenerate_json(self, **kwargs):
            self.started.set()
            await self.release.wait()
            return '{"act":"reply","text":"Mình đây."}'

    model = SlowModel()
    graph, _, _ = runtime(services, tmp_path, model=model)
    try:
        running = await start(graph)
        answering = asyncio.create_task(
            graph.ainvoke(
                {"session_id": "driver", "turn_id": "slow", "input_text": "bạn khỏe không", "provider": "google"}
            )
        )
        await asyncio.wait_for(model.started.wait(), 2)
        progressed = await event(graph, running, "progress", "progress", distance_m=1000)
        model.release.set()
        result = await answering
        assert result["status"] == "answered"
        assert result["navigation"]["progress_m"] == progressed["navigation"]["progress_m"]
        assert result["snapshot_sequence"] > progressed["snapshot_sequence"]
    finally:
        model.release.set()
        await graph.aclose()


@pytest.mark.asyncio
async def test_queued_progress_coalesces_and_parking_runs_before_telemetry(services, tmp_path):
    graph, _, gateway = runtime(services, tmp_path)
    try:
        running = await start(graph)
        config = graph.config(graph.event({"session_id": "driver"}))
        lock = graph.navigation.lock(config["configurable"]["thread_id"])
        await lock.acquire()
        updates = [
            asyncio.create_task(event(graph, running, "progress", f"progress-{i}", distance_m=i * 100))
            for i in range(1, 4)
        ]
        await asyncio.sleep(0.01)
        parking = asyncio.create_task(event(graph, running, "gear", "park", gear="P"))
        await asyncio.sleep(0.01)
        lock.release()
        parked = await parking
        results = await asyncio.gather(*updates)
        assert parked["navigation"]["status"] == "paused"
        assert all(result["navigation"]["progress_m"] == 0 for result in results)
        assert not gateway.vehicle.state_for("driver").driving
        assert results[0]["diagnostics"]["coalesced_events"] == 3
        assert all(result["snapshot_sequence"] == results[0]["snapshot_sequence"] for result in results)
        snapshot = await graph.snapshot("driver")
        assert {"progress-1", "progress-2", "progress-3"} <= set(snapshot["navigation_workspace"]["applied_events"])
        assert (
            snapshot["navigation_workspace"]["applied_events"] == results[0]["navigation_workspace"]["applied_events"]
        )
    finally:
        await graph.aclose()


@pytest.mark.asyncio
async def test_later_queued_park_supersedes_earlier_queued_drive(services, tmp_path):
    graph, _, gateway = runtime(services, tmp_path)
    try:
        config = graph.config(graph.event({"session_id": "driver"}))
        lock = graph.navigation.lock(config["configurable"]["thread_id"])
        await lock.acquire()
        driving = asyncio.create_task(
            graph.ainvoke(
                {
                    "session_id": "driver",
                    "turn_id": "drive",
                    "navigation_event": {"kind": "gear", "gear": "D", "id": "drive", "revision": 0},
                }
            )
        )
        await asyncio.sleep(0.01)
        parking = asyncio.create_task(
            graph.ainvoke(
                {
                    "session_id": "driver",
                    "turn_id": "park",
                    "navigation_event": {"kind": "gear", "gear": "P", "id": "park", "revision": 0},
                }
            )
        )
        await asyncio.sleep(0.01)
        lock.release()
        parked, driven = await asyncio.gather(parking, driving)
        assert not parked["vehicle_state"]["driving"]
        assert driven["status"] == "blocked"
        assert not gateway.vehicle.state_for("driver").driving
        assert (await graph.snapshot("driver"))["control_receipts"][-1]["event_id"] == "drive"
    finally:
        await graph.aclose()


@pytest.mark.asyncio
async def test_voice_gear_uses_same_accepted_journey_as_button(services, tmp_path):
    from tests.support.dialogue_v2 import invoke

    graph, _, gateway = runtime(services, tmp_path)
    try:
        running = await start(graph)
        parked = await invoke(graph, "chuyển về P", "voice-park")
        assert parked["navigation"]["status"] == "paused"
        assert not gateway.vehicle.state_for("driver").driving
        resumed = await invoke(graph, "chuyển sang số D", "voice-drive")
        assert resumed["navigation"]["status"] == "running"
        assert resumed["navigation"]["route_id"] == running["navigation"]["route_id"]
        assert gateway.vehicle.state_for("driver").driving
    finally:
        await graph.aclose()


def test_negated_gear_request_cannot_create_a_drive_action():
    from src.vivi.dialogue.contracts import SessionState
    from eval.legacy.grammar import OfflineParser

    decision = OfflineParser().interpret("không chuyển sang D", SessionState())
    assert not decision.actions or decision.negated


@pytest.mark.asyncio
async def test_coalesced_charge_ignores_duplicate_and_previously_applied_elapsed(services, tmp_path):
    from src.vivi.navigation.energy import EnergyPlanner
    from tests.support.dialogue_v2 import invoke
    from tests.support.navigation_workflow import HANOI, STOP, IntercityMaps, profile, station

    vehicle = services.action_gateway.vehicle
    vehicle.set_energy("driver", 40, 120)
    graph, _, _ = build(
        replace(
            services,
            navigation=IntercityMaps(),
            energy_planner=EnergyPlanner(profile(), [station(lat=STOP[0], lng=STOP[1])]),
        ),
        tmp_path,
    )
    try:
        await graph.ainvoke(
            {
                "session_id": "driver",
                "turn_id": "find",
                "input_text": "đi Hải Phòng TEST",
                "position": {"lat": HANOI[0], "lng": HANOI[1]},
            }
        )
        await invoke(graph, "chỗ đầu tiên", "select")
        running = await invoke(graph, "đồng ý", "start")
        charging = await event(graph, running, "progress", "to-stop", distance_m=60000)
        assert charging["navigation"]["status"] == "charging"
        charging = await event(graph, charging, "charge", "already-applied", elapsed_s=10)
        config = graph.config(graph.event({"session_id": "driver"}))
        lock = graph.navigation.lock(config["configurable"]["thread_id"])
        await lock.acquire()
        updates = [
            asyncio.create_task(event(graph, charging, "charge", identifier, elapsed_s=10))
            for identifier in ["already-applied", "new-1", "new-1", "new-2"]
        ]
        await asyncio.sleep(0.01)
        lock.release()
        results = await asyncio.gather(*updates)
        assert all(result["navigation"]["charge_elapsed_s"] == 30 for result in results)
        replay = await event(graph, results[-1], "charge", "new-1", elapsed_s=10)
        assert replay["navigation"]["charge_elapsed_s"] == 30
        repeated_last = await event(graph, replay, "charge", "new-2", elapsed_s=10)
        assert repeated_last["navigation"]["charge_elapsed_s"] == 30
    finally:
        await graph.aclose()
