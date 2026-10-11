"""Latest logs, defaults and model-owned task patches under the same policy."""

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from src.vivi.dialogue.contracts import ActionDraft, DialogueTask, SemanticTurn, SessionState, TaskStep
from src.vivi.dialogue.parser import OfflineParser
from src.vivi.dialogue.reducer import reduce_turn
from tests.test_dialogue_v2 import ScriptedModel, build, invoke
from tests.test_long_term_memory import services as services


@pytest.mark.parametrize("tier", ["rules", "google", "local"])
@pytest.mark.asyncio
async def test_logs_910_924_with_recorded_wrong_model_outputs(services, tmp_path, tier):
    rows = [
        json.loads(line) for line in (Path(__file__).parents[1] / "tests/fixtures/dialogue_audits/audit_910_924.jsonl").read_text().splitlines()
    ]
    model = ScriptedModel([row["recorded_semantic"] for row in rows])
    graph, _, gateway = build(
        services, tmp_path, model if tier == "google" else None, local=model if tier == "local" else None
    )
    graph.default_provider = tier
    for row in rows:
        result = await invoke(graph, row["query"], str(row["row"]))
        assert result["diagnostics"]["effective_provider"] == tier
        if 911 <= row["row"] <= 914:
            assert result["status"] == "confirmation_required"
            targets = result["action_proposal"]["arguments"]
            if row["row"] == 911:
                assert targets["zone"] == "all"
            elif row["row"] == 912:
                assert targets["zones"] == ["front_passenger", "rear_left", "rear_right"]
            else:
                assert targets["zones"] == ["rear_left", "rear_right"]
            assert gateway.vehicle.state_for("driver").state_version == 0
        if row["row"] >= 915:
            expected = {915: 25, 916: 27, 917: 30, 918: 16, 919: 16, 920: 16, 921: 18, 922: 16, 923: 18, 924: 20}[
                row["row"]
            ]
            assert gateway.vehicle.state_for("driver").temperature_celsius == expected
            assert all(step["status"] == "completed" for step in result["steps"])
            if row["row"] >= 923:
                assert len(result["steps"]) == 2 and gateway.vehicle.state_for("driver").media_playing
    graph.close()


@pytest.mark.parametrize(
    "query,expected",
    [
        ("tăng điều hoà", 25),
        ("giảm điều hoà", 21),
        ("tăng nhiệt độ lên 2 độ", 25),
        ("giảm điều hoà tối đa", 16),
        ("tăng điều hoà tối đa", 30),
        ("đặt điều hoà tối đa", 30),
        ("vẫn nóng lắm", 21),
    ],
)
@pytest.mark.asyncio
async def test_climate_defaults_limits_and_explicit_amounts(services, tmp_path, query, expected):
    graph, _, gateway = build(services, tmp_path)
    result = await invoke(graph, query, "one")
    assert result["execution"]["verified"] and gateway.vehicle.state_for("driver").temperature_celsius == expected
    assert str(expected) in result["response_text"]
    graph.close()


@pytest.mark.asyncio
async def test_fill_slot_and_append_task_without_replacing_ids(services, tmp_path):
    graph, _, gateway = build(services, tmp_path)
    first = await invoke(graph, "đặt nhiệt độ", "one")
    assert first["status"] == "clarify"
    filled = await invoke(graph, "tăng nhiệt độ lên 2 độ và bật nhạc", "two")
    assert filled["session"]["active"]["id"] == first["session"]["active"]["id"]
    assert filled["steps"][0]["id"] == first["steps"][0]["id"]
    assert len(filled["steps"]) == 2 and all(s["status"] == "completed" for s in filled["steps"])
    assert gateway.vehicle.state_for("driver").temperature_celsius == 25
    replay = await invoke(graph, "tăng nhiệt độ lên 2 độ và bật nhạc", "two")
    assert replay["steps"] == filled["steps"] and gateway.vehicle.state_for("driver").state_version == 2
    graph.close()


@pytest.mark.asyncio
async def test_model_can_patch_later_steps_and_append_without_parser_overwrite(services, tmp_path):
    model = ScriptedModel(
        [
            {
                "act": "request",
                "actions": [
                    {"capability": "window.set_position", "arguments": {"zone": "driver", "position_percent": 100}},
                    {"capability": "climate.set_temperature", "arguments": {"value_celsius": 24}},
                    {"capability": "media.play"},
                ],
            },
            {
                "act": "modify",
                "task_ref": "active",
                "step_updates": [
                    {
                        "step_index": 1,
                        "action": {"capability": "climate.set_temperature", "arguments": {"value_celsius": 26}},
                    },
                    {"step_index": 2, "action": {"capability": "media.play", "arguments": {"media_query": "jazz"}}},
                ],
                "append_actions": [{"capability": "media.pause"}],
            },
            {"act": "confirm", "task_ref": "active"},
        ]
    )
    graph, _, gateway = build(services, tmp_path, model=model)
    first = await invoke(graph, "hé kính tài rồi chỉnh 24 độ và mở nhạc", "one")
    edited = await invoke(graph, "đổi nhiệt độ trong chuỗi sang 26, chọn jazz rồi cuối cùng tắt nhạc", "two")
    assert len(edited["steps"]) == 4 and edited["status"] == "confirmation_required"
    assert not edited["diagnostics"]["provider_attempts"][0].get("semantic_guard")
    assert gateway.vehicle.state_for("driver").state_version == 0
    assert edited["confirmation"]["confirmation_id"] != first["confirmation"]["confirmation_id"]
    final = await invoke(graph, "đồng ý", "three")
    assert all(s["status"] == "completed" for s in final["steps"])
    assert gateway.vehicle.state_for("driver").temperature_celsius == 26
    assert not gateway.vehicle.state_for("driver").media_playing
    assert gateway.vehicle.state_for("driver").state_version == 4
    graph.close()


def test_invalid_patch_does_not_mutate_any_slot_and_revokes_consent():
    state = SessionState(
        active=DialogueTask(
            steps=[
                TaskStep(
                    draft=ActionDraft(capability="climate.set_temperature", arguments={"value_celsius": 24}),
                    status="completed",
                ),
                TaskStep(
                    draft=ActionDraft(
                        capability="window.set_position", arguments={"zone": "driver", "position_percent": 100}
                    ),
                    status="waiting_confirmation",
                    confirmation={"id": "old"},
                ),
            ]
        )
    )
    event = SemanticTurn(
        act="modify",
        task_ref="active",
        step_updates=[
            {"step_index": 1, "action": {"capability": "window.set_position", "arguments": {"position_percent": 50}}},
            {"step_index": 0, "action": {"capability": "climate.set_temperature", "arguments": {"value_celsius": 26}}},
        ],
    )
    result, message = reduce_turn(state, event)
    assert message and result.active.steps[1].draft.arguments.position_percent == 100
    assert result.active.steps[0].status == "completed" and not result.active.steps[1].confirmation
    assert result.active.revision > state.active.revision


def test_patch_duplicates_and_more_than_five_steps_are_rejected():
    with pytest.raises(ValidationError):
        SemanticTurn(
            act="modify",
            task_ref="active",
            step_updates=[
                {"step_index": 0, "action": {"capability": "media.play"}},
                {"step_index": 0, "action": {"capability": "media.pause"}},
            ],
        )
    state = SessionState(
        active=DialogueTask(steps=[TaskStep(draft=ActionDraft(capability="media.play")) for _ in range(5)])
    )
    result, message = reduce_turn(
        state, SemanticTurn(act="modify", task_ref="active", append_actions=[ActionDraft(capability="media.pause")])
    )
    assert message and len(result.active.steps) == 5


def test_default_does_not_guess_missing_absolute_temperature():
    frame = OfflineParser().interpret("đặt nhiệt độ", SessionState())
    assert frame.actions[0].arguments.value_celsius is None
    assert frame.actions[0].arguments.temperature_delta_celsius is None


@pytest.mark.asyncio
async def test_unknown_correction_blocks_old_consent_on_parser_fallback(services, tmp_path):
    graph, _, gateway = build(services, tmp_path)
    first = await invoke(graph, "mở kính bên tài và đặt nhiệt độ 24 độ", "one")
    unclear = await invoke(graph, "bước phía sau đổi theo mức XYZ", "two")
    assert unclear["status"] == "clarify"
    denied = gateway.resolve_confirmation("driver", "old", first["confirmation"]["confirmation_id"], "approve")[1]
    assert not denied.allowed
    after = await invoke(graph, "đồng ý", "three")
    assert after["status"] == "clarify" and gateway.vehicle.state_for("driver").state_version == 0
    graph.close()


@pytest.mark.asyncio
async def test_offline_future_patch_and_append_chain(services, tmp_path):
    graph, _, gateway = build(services, tmp_path)
    await invoke(graph, "mở kính bên tài rồi đặt nhiệt độ 24 độ và bật nhạc", "one")
    edited = await invoke(graph, "bước nhiệt độ đổi sang 26 độ và thêm tắt nhạc ở cuối chuỗi", "two")
    assert edited["status"] == "confirmation_required" and len(edited["steps"]) == 4
    assert gateway.vehicle.state_for("driver").state_version == 0
    final = await invoke(graph, "đồng ý", "three")
    assert gateway.vehicle.state_for("driver").temperature_celsius == 26
    assert not gateway.vehicle.state_for("driver").media_playing
    assert all(s["status"] == "completed" for s in final["steps"])
    graph.close()


def test_model_context_supplies_exclusions_and_editable_step_handles():
    from src.vivi.dialogue.planner import SemanticResolver

    state = SessionState(
        active=DialogueTask(
            steps=[
                TaskStep(draft=ActionDraft(capability="media.play"), status="completed"),
                TaskStep(
                    draft=ActionDraft(
                        capability="window.set_position",
                        arguments={"zone": "all", "position_percent": 100},
                        excluded_zones=["driver"],
                    ),
                    status="waiting_confirmation",
                ),
            ]
        )
    )
    context = SemanticResolver.context(state)["active"]
    assert context["editable_steps"] == [1] and context["current_step"] == 1
    assert context["steps"][1]["step_handle"] == "active:step:1"
    assert context["steps"][1]["excluded_zones"] == ["driver"]


def test_explicit_replacement_of_exclusions_requires_replace_mode():
    state = SessionState(
        active=DialogueTask(
            steps=[
                TaskStep(
                    draft=ActionDraft(
                        capability="window.set_position",
                        arguments={"zone": "all", "position_percent": 100},
                        excluded_zones=["driver"],
                    )
                )
            ]
        )
    )
    result, message = reduce_turn(
        state,
        SemanticTurn(
            act="modify",
            task_ref="active",
            step_updates=[
                {
                    "step_index": 0,
                    "action": {
                        "capability": "window.set_position",
                        "excluded_zones": ["front_passenger"],
                        "exclusion_mode": "replace",
                    },
                },
            ],
        ),
    )
    assert message is None and result.active.steps[0].draft.excluded_zones == ["front_passenger"]


@pytest.mark.asyncio
async def test_each_relative_step_reads_the_updated_vehicle_state(services, tmp_path):
    graph, _, gateway = build(services, tmp_path)
    result = await invoke(graph, "tăng điều hoà 2 độ rồi tăng điều hoà 3 độ rồi bật nhạc", "one")
    assert [s["result"]["action"]["arguments"].get("value_celsius") for s in result["steps"][:2]] == [25, 28]
    assert gateway.vehicle.state_for("driver").temperature_celsius == 28
    assert gateway.vehicle.state_for("driver").state_version == 3
    graph.close()


def test_guard_preserves_model_target_for_ambiguous_target_wording():
    model = SemanticTurn(
        act="request", actions=[ActionDraft(capability="climate.set_temperature", arguments={"value_celsius": 24})]
    )
    parser = OfflineParser()
    assert parser.interpret("tăng điều hoà lên 24 độ", SessionState()).actions[0].arguments.value_celsius == 24
    assert parser.guard("tăng điều hoà lên 24 độ", model, SessionState()) == model
    model.actions[0].arguments.value_celsius = None
    model.actions[0].arguments.temperature_delta_celsius = 24
    assert parser.guard("tăng điều hoà lên 24 độ", model, SessionState()) == model


def test_explicit_model_patch_cannot_turn_exclusion_into_positive_target():
    state = SessionState(
        active=DialogueTask(
            steps=[
                TaskStep(
                    draft=ActionDraft(
                        capability="window.set_position",
                        arguments={"zone": "all", "position_percent": 100},
                        excluded_zones=["driver", "front_passenger"],
                    ),
                    status="waiting_confirmation",
                )
            ]
        )
    )
    model = SemanticTurn(
        act="modify",
        task_ref="active",
        step_updates=[
            {
                "step_index": 0,
                "action": {
                    "capability": "window.set_position",
                    "arguments": {"zones": ["driver"], "position_percent": 100},
                },
            }
        ],
    )
    guarded = OfflineParser().guard("ngoại trừ tiếp cả cửa sổ bên tài", model, state)
    assert guarded.step_updates[0].step_index == 0
    assert guarded.step_updates[0].action.arguments.model_dump(exclude_none=True) == {}
    updated, message = reduce_turn(state, guarded)
    assert message is None
    from src.vivi.capabilities.registry import REGISTRY

    args, error = REGISTRY["window.set_position"].validate(updated.active.steps[0].draft)
    assert error is None and args["zones"] == ["rear_left", "rear_right"]


@pytest.mark.asyncio
async def test_patch_and_append_after_partial_execution_survive_restart(services, tmp_path):
    model = ScriptedModel(
        [
            {
                "act": "request",
                "actions": [
                    {"capability": "climate.set_temperature", "arguments": {"temperature_delta_celsius": 2}},
                    {"capability": "door.set_open", "arguments": {"zone": "driver", "open": True}},
                    {"capability": "media.play"},
                ],
            },
            {
                "act": "modify",
                "task_ref": "active",
                "step_updates": [
                    {"step_index": 2, "action": {"capability": "media.play", "arguments": {"media_query": "jazz"}}},
                ],
                "append_actions": [{"capability": "media.pause"}],
            },
        ]
    )
    graph, store, gateway = build(services, tmp_path, model=model)
    first = await invoke(graph, "tăng điều hoà và mở cửa bên tài rồi phát nhạc", "one")
    assert first["steps"][0]["status"] == "completed" and gateway.vehicle.state_for("driver").state_version == 1
    edited = await invoke(graph, "chọn jazz cho bước nhạc và cuối cùng dừng nhạc", "two")
    assert edited["steps"][0] == first["steps"][0] and len(edited["steps"]) == 4
    assert edited["status"] == "confirmation_required" and gateway.vehicle.state_for("driver").state_version == 1
    graph.close()
    graph, _, _ = build(services, tmp_path, store=store)
    final = await invoke(graph, "đồng ý", "three")
    assert all(s["status"] == "completed" for s in final["steps"])
    assert gateway.vehicle.state_for("driver").temperature_celsius == 25
    assert gateway.vehicle.state_for("driver").state_version == 4
    assert not gateway.vehicle.state_for("driver").media_playing
    await invoke(graph, "đồng ý", "three")
    assert gateway.vehicle.state_for("driver").state_version == 4
    graph.close()
