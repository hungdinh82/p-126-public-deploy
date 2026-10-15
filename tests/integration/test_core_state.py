import asyncio
import json
from datetime import UTC, datetime, timedelta

import pytest

from src.vivi.dialogue.contracts import QuestionReply
from src.vivi.navigation.vietmap import Place
from tests.support.core import Harness, Maps, ScriptedModel, action


async def start(harness):
    await harness.turn("chỉ đường đến Tràng An")
    return await harness.turn("đồng ý")


@pytest.mark.asyncio
async def test_draft_replacement_does_not_replace_running_journey(tmp_path):
    h = Harness(
        tmp_path,
        [
            action("navigation.plan", {"query": "Tràng An"}),
            {"act": "confirm", "task_ref": "active"},
            action("navigation.plan", {"query": "Tam Cốc"}),
        ],
    )
    try:
        running = await start(h)
        old = running["navigation_workspace"]["journey"]
        result = await h.turn("dẫn đường đến Tam Cốc")
        workspace = result["navigation_workspace"]
        assert workspace["journey"]["route_id"] == old["route_id"]
        assert workspace["draft"]["selected"]["name"] == "Tam Cốc"
        assert workspace["draft"]["route_id"] != old["route_id"]
        assert h.vehicle.state_for("driver").driving
        with h.ledger.connect() as db:
            stored = json.loads(db.execute("SELECT payload FROM navigation_sessions").fetchone()[0])
            assert "trip_plan" not in stored and "navigation" not in stored
            assert "route" not in stored["navigation_workspace"]["journey"]
            assert stored["navigation_workspace"]["journey"]["calculation_reference"] == old["route_id"]
        checkpoint = await h.graph.compiled.aget_state(h.graph.config({"session_id": "driver"}))
        assert "navigation_workspace" not in checkpoint.values["session"]
        assert '"coordinates":' not in json.dumps(checkpoint.values["session"])
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_typed_question_reply_never_calls_model_and_rejects_reuse(tmp_path):
    h = Harness(tmp_path, [action("window.set_position", {"zone": "driver", "position_percent": 50})])
    try:
        quote = await h.turn("mở kính bên tài một nửa")
        answer = QuestionReply(question_id=quote["pending_question"]["id"], option_id="yes").model_dump()
        done = await h.turn(question_reply=answer)
        version = h.vehicle.state_for("driver").state_version
        assert h.vehicle.state_for("driver").window_positions.driver == 50
        assert done["output"]["diagnostics"]["semantic_source"] == "typed_event"
        assert len(h.model.calls) == 1
        stale = await h.turn(question_reply=answer)
        assert stale["status"] == "clarify"
        assert h.vehicle.state_for("driver").state_version == version
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_fallback_does_not_parse_a_compound_or_replace_pending_task(tmp_path):
    h = Harness(
        tmp_path, [action("window.set_position", {"zone": "driver", "position_percent": 50}), RuntimeError("offline")]
    )
    try:
        quote = await h.turn("mở kính bên tài 50%")
        result = await h.turn("đồng ý rồi mở tất cả cửa")
        assert result["session"]["active"]["id"] == quote["session"]["active"]["id"]
        assert result["pending_question"]["id"] == quote["pending_question"]["id"]
        assert h.vehicle.state_for("driver").window_positions.driver == 0
        assert not any(d.open for _, d in h.vehicle.state_for("driver").door_states)
    finally:
        await h.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("answer", ["đồng ý", "đúng", "ĐÚNG"])
async def test_fallback_yes_uses_only_current_question(tmp_path, answer):
    h = Harness(
        tmp_path, [action("window.set_position", {"zone": "driver", "position_percent": 50}), RuntimeError("offline")]
    )
    try:
        await h.turn("mở kính bên tài 50%")
        done = await h.turn(answer)
        assert h.vehicle.state_for("driver").window_positions.driver == 50
        assert done["output"]["diagnostics"]["semantic_source"] == "fallback"
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_expired_question_cannot_approve(tmp_path):
    h = Harness(tmp_path, [action("window.set_position", {"zone": "driver", "position_percent": 50})])
    try:
        quote = await h.turn("mở kính bên tài 50%")
        config = h.graph.config({"session_id": "driver"})
        state = await h.graph.compiled.aget_state(config)
        session = dict(state.values["session"])
        session["dialogue_question"]["expires_at"] = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
        await h.graph.compiled.aupdate_state(config, {"session": session}, as_node="persist")
        result = await h.turn(question_reply={"question_id": quote["pending_question"]["id"], "option_id": "yes"})
        assert result["status"] == "clarify"
        assert h.vehicle.state_for("driver").window_positions.driver == 0
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_origin_or_battery_change_requires_new_consent(tmp_path):
    h = Harness(tmp_path, [action("navigation.plan", {"query": "Tràng An"}), {"act": "confirm", "task_ref": "active"}])
    try:
        quote = await h.turn("đến Tràng An")
        revision = quote["navigation_workspace"]["draft"]["revision"]
        h.vehicle.set_energy("driver", 70, 200)
        changed = await h.turn("đồng ý")
        assert not h.vehicle.state_for("driver").driving
        assert changed["navigation_workspace"]["draft"]["revision"] > revision
        assert changed["pending_question"]["purpose"] == "approve_execution"
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_map_failure_preserves_active_journey_and_has_dependency_error(tmp_path):
    from src.vivi.navigation.vietmap import VietMapError

    h = Harness(
        tmp_path,
        [
            action("navigation.plan", {"query": "Tràng An"}),
            {"act": "confirm", "task_ref": "active"},
            action("navigation.plan", {"query": "Tam Cốc"}),
        ],
    )
    try:
        initial = await start(h)

        async def fail(*_, **__):
            raise VietMapError("unavailable")

        h.maps.search = fail
        result = await h.turn("đến Tam Cốc")
        assert (
            result["navigation_workspace"]["journey"]["route_id"]
            == initial["navigation_workspace"]["journey"]["route_id"]
        )
        assert h.vehicle.state_for("driver").driving
        assert result["status"] in {"blocked", "clarify"}
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_parking_is_not_blocked_by_slow_model(tmp_path):
    entered, release = asyncio.Event(), asyncio.Event()
    h = Harness(tmp_path, [action("navigation.plan", {"query": "Tràng An"}), {"act": "confirm", "task_ref": "active"}])
    try:
        await start(h)
        original = h.model.agenerate_json

        async def slow(**kwargs):
            entered.set()
            await release.wait()
            return json.dumps({"act": "reply", "text": "Xin chào"})

        h.model.agenerate_json = slow
        pending = asyncio.create_task(h.turn("xin chào"))
        await asyncio.wait_for(entered.wait(), 1)
        stopped = await asyncio.wait_for(
            h.turn(navigation_event={"id": "park", "kind": "gear", "gear": "P", "revision": 0}), 0.5
        )
        assert stopped["receipt"]["status"] == "completed"
        assert not h.vehicle.state_for("driver").driving
        release.set()
        await pending
        h.model.agenerate_json = original
    finally:
        release.set()
        await h.close()


@pytest.mark.asyncio
async def test_event_replay_and_payload_collision(tmp_path):
    h = Harness(tmp_path)
    try:
        event = {"id": "same", "kind": "gear", "gear": "D", "revision": 0}
        await h.turn(navigation_event=event)
        version = h.vehicle.state_for("driver").state_version
        await h.turn(navigation_event=event)
        assert h.vehicle.state_for("driver").state_version == version
        with pytest.raises(ValueError, match="event_id"):
            await h.turn(navigation_event={**event, "gear": "P"})
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_unknown_effect_is_not_resent(tmp_path):
    h = Harness(tmp_path)
    try:
        key = h.graph.navigation.key("driver")
        event = {"id": "unknown", "kind": "gear", "gear": "D", "revision": 0}
        # Simulate a crash after durable reservation, before a final receipt.
        from src.vivi.navigation.workflow import NavigationEvent

        recorded = NavigationEvent.model_validate(event).model_dump(mode="json")
        h.graph.navigation.repository.reserve(key, recorded)
        result = await h.turn(navigation_event=event)
        assert result["receipt"]["status"] == "unknown"
        assert not h.vehicle.state_for("driver").driving
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_candidate_ordinal_and_single_yes_do_not_start(tmp_path):
    class Multiple(Maps):
        async def search(self, text, **kwargs):
            return [
                Place(ref_id="a", name=text + " cơ sở A", address="Ninh Bình", lat=20.25, lng=105.92),
                Place(ref_id="b", name=text + " cơ sở B", address="Ninh Bình", lat=20.26, lng=105.93),
            ]

    model = ScriptedModel(
        [action("navigation.plan", {"query": "Tràng An"}), RuntimeError("offline")],
        destination={"mode": "clarify", "handle": None, "reason": "ambiguous"},
    )
    h = Harness(tmp_path, model=model, maps=Multiple())
    try:
        selecting = await h.turn("đến Tràng An")
        assert len(selecting["pending_question"]["choices"]) == 2
        selected = await h.turn("chỗ thứ 2")
        assert selected["navigation_workspace"]["draft"]["selected"]["ref_id"] == "b"
        assert not h.vehicle.state_for("driver").driving
        assert selected["pending_question"]["purpose"] == "approve_execution"
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_parking_fences_result_from_slow_map_read(tmp_path):
    entered, release = asyncio.Event(), asyncio.Event()
    h = Harness(
        tmp_path,
        [
            action("navigation.plan", {"query": "Tràng An"}),
            {"act": "confirm", "task_ref": "active"},
            action("navigation.plan", {"query": "Tam Cốc"}),
        ],
    )
    pending = None
    try:
        initial = await start(h)
        original = h.maps.search

        async def slow(*args, **kwargs):
            entered.set()
            await release.wait()
            return await original(*args, **kwargs)

        h.maps.search = slow
        pending = asyncio.create_task(h.turn("đến Tam Cốc"))
        await asyncio.wait_for(entered.wait(), 1)
        parked = await asyncio.wait_for(
            h.turn(navigation_event={"id": "priority-P", "kind": "gear", "gear": "P", "revision": 0}), 0.5
        )
        assert parked["receipt"]["status"] == "completed"
        assert not h.vehicle.state_for("driver").driving
        release.set()
        await pending
        snapshot = await h.graph.snapshot("driver")
        assert (
            snapshot["navigation_workspace"]["journey"]["route_id"]
            == initial["navigation_workspace"]["journey"]["route_id"]
        )
        assert snapshot["navigation_workspace"]["journey"]["status"] == "paused"
        assert snapshot["navigation_workspace"]["draft"]["id"] == initial["navigation_workspace"]["draft"]["id"]
        assert snapshot["navigation_workspace"]["draft"]["selected"]["name"] == "Tràng An"
    finally:
        release.set()
        if pending:
            await asyncio.gather(pending, return_exceptions=True)
        await h.close()


@pytest.mark.asyncio
async def test_missing_poi_area_reply_keeps_name_and_plan_binding(tmp_path):
    class Missing(Maps):
        async def search(self, text, **kwargs):
            self.queries.append(text)
            return []

    h = Harness(tmp_path, [action("navigation.plan", {"query": "Quán Sao Mai"})], maps=Missing())
    try:
        first = await h.turn("đến Quán Sao Mai")
        question = first["pending_question"]
        assert question["slot"] == "area"
        draft_id = first["navigation_workspace"]["draft"]["id"]
        result = await h.turn(question_reply={"question_id": question["id"], "value": "Ninh Bình"})
        draft = result["navigation_workspace"]["draft"]
        assert draft["id"] == draft_id
        assert draft["destination"]["name"] == "Quán Sao Mai"
        assert draft["destination"]["area"]["text"] == "Ninh Bình"
        assert any("Quán Sao Mai" in q and "Ninh Bình" in q for q in h.maps.queries)
        assert not h.vehicle.state_for("driver").driving
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_deny_new_draft_does_not_stop_existing_journey(tmp_path):
    h = Harness(
        tmp_path,
        [
            action("navigation.plan", {"query": "Tràng An"}),
            {"act": "confirm", "task_ref": "active"},
            action("navigation.plan", {"query": "Tam Cốc"}),
        ],
    )
    try:
        first = await start(h)
        preview = await h.turn("đến Tam Cốc")
        result = await h.turn(question_reply={"question_id": preview["pending_question"]["id"], "option_id": "no"})
        assert (
            result["navigation_workspace"]["journey"]["route_id"]
            == first["navigation_workspace"]["journey"]["route_id"]
        )
        assert h.vehicle.state_for("driver").driving
        assert result["pending_question"] is None
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_two_tabs_cannot_drive_from_stale_control_revision(tmp_path):
    h = Harness(tmp_path)
    try:
        before = await h.graph.snapshot("driver")
        await h.turn(
            navigation_event={
                "id": "tab-a-park",
                "kind": "gear",
                "gear": "P",
                "revision": 0,
                "client_id": "a",
                "control_sequence": 2,
            }
        )
        stale = await h.turn(
            navigation_event={
                "id": "tab-b-drive",
                "kind": "gear",
                "gear": "D",
                "revision": 0,
                "client_id": "b",
                "control_sequence": 1,
                "expected_control_revision": before["control_revision"],
            }
        )
        assert stale["receipt"]["error_code"] == "stale_control"
        assert not h.vehicle.state_for("driver").driving
        delayed = await h.turn(
            navigation_event={
                "id": "tab-a-old-drive",
                "kind": "gear",
                "gear": "D",
                "revision": 0,
                "client_id": "a",
                "control_sequence": 1,
            }
        )
        assert delayed["receipt"]["error_code"] == "superseded_control"
        fresh = await h.graph.snapshot("driver")
        accepted = await h.turn(
            navigation_event={
                "id": "tab-b-new-drive",
                "kind": "gear",
                "gear": "D",
                "revision": 0,
                "client_id": "b",
                "control_sequence": 2,
                "expected_control_revision": fresh["control_revision"],
            }
        )
        assert accepted["receipt"]["status"] == "completed"
        assert h.vehicle.state_for("driver").driving
    finally:
        await h.close()
