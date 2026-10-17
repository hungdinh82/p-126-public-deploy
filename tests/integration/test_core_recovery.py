from dataclasses import replace

import pytest

from fixtures.core import Harness, action
from src.vivi.dialogue.planner import SemanticResolver
from src.vivi.graphs.assistant import build_assistant_graph
from src.vivi.persistence.checkpoints import ThreadedSqliteSaver
from src.vivi.persistence.migration import migrate_runtime


@pytest.mark.asyncio
async def test_restart_preserves_navigation_and_does_not_restart_driving(tmp_path):
    h = Harness(tmp_path, [action("navigation.plan", {"query": "Tràng An"}), {"act": "confirm", "task_ref": "active"}])
    await h.turn("đến Tràng An")
    started = await h.turn("đồng ý")
    route = started["navigation_workspace"]["journey"]["route_id"]
    vehicle, maps, model = h.vehicle, h.maps, h.model
    await h.close()
    resumed = Harness(tmp_path, vehicle=vehicle, maps=maps, model=model)
    try:
        snapshot = await resumed.graph.snapshot("driver")
        assert snapshot["navigation_workspace"]["journey"]["route_id"] == route
        assert snapshot["navigation_workspace"]["journey"]["status"] == "paused"
        assert not vehicle.state_for("driver").driving
        assert len(model.calls) == 3  # semantic, place choice, spoken confirmation
        assert len(maps.routes) == 1
    finally:
        await resumed.close()


@pytest.mark.asyncio
async def test_crash_after_verified_start_reconciles_ledger_before_grant(tmp_path):
    h = Harness(tmp_path, [action("navigation.plan", {"query": "Tràng An"}), {"act": "confirm", "task_ref": "active"}])
    try:
        preview = await h.turn("đến Tràng An")
        original_save = h.graph.navigation.save
        fail = True

        async def interrupted(session, session_id, **kwargs):
            nonlocal fail
            if fail and session.navigation_workspace.journey:
                fail = False
                raise RuntimeError("crash after effect receipt")
            return await original_save(session, session_id, **kwargs)

        h.graph.navigation.save = interrupted
        await h.turn("đồng ý")
        version = h.vehicle.state_for("driver").state_version
        h.graph.navigation.save = original_save
        # The next entrypoint consults the effect ledger, not a consumed grant.
        result = await h.turn(action={"capability": "vehicle.get_status"})
        assert h.vehicle.state_for("driver").state_version == version
        assert (
            result["navigation_workspace"]["journey"]["route_id"]
            == preview["navigation_workspace"]["draft"]["route_id"]
        )
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_migration_is_once_backs_up_and_keeps_memory_history_and_effects(tmp_path):
    h = Harness(tmp_path, [action("navigation.plan", {"query": "Tràng An"}), {"act": "confirm", "task_ref": "active"}])
    try:
        await h.turn("đến Tràng An")
        await h.turn("đồng ý")
        with h.ledger.connect() as db:
            effects = db.execute("SELECT COUNT(*) FROM command_ledger").fetchone()[0]
            assert effects > 0
            # Simulate the pre-cutover schema marker while retaining real state rows.
            db.execute("DELETE FROM runtime_schema WHERE version=7")
        history_path = h.services.history.path
        backup = migrate_runtime(h.ledger, databases=[history_path, h.saver.path])
        assert len(backup) == 3
        assert migrate_runtime(h.ledger, databases=[history_path]) == []
        with h.ledger.connect() as db:
            assert db.execute("SELECT COUNT(*) FROM command_ledger").fetchone()[0] == effects
            assert db.execute("SELECT COUNT(*) FROM navigation_sessions").fetchone()[0] == 0
            assert not db.execute("SELECT 1 FROM confirmation_grants WHERE used=0").fetchone()
        # Cutover uses a new graph/checkpoint namespace, but the same durable effect ledger.
        await h.close()
        saver = ThreadedSqliteSaver(tmp_path / "new-checkpoints.sqlite3")
        graph = build_assistant_graph(replace(h.services), saver, h.ledger, resolver=SemanticResolver(gateway=h.models))
        try:
            snapshot = await graph.snapshot("driver")
            assert snapshot["navigation_workspace"]["draft"] is None
            assert snapshot["navigation_workspace"]["journey"] is None
            assert not h.vehicle.state_for("driver").driving
        finally:
            await graph.aclose()
    finally:
        if not h.graph._closing:
            await h.close()


@pytest.mark.asyncio
async def test_telemetry_changes_journey_without_changing_draft_revision_or_checkpoint(tmp_path):
    h = Harness(
        tmp_path,
        [
            action("navigation.plan", {"query": "Tràng An"}),
            {"act": "confirm", "task_ref": "active"},
            action("navigation.plan", {"query": "Tam Cốc"}),
        ],
    )
    try:
        await h.turn("đến Tràng An")
        await h.turn("đồng ý")
        preview = await h.turn("đến Tam Cốc")
        draft = preview["navigation_workspace"]["draft"]
        config = h.graph.config({"session_id": "driver"})
        checkpoint = (await h.graph.compiled.aget_state(config)).config
        result = await h.turn(
            navigation_event={
                "kind": "progress",
                "id": "tick",
                "revision": preview["navigation_workspace"]["revision"],
                "distance_m": 100,
            }
        )
        assert result["navigation_workspace"]["draft"] == draft
        assert result["navigation_workspace"]["journey"]["progress_m"] == 100
        assert (await h.graph.compiled.aget_state(config)).config == checkpoint
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_explicit_memory_survives_restart_without_replaying_actions(tmp_path):
    h = Harness(tmp_path)
    await h.turn(
        action={"capability": "memory.remember", "arguments": {"memory_key": "display_name", "memory_value": "Nam"}}
    )
    vehicle = h.vehicle
    version = vehicle.state_for("driver").state_version
    await h.close()
    resumed = Harness(tmp_path, vehicle=vehicle)
    try:
        recalled = await resumed.turn(
            turn_id="recall-after-restart",
            action={"capability": "memory.recall", "arguments": {"memory_key": "display_name"}},
        )
        assert "Nam" in recalled["output"]["response_text"]
        assert vehicle.state_for("driver").state_version == version
        assert not resumed.model.calls
        await resumed.turn(action={"capability": "memory.forget", "arguments": {"memory_key": "display_name"}})
        assert resumed.services.memory.list("default") == []
    finally:
        await resumed.close()
