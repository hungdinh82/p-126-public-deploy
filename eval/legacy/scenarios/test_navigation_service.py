"""Navigation ownership, ordering, crash recovery and public transport contracts."""

import asyncio
from threading import Event
from unittest.mock import patch

import pytest

from src.vivi.api.runtime import runtime as api_runtime
from src.vivi.navigation.transport import compact_snapshot
from src.vivi.orchestration import LangGraphOrchestrator
from src.vivi.persistence.checkpoints import ThreadedSqliteSaver
from tests.support.dialogue_v2 import build, invoke
from tests.support.long_term_memory import services as services
from tests.support.navigation_lifecycle import event, runtime, start


async def ordered(graph, gear, identifier, sequence, client="tab-1", **extra):
    snapshot = await graph.snapshot("driver")
    return await event(
        graph,
        snapshot,
        "gear",
        identifier,
        gear=gear,
        client_id=client,
        control_sequence=sequence,
        runtime_epoch=graph.runtime_epoch,
        **extra,
    )


@pytest.mark.asyncio
async def test_delayed_drive_cannot_overtake_newer_park_even_after_restart(services, tmp_path):
    graph, store, gateway = runtime(services, tmp_path)
    try:
        parked = await ordered(graph, "P", "new-park", 2)
        stale = await ordered(graph, "D", "delayed-drive", 1)
        assert stale["receipt"]["error_code"] == "superseded_control"
        assert not gateway.vehicle.state_for("driver").driving
        await graph.aclose()
        graph, _, gateway = runtime(services, tmp_path, store=store)
        stale = await ordered(graph, "D", "delayed-after-restart", 1)
        assert stale["receipt"]["error_code"] == "superseded_control"
        assert parked["receipt"]["status"] == "completed"
    finally:
        await graph.aclose()


@pytest.mark.asyncio
async def test_stale_target_is_blocked_and_calculation_trace_separates_dependencies(services, tmp_path):
    graph, _, gateway = runtime(services, tmp_path)
    try:
        await graph.ainvoke(
            {
                "session_id": "driver",
                "turn_id": "find",
                "input_text": "đi Tràng An",
                "position": {"lat": 21, "lng": 105.85},
            }
        )
        preview = await invoke(graph, "chỗ đầu tiên", "choose")
        timings = preview["output"]["timings"]
        assert "navigation_route" in timings
        assert "navigation_energy" in timings
        running = await invoke(graph, "đồng ý", "start")
        stale = await event(graph, running, "pause", "wrong-journey", target_id="route:retired")
        assert stale["receipt"]["error_code"] == "stale_target"
        assert gateway.vehicle.state_for("driver").driving
        parked = await event(graph, running, "pause", "right-journey", target_id=running["navigation"]["route_id"])
        assert not parked["vehicle_state"]["driving"]
    finally:
        await graph.aclose()


@pytest.mark.asyncio
async def test_parking_preempts_route_replanning_inside_telemetry(services, tmp_path):
    from src.vivi.dialogue.contracts import SessionState
    from tests.support.navigation_refinement import DestinationMaps

    class SlowMaps(DestinationMaps):
        def __init__(self):
            super().__init__()
            self.entered, self.release = asyncio.Event(), asyncio.Event()

        async def route(self, *args):
            self.entered.set()
            await self.release.wait()
            return await super().route(*args)

    graph, _, _ = runtime(services, tmp_path)
    maps, progress = SlowMaps(), None
    try:
        running = await start(graph)
        state = await graph.navigation.hydrate(SessionState(), "driver")
        state.navigation_workspace.journey.initial_soc = 8
        await graph.navigation.save(state, "driver")
        graph.navigation.vehicle.set_energy("driver", 8, 24)
        graph.navigation.workflow.services.navigation.client = maps
        progress = asyncio.create_task(event(graph, running, "progress", "low-energy", distance_m=1000))
        try:
            await asyncio.wait_for(maps.entered.wait(), 2)
        except TimeoutError:
            if progress.done():
                raise AssertionError(progress.result()["output"])
            raise
        parked = await asyncio.wait_for(ordered(graph, "P", "park-during-replan", 1), 0.5)
        assert parked["navigation"]["status"] == "paused"
        maps.release.set()
        obsolete = await asyncio.wait_for(progress, 2)
        assert obsolete["receipt"]["error_code"] == "superseded_turn"
        current = await graph.snapshot("driver")
        assert current["navigation"]["status"] == "paused"
        assert current["navigation"]["pending"] is None
        assert not current["vehicle_state"]["driving"]
    finally:
        maps.release.set()
        await graph.aclose()
        if progress:
            await asyncio.gather(progress, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("park_kind", ["gear", "pause"])
async def test_two_tabs_use_control_revision_even_when_parking_does_not_change_vehicle(services, tmp_path, park_kind):
    graph, _, gateway = runtime(services, tmp_path)
    try:
        before = await graph.snapshot("driver")
        if park_kind == "gear":
            await ordered(graph, "P", "tab-a-park", 1, client="tab-a")
        else:
            await event(graph, before, "pause", "tab-a-park")
        stale = await ordered(
            graph,
            "D",
            "tab-b-old-drive",
            1,
            client="tab-b",
            expected_vehicle_version=before["vehicle_state"]["state_version"],
            expected_control_revision=before["control_revision"],
        )
        assert stale["receipt"]["error_code"] == "stale_control"
        assert not gateway.vehicle.state_for("driver").driving
        fresh = await graph.snapshot("driver")
        accepted = await ordered(
            graph,
            "D",
            "tab-b-new-drive",
            2,
            client="tab-b",
            expected_vehicle_version=fresh["vehicle_state"]["state_version"],
            expected_control_revision=fresh["control_revision"],
        )
        assert accepted["vehicle_state"]["driving"]
    finally:
        await graph.aclose()


@pytest.mark.asyncio
async def test_restart_parks_free_drive_with_receipt_without_changing_unowned_vehicle_state(services, tmp_path):
    graph, store, gateway = runtime(services, tmp_path)
    try:
        driven = await ordered(graph, "D", "free-drive", 1)
        assert driven["vehicle_state"]["driving"]
        await graph.aclose()
        graph, _, _ = runtime(services, tmp_path, store=store)
        assert not (await graph.snapshot("driver"))["vehicle_state"]["driving"]
    finally:
        await graph.aclose()


@pytest.mark.asyncio
async def test_telemetry_never_writes_dialogue_checkpoint_and_receipt_survives_recent_list(services, tmp_path):
    graph, store, gateway = runtime(services, tmp_path)
    try:
        running = await start(graph)
        key = graph.navigation.key("driver")
        with graph.saver.cursor(transaction=False) as cursor:
            initial = cursor.execute("SELECT COUNT(*) FROM checkpoints WHERE thread_id=?", (key,)).fetchone()[0]
        for index in range(3):
            running = await event(graph, running, "progress", f"progress-{index}", distance_m=(index + 1) * 100)
        snapshot = await graph.compiled.aget_state(graph.config({"session_id": "driver"}))
        assert "navigation_reference" in snapshot.values["session"]
        assert "navigation_workspace" not in snapshot.values["session"]
        with graph.saver.cursor(transaction=False) as cursor:
            assert cursor.execute("SELECT COUNT(*) FROM checkpoints WHERE thread_id=?", (key,)).fetchone()[0] == initial
        paused = await ordered(graph, "P", "original-park", 1)
        for index in range(34):
            await ordered(graph, "P", f"park-{index}", index + 2)
        latest = await graph.snapshot("driver", "original-park")
        assert latest["receipt"]["status"] == "completed"
        assert not any(item["event_id"] == "original-park" for item in latest["control_receipts"])
        replay = await event(
            graph,
            paused,
            "gear",
            "original-park",
            gear="P",
            client_id="tab-1",
            control_sequence=1,
            runtime_epoch=graph.runtime_epoch,
        )
        assert replay["receipt"]["status"] == "completed"
        assert not gateway.vehicle.state_for("driver").driving
        with store.connect() as db:
            assert db.execute("SELECT COUNT(*) FROM navigation_receipts").fetchone()[0] >= 38
    finally:
        await graph.aclose()


@pytest.mark.asyncio
async def test_unknown_effect_is_not_replayed_and_event_payload_collision_is_rejected(services, tmp_path):
    graph, _, gateway = runtime(services, tmp_path)
    try:
        value = {"kind": "gear", "gear": "D", "id": "interrupted", "revision": 0}
        # Reservation without a result represents crash after possible dispatch.
        from src.vivi.navigation.workflow import NavigationEvent

        graph.navigation.repository.reserve(
            graph.navigation.key("driver"), NavigationEvent.model_validate(value).model_dump(mode="json")
        )
        result = await graph.ainvoke({"session_id": "driver", "turn_id": "recover", "navigation_event": value})
        assert result["receipt"]["status"] == "unknown"
        assert not gateway.vehicle.state_for("driver").driving
        # Dispatch might have happened before a crash lost the result. Recovery
        # must park even when there is no completed receipt or session row.
        gateway.vehicle.set_driving("driver", True)
        await graph.aclose()
        graph, _, gateway = runtime(services, tmp_path)
        recovered = await graph.snapshot("driver", "interrupted")
        assert not recovered["vehicle_state"]["driving"]
        assert recovered["receipt"]["status"] == "unknown"
        assert recovered["control_uncertain"]
        await ordered(graph, "P", "explicit-recovery-park", 1)
        assert not (await graph.snapshot("driver"))["control_uncertain"]
        with pytest.raises(ValueError, match="event_id"):
            await graph.ainvoke(
                {"session_id": "driver", "turn_id": "conflict", "navigation_event": {**value, "gear": "P"}}
            )
    finally:
        await graph.aclose()


@pytest.mark.asyncio
async def test_cancelled_dispatched_drive_settles_before_newer_park(services, tmp_path):
    graph, _, gateway = runtime(services, tmp_path)
    entered, release = Event(), Event()
    original = gateway.vehicle.set_driving
    drive, park = None, None

    def blocked_drive(session_id, driving, **kwargs):
        if driving:
            entered.set()
            assert release.wait(3)
        return original(session_id, driving, **kwargs)

    try:
        with patch.object(gateway.vehicle, "set_driving", blocked_drive):
            drive = asyncio.create_task(ordered(graph, "D", "cancelled-drive", 1))
            assert await asyncio.to_thread(entered.wait, 2)
            drive.cancel()
            park = asyncio.create_task(ordered(graph, "P", "park-after-cancel", 2))
            await asyncio.sleep(0.05)
            assert not park.done()
            drive.cancel()
            await asyncio.sleep(0.01)
            assert not park.done()
            release.set()
            assert isinstance((await asyncio.gather(drive, return_exceptions=True))[0], asyncio.CancelledError)
            parked = await asyncio.wait_for(park, 2)
            assert parked["receipt"]["status"] == "completed"
            assert not gateway.vehicle.state_for("driver").driving
            assert not (await graph.snapshot("driver"))["control_uncertain"]
    finally:
        release.set()
        await graph.aclose()
        await asyncio.gather(*(task for task in (drive, park) if task), return_exceptions=True)


@pytest.mark.asyncio
async def test_checkpoint_close_waits_for_cancelled_worker_without_closing_connection(services, tmp_path):
    saver = ThreadedSqliteSaver(tmp_path / "worker.sqlite3")
    entered, release = Event(), Event()

    def blocked_read():
        entered.set()
        release.wait(5)
        return saver.connection.execute("SELECT 1").fetchone()[0]

    task = asyncio.create_task(saver._run(blocked_read))
    await asyncio.to_thread(entered.wait, 2)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    closing = asyncio.create_task(saver.aclose())
    await asyncio.sleep(0.01)
    assert not closing.done()
    release.set()
    await asyncio.wait_for(closing, 2)
    await saver.aclose()  # Idempotent teardown.


@pytest.mark.asyncio
async def test_navigation_shutdown_drains_cancelled_sqlite_work(services, tmp_path):
    graph, _, _ = runtime(services, tmp_path)
    entered, release = Event(), Event()

    def blocked_write():
        entered.set()
        release.wait(5)
        with graph.navigation.repository.ledger.connect() as db:
            db.execute("INSERT INTO runtime_schema VALUES (100)")

    task = asyncio.create_task(graph.navigation.repository.run(blocked_write))
    await asyncio.to_thread(entered.wait, 2)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    closing = asyncio.create_task(graph.aclose())
    await asyncio.sleep(0.01)
    assert not closing.done()
    release.set()
    await asyncio.wait_for(closing, 2)


@pytest.mark.asyncio
async def test_migration_backs_up_once_preserves_history_and_parks_legacy_journey(services, tmp_path):
    from langgraph.checkpoint.base import empty_checkpoint

    from src.vivi.dialogue.contracts import SessionState
    from src.vivi.navigation.store import NavigationRepository
    from src.vivi.persistence.runtime_store import RuntimeStore

    graph, _, _ = runtime(services, tmp_path / "fixture")
    try:
        running = await start(graph)
        legacy = SessionState.model_validate(running["session"])
        legacy.navigation_workspace.history = [{"id": "old", "status": "ended"}]
        store = RuntimeStore(tmp_path / "legacy-runtime.sqlite3")
        saver = ThreadedSqliteSaver(tmp_path / "legacy-checkpoints.sqlite3")
        checkpoint = empty_checkpoint()
        checkpoint["channel_values"] = {"session": legacy.model_dump(mode="json")}
        checkpoint["channel_versions"] = {"session": "1"}
        key = graph.navigation.key("driver")
        saver.put({"configurable": {"thread_id": key, "checkpoint_ns": ""}}, checkpoint, {}, {"session": "1"})
        repository = NavigationRepository(store)
        try:
            repository.migrate(saver)
            migrated = SessionState.model_validate(repository.load(key))
            assert migrated.navigation_workspace.journey.status == "paused"
            assert migrated.navigation_workspace.journey.resume_status == "running"
            assert migrated.navigation_workspace.history == [{"id": "old", "status": "ended"}]
            backups = list((tmp_path / "backups").glob("navigation-*"))
            assert len(backups) == 1
            assert (backups[0] / "runtime.sqlite3").exists()
            assert (backups[0] / "checkpoints.sqlite3").exists()
            repository.migrate(saver)
            assert list((tmp_path / "backups").glob("navigation-*")) == backups
        finally:
            await repository.aclose()
            await saver.aclose()
    finally:
        await graph.aclose()


@pytest.mark.asyncio
async def test_shutdown_cancels_active_graph_before_closing_store(services, tmp_path):
    class Model:
        def __init__(self):
            self.started = asyncio.Event()

        async def agenerate_json(self, **kwargs):
            self.started.set()
            await asyncio.Event().wait()

    model = Model()
    graph, _, _ = build(services, tmp_path, model=model)
    task = asyncio.create_task(
        graph.ainvoke({"session_id": "driver", "turn_id": "in-flight", "input_text": "tôi nóng", "provider": "google"})
    )
    await asyncio.wait_for(model.started.wait(), 2)
    await asyncio.wait_for(graph.aclose(), 2)
    assert task.cancelled()
    with pytest.raises(RuntimeError, match="closing"):
        await graph.ainvoke({"session_id": "driver", "turn_id": "too-late", "input_text": "xin chào"})


@pytest.mark.asyncio
async def test_public_event_api_and_legacy_use_same_domain_without_provider_probe(client, services, tmp_path):
    graph, ledger, gateway = runtime(services, tmp_path)
    pipeline = LangGraphOrchestrator({"rules": graph}, gateway, api_runtime.store, ledger=ledger)
    try:
        with patch.object(api_runtime, "orchestrator", pipeline), patch.object(api_runtime, "vehicle", gateway.vehicle):
            response = await client.post(
                "/api/v1/navigation/events",
                json={"session_id": "driver", "event": {"kind": "gear", "gear": "D", "id": "api-drive", "revision": 0}},
            )
            assert response.status_code == 200, response.text
            assert response.json()["vehicle_state"]["driving"]
            response = await client.post(
                "/api/v1/turn",
                json={
                    "session_id": "driver",
                    "turn_id": "legacy",
                    "transcript": "P",
                    "navigation_event": {"kind": "gear", "gear": "P", "id": "legacy-park", "revision": 0},
                    "llm_provider": "local",
                },
            )
            assert response.status_code == 200, response.text
            assert not response.json()["vehicle_state"]["driving"]
            response = await client.get(
                "/api/v1/navigation/snapshot", params={"session_id": "driver", "event_id": "api-drive"}
            )
            assert response.json()["receipt"]["status"] == "completed"
    finally:
        await pipeline.aclose()


@pytest.mark.asyncio
async def test_map_config_never_calls_upstream_probe(client):
    from src.vivi.navigation.vietmap import VietMapClient

    async def forbidden(*args, **kwargs):
        raise AssertionError("Config must not probe upstream")

    with (
        patch.object(api_runtime.settings, "vietmap_api_key", "fixture"),
        patch.object(VietMapClient, "probe_tiles", forbidden),
    ):
        response = await client.get("/api/v1/navigation/config")
        assert response.status_code == 200
        assert response.json()["available"]


@pytest.mark.asyncio
async def test_position_replay_is_not_changed_by_server_observation_clock(services, tmp_path):
    graph, _, _ = runtime(services, tmp_path)
    try:
        before = await graph.snapshot("driver")

        async def send(turn, lat):
            return await graph.ainvoke(
                {
                    "session_id": "driver",
                    "turn_id": turn,
                    "navigation_event": {
                        "kind": "position",
                        "id": "same-position",
                        "revision": before["navigation"]["revision"],
                        "position": {"lat": lat, "lng": 105},
                    },
                }
            )

        first = await send("first", 21)
        replay = await send("replay", 21)
        assert replay["navigation"]["origin"] == first["navigation"]["origin"]
        assert (await graph.snapshot("driver", "same-position"))["receipt"]["status"] == "completed"
        with pytest.raises(ValueError, match="event_id"):
            await send("conflict", 22)
    finally:
        await graph.aclose()


def test_transport_preserves_current_soc_and_omits_only_known_geometry():
    route = {"coordinates": [[105, 21], [106, 21]], "distance_m": 1000, "duration_s": 10, "steps": []}
    payload = {
        "navigation": {"route_id": "r1", "route": route, "waypoint_routes": [route], "simulated_soc": 49},
        "navigation_workspace": {"journey": {"route_id": "r1", "route": route}},
        "trip_plan": None,
    }
    compact = compact_snapshot(payload, {"r1"})
    assert "route" not in compact["navigation"]
    assert compact["navigation"]["route_reference"] == "r1"
    assert compact["navigation"]["simulated_soc"] == 49
    assert compact_snapshot(payload, set()) == payload
    assert payload["navigation"]["route"] == route
