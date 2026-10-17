import pytest

from tests.support.core import Harness, action


@pytest.mark.asyncio
async def test_navigate_waits_for_start_before_later_steps(tmp_path):
    plan = action("navigation.plan", {"query": "Tràng An", "purpose": "navigate"})
    plan["actions"].append({"capability": "media.play"})
    harness = Harness(tmp_path, [plan, {"act": "confirm", "task_ref": "active"}])
    try:
        preview = await harness.turn("dẫn đường đến Tràng An rồi bật nhạc")
        assert preview["navigation_workspace"]["draft"]["route"]
        assert not harness.vehicle.state_for("driver").driving
        assert not harness.vehicle.state_for("driver").media_playing
        assert preview["pending_question"]["purpose"] == "approve_execution"
        done = await harness.turn("đồng ý")
        assert harness.vehicle.state_for("driver").driving
        assert harness.vehicle.state_for("driver").media_playing
        assert all(step["status"] == "completed" for step in done["steps"])
        assert done["pending_question"] is None
        assert not (await harness.graph.compiled.aget_state(harness.graph.config({"session_id": "driver"}))).next
    finally:
        await harness.close()


@pytest.mark.asyncio
async def test_itinerary_finishes_calculation_then_runs_music(tmp_path):
    plan = action("navigation.plan", {"query": "Tràng An", "purpose": "itinerary"})
    plan["actions"].append({"capability": "media.play"})
    harness = Harness(tmp_path, [plan])
    try:
        result = await harness.turn("lập kế hoạch tới Tràng An rồi bật nhạc")
        assert result["navigation_workspace"]["draft"]["purpose"] == "itinerary"
        assert not harness.vehicle.state_for("driver").driving
        assert harness.vehicle.state_for("driver").media_playing
    finally:
        await harness.close()


@pytest.mark.asyncio
async def test_partial_slot_answer_preserves_completed_and_later_steps(tmp_path):
    plan = action("media.play")
    plan["actions"].append({"capability": "window.set_position", "arguments": {"position_percent": 50}})
    h = Harness(tmp_path, [plan])
    try:
        first = await h.turn("bật nhạc rồi mở kính một nửa")
        assert first["pending_question"]["slot"] == "zone"
        task_id = first["session"]["active"]["id"]
        patched = await h.turn(question_reply={"question_id": first["pending_question"]["id"], "value": "driver"})
        assert patched["session"]["active"]["id"] == task_id
        assert patched["steps"][0]["status"] == "completed"
        assert patched["steps"][1]["status"] == "waiting_confirmation"
        done = await h.turn(question_reply={"question_id": patched["pending_question"]["id"], "option_id": "yes"})
        assert h.vehicle.state_for("driver").window_positions.driver == 50
        assert all(s["status"] == "completed" for s in done["steps"])
        with h.ledger.connect() as db:
            assert db.execute("SELECT COUNT(*) FROM command_ledger").fetchone()[0] == 2
    finally:
        await h.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("flag", ["negated", "conditional", "scheduled", "conflicting"])
async def test_unresolved_or_conditional_compound_never_executes_prefix(tmp_path, flag):
    value = action("media.play", **{flag: True})
    value["actions"].append(
        {"capability": "window.set_position", "arguments": {"zone": "driver", "position_percent": 50}}
    )
    h = Harness(tmp_path, [value])
    try:
        result = await h.turn("yêu cầu nhiều bước chưa đủ điều kiện")
        assert result["status"] == "clarify"
        assert h.vehicle.state_for("driver").state_version == 0
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_suspending_and_resuming_navigation_requires_new_consent(tmp_path):
    h = Harness(
        tmp_path,
        [
            action("navigation.plan", {"query": "Tràng An", "purpose": "navigate"}),
            {"act": "suspend", "task_ref": "active"},
            {"act": "resume", "task_ref": "suspended:0"},
        ],
    )
    try:
        await h.turn("đến Tràng An")
        await h.turn("tạm giữ yêu cầu")
        resumed = await h.turn("tiếp tục yêu cầu vừa rồi")
        assert resumed["pending_question"]["purpose"] == "approve_execution"
        assert not h.vehicle.state_for("driver").driving
    finally:
        await h.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["đi Tràng An", "dẫn đường đến Tràng An"])
async def test_equivalent_directions_call_same_semantic_and_place_stages(tmp_path, text):
    h = Harness(tmp_path, [action("navigation.plan", {"query": "Tràng An", "purpose": "navigate"})])
    try:
        result = await h.turn(text)
        assert [call["schema_name"] for call in h.model.calls] == ["vivi_semantic_turn", "destination_choice"]
        assert result["navigation_workspace"]["draft"]["selected"]["name"] == "Tràng An"
        assert result["output"]["diagnostics"]["semantic_source"] == "llm"
        assert not h.vehicle.state_for("driver").driving
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_direct_start_action_cannot_replace_bound_question_answer(tmp_path):
    h = Harness(tmp_path, [action("navigation.plan", {"query": "Tràng An", "purpose": "navigate"})])
    try:
        preview = await h.turn("đến Tràng An")
        draft = preview["navigation_workspace"]["draft"]
        direct = await h.turn(
            action={
                "capability": "navigation.start",
                "arguments": {"plan_id": draft["id"], "route_revision": draft["revision"]},
            }
        )
        assert not h.vehicle.state_for("driver").driving
        assert direct["pending_question"]["purpose"] == "approve_execution"
        confirmed = await h.turn(question_reply={"question_id": direct["pending_question"]["id"], "option_id": "yes"})
        assert confirmed["navigation_workspace"]["journey"]["status"] == "running"
        assert h.vehicle.state_for("driver").driving
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_append_step_revokes_start_consent_instead_of_running_pending_start(tmp_path):
    h = Harness(
        tmp_path,
        [
            action("navigation.plan", {"query": "Tràng An", "purpose": "navigate"}),
            {"act": "modify", "task_ref": "active", "append_actions": [{"capability": "media.play"}]},
        ],
    )
    try:
        preview = await h.turn("đến Tràng An")
        changed = await h.turn("thêm bước bật nhạc")
        assert not h.vehicle.state_for("driver").driving
        assert not h.vehicle.state_for("driver").media_playing
        assert changed["pending_question"]["purpose"] == "approve_execution"
        assert changed["pending_question"]["id"] != preview["pending_question"]["id"]
        await h.turn(question_reply={"question_id": changed["pending_question"]["id"], "option_id": "yes"})
        assert h.vehicle.state_for("driver").driving
        assert h.vehicle.state_for("driver").media_playing
    finally:
        await h.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("input_kind", ["spoken", "typed"])
async def test_information_detour_preserves_navigation_and_following_music(tmp_path, input_kind):
    plan = action("navigation.plan", {"query": "Tràng An", "purpose": "navigate"})
    plan["actions"].append({"capability": "media.play"})
    replies = [plan]
    if input_kind == "spoken":
        replies.append(
            {"act": "question", "actions": [{"capability": "manual.search", "arguments": {"query": "áp suất lốp"}}]}
        )
    replies.append({"act": "resume", "task_ref": "suspended:0"})
    h = Harness(tmp_path, replies)
    try:
        preview = await h.turn("đến Tràng An rồi bật nhạc")
        task_id = preview["session"]["active"]["id"]
        if input_kind == "spoken":
            detour = await h.turn("áp suất lốp bao nhiêu")
        else:
            detour = await h.turn(action={"capability": "manual.search", "arguments": {"query": "áp suất lốp"}})
        assert detour["session"]["suspended"][0]["id"] == task_id
        assert not h.vehicle.state_for("driver").driving
        resumed = await h.turn("tiếp tục yêu cầu trước")
        assert resumed["session"]["active"]["id"] == task_id
        assert resumed["pending_question"]["purpose"] == "approve_execution"
        assert not h.vehicle.state_for("driver").media_playing
        await h.turn(question_reply={"question_id": resumed["pending_question"]["id"], "option_id": "yes"})
        assert h.vehicle.state_for("driver").driving
        assert h.vehicle.state_for("driver").media_playing
    finally:
        await h.close()
