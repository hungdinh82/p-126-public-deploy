"""Ported lifecycle invariants from 5d47f6c, plus current-runtime boundaries."""

import json
from copy import deepcopy

import httpx
import pytest
from jsonschema import Draft202012Validator

from src.vivi.capabilities.registry import catalog
from src.vivi.dialogue.contracts import ActionDraft, DialogueTask, SessionState, TaskStep
from src.vivi.dialogue.form_tools import current_form, decode_form_call
from src.vivi.dialogue.local_operations import interpret_operations, operation_prompt, prepare_context, select_documents
from src.vivi.dialogue.local_tools import local_tool_documents, proposal_tools, proposals_to_turn
from src.vivi.dialogue.planner import SemanticResolver
from src.vivi.structured_llm import StructuredChatClient, TruncatedCompletionError


def decode_proposals(calls, documents, tasks, utterance, tools):
    return proposals_to_turn(calls, documents, tasks, utterance, tools)


def prepared(status="waiting_slot", capability="door.set_open", values=None):
    values = values or {"open": False, "target_side": "right"}
    session = SessionState(
        active=DialogueTask(
            steps=[
                TaskStep(
                    status=status,
                    draft=ActionDraft(capability=capability, arguments=values),
                )
            ]
        )
    )
    tasks = SemanticResolver.context(session)
    tasks["active"]["form_identity"] = session.active.id
    documents = local_tool_documents(catalog(True))
    return session, documents, tasks


def test_all_catalog_and_form_schemas_are_self_contained():
    _, documents, tasks = prepared()
    tools = proposal_tools(documents, tasks, "sau phải")
    for item in tools:
        Draft202012Validator.check_schema(item["function"]["parameters"])
    assert "form_submit" not in {t["function"]["name"] for t in tools}
    Draft202012Validator.check_schema(StructuredChatClient.proposal_schema(tools))


@pytest.mark.parametrize("opening", [True, False])
def test_location_update_keeps_operation_and_rejects_stale_form(opening):
    _, documents, tasks = prepared(values={"open": opening, "target_side": "right"})
    form = current_form(tasks, documents)
    payload = {k: form[k] for k in ("form_id", "revision")}
    payload["values"] = {"target_row": "rear"}
    result = decode_form_call([("form_update", payload)], documents, tasks)
    assert result.act == "modify" and result.step_updates[0].action.arguments.target_row == "rear"
    assert result.step_updates[0].action.arguments.open is None
    with pytest.raises(ValueError):
        decode_form_call([("form_update", {**payload, "revision": payload["revision"] + 1})], documents, tasks)
    with pytest.raises(ValueError):
        decode_form_call([("form_update", {**payload, "values": {"open": not opening}})], documents, tasks)


def test_submit_requires_exact_values_and_host_consent_even_if_not_advertised():
    _, documents, tasks = prepared("waiting_confirmation", values={"open": True, "zone": "rear_right"})
    form = current_form(tasks, documents)
    payload = {k: form[k] for k in ("form_id", "revision", "values")}
    with pytest.raises(ValueError):
        decode_form_call([("form_submit", payload)], documents, tasks)
    with pytest.raises(ValueError):
        decode_form_call(
            [("form_submit", {**payload, "values": {"open": True, "zone": "driver"}})],
            documents,
            tasks,
            allow_submit=True,
        )
    assert decode_form_call([("form_submit", payload)], documents, tasks, allow_submit=True).act == "confirm"


@pytest.mark.parametrize(
    ("capability", "values", "name", "arguments", "utterance"),
    [
        (
            "door.set_open",
            {"open": True, "zone": "driver"},
            "open_door",
            {"targets": ["rear_right"]},
            "mở cửa sau phải",
        ),
        (
            "door.set_open",
            {"open": False, "zone": "driver"},
            "close_door",
            {"targets": ["rear_right"]},
            "đóng cửa sau phải",
        ),
        (
            "window.set_position",
            {"position_percent": 0, "zone": "driver"},
            "raise_window",
            {"targets": ["rear_right"]},
            "nâng kính sau phải",
        ),
        (
            "window.set_position",
            {"position_percent": 100, "zone": "driver"},
            "lower_window",
            {"targets": ["rear_right"]},
            "hạ kính sau phải",
        ),
        ("climate.set_temperature", {"value_celsius": 24.0}, "set_temperature", {"celsius": 25}, "đặt điều hòa 25 độ"),
        ("media.play", {"media_query": "jazz"}, "play_music", {}, "bật nhạc"),
    ],
)
def test_pending_form_does_not_hide_an_independent_request(capability, values, name, arguments, utterance):
    _, docs, tasks = prepared(status="waiting_confirmation", capability=capability, values=values)
    result = decode_proposals([(name, arguments)], docs, tasks, utterance, proposal_tools(docs, tasks, utterance))
    assert result.act == "request"
    assert result.actions[0].capability == capability
    assert not result.step_updates
    # The old form remains a separate host-owned proposal.
    assert current_form(tasks, docs)["values"] == values


@pytest.mark.parametrize("arguments", [{}, {"celsius": None}])
def test_empty_or_null_patch_is_not_a_change(arguments):
    _, docs, tasks = prepared(
        status="waiting_confirmation", capability="climate.set_temperature", values={"value_celsius": 24.0}
    )
    with pytest.raises(ValueError):
        decode_proposals(
            [("update_current_request", arguments)], docs, tasks, "chưa biết", proposal_tools(docs, tasks, "chưa biết")
        )


@pytest.mark.parametrize("purpose", ["navigate", "itinerary"])
def test_navigation_alias_retains_bound_task_and_purpose_on_edit(purpose):
    session = SessionState(
        suspended=[
            DialogueTask(
                steps=[
                    TaskStep(
                        status="waiting_slot",
                        draft=ActionDraft(
                            capability="navigation.plan", arguments={"query": "Hà Nội", "purpose": purpose}
                        ),
                    )
                ]
            )
        ]
    )
    tasks = SemanticResolver.context(session)
    docs = local_tool_documents(catalog(True))
    text = "sửa nơi đến của yêu cầu đang giữ thành Huế"
    tools = proposal_tools(docs, tasks, text)
    result = decode_proposals([("update_suspended_0_step_0", {"query": "Huế"})], docs, tasks, text, tools)
    assert result.act == "modify" and result.task_ref == "suspended:0"
    assert result.step_updates[0].action.capability == "navigation.plan"
    assert result.step_updates[0].action.arguments.query == "Huế"
    assert result.step_updates[0].action.arguments.purpose == purpose
    assert not result.actions


@pytest.mark.parametrize(("name", "position"), [("raise_window", 0), ("lower_window", 100)])
def test_window_polarity_and_missing_location(name, position):
    docs = local_tool_documents(catalog(False))
    tools = proposal_tools(docs, {}, "lên kính")
    result = decode_proposals([(name, {"targets": []})], docs, {}, "lên kính", tools)
    assert result.actions[0].arguments.position_percent == position
    assert result.actions[0].arguments.zone is None
    schema = next(t["function"]["parameters"] for t in tools if t["function"]["name"] == name)
    assert set(schema["properties"]) == {"targets"}


def test_closed_small_schema_rejects_legacy_and_unrelated_fields():
    docs = local_tool_documents(catalog(False))
    tools = proposal_tools(docs, {}, "hạ kính")
    for field, value in [("zone", "driver"), ("open", True), ("location_text", "bên tài"), ("referenced_target", True)]:
        with pytest.raises(ValueError):
            decode_proposals([("lower_window", {"targets": [], field: value})], docs, {}, "hạ kính", tools)


@pytest.mark.parametrize(
    ("targets", "expected"),
    [
        ([], {}),
        (["left"], {"target_side": "left"}),
        (["right", "rear"], {"target_side": "right", "target_row": "rear"}),
        (["front_passenger"], {"zone": "front_passenger"}),
        (["all"], {"zone": "all"}),
        (["driver", "rear_left"], {"zones": ["driver", "rear_left"]}),
        (["same"], {"referenced_target": True}),
    ],
)
def test_one_target_representation_retains_partial_multi_and_reference_meaning(targets, expected):
    from src.vivi.dialogue.small_tools import decode_targets

    assert decode_targets(targets) == expected


@pytest.mark.parametrize(
    "targets", [["all", "driver"], ["left", "right"], ["driver", "rear"], ["same", "all"], ["driver", "driver"]]
)
def test_ambiguous_or_conflicting_target_sets_are_rejected(targets):
    from src.vivi.dialogue.small_tools import decode_targets

    with pytest.raises(ValueError):
        decode_targets(targets)


def test_compound_is_ordered_and_invalid_batch_is_atomic():
    docs = local_tool_documents(catalog(False))
    text = "đặt điều hòa 26 độ và hạ kính sau bên phải 35%"
    tools = proposal_tools(docs, {}, text)
    calls = [
        ("set_temperature", {"celsius": 26}),
        ("set_window", {"percent": 35, "targets": ["rear_right"]}),
    ]
    result = proposals_to_turn(calls, docs, {}, text, tools)
    assert [a.capability for a in result.actions] == ["climate.set_temperature", "window.set_position"]
    assert result.actions[1].arguments.zone == "rear_right"
    blocked = proposals_to_turn([*calls, ("dialogue_unsupported", {})], docs, {}, text, tools)
    assert blocked.act == "unsupported" and not blocked.actions
    with pytest.raises(ValueError):
        proposals_to_turn([*calls, ("pause_music", {"invented": True})], docs, {}, text, tools)


@pytest.mark.asyncio
@pytest.mark.parametrize("block", ["dialogue_clarify", "dialogue_unsupported"])
async def test_partial_block_cannot_be_repaired_into_partial_execution(block):
    class Model:
        tools_as_json = True
        calls = 0

        async def agenerate_proposals(self, **kwargs):
            self.calls += 1
            # A second call would lose the veto, as observed on real Qwen.
            return [("pause_music", {}), (block, {})] if self.calls == 1 else [("pause_music", {})]

    context = {"utterance": "tạm dừng nhạc và bật đèn pha", "tasks": {}, "capabilities": catalog(True)}
    model = Model()
    attempts = [{"calls": 0}]
    result = await interpret_operations(model, context, attempts)
    assert result.act == block.removeprefix("dialogue_") and not result.actions
    assert model.calls == 1
    assert attempts[0]["proposal_attempts"][0]["calls"] == [("pause_music", {}), (block, {})]


@pytest.mark.parametrize(
    ("operation", "degrees", "delta"),
    [("increase_temperature", 3, 3), ("decrease_temperature", 3, -3)],
)
def test_direction_is_operation_and_amount_is_unsigned(operation, degrees, delta):
    docs = local_tool_documents(catalog(False))
    text = "thay đổi điều hòa 3 độ"
    tools = proposal_tools(docs, {}, text)
    result = decode_proposals([(operation, {"degrees": degrees})], docs, {}, text, tools)
    assert result.actions[0].arguments.temperature_delta_celsius == delta
    assert result.actions[0].arguments.value_celsius is None


@pytest.mark.parametrize("operation", ["increase_temperature", "decrease_temperature"])
def test_amountless_relative_temperature_carries_only_the_direction(operation):
    docs = local_tool_documents(catalog(False))
    text = "thay đổi điều hòa"
    tools = proposal_tools(docs, {}, text)
    result = decode_proposals([(operation, {"degrees": None})], docs, {}, text, tools)
    direction = "increase" if operation == "increase_temperature" else "decrease"
    assert result.actions[0].arguments.temperature_direction == direction
    assert result.actions[0].arguments.temperature_delta_celsius is None
    assert result.actions[0].arguments.value_celsius is None


def test_music_only_exposes_fixed_clip_toggle():
    docs = local_tool_documents(catalog(False))
    tools = proposal_tools(docs, {}, "nhạc")
    schemas = {t["function"]["name"]: t["function"]["parameters"] for t in tools}
    assert set(schemas["play_music"]["properties"]) == set()
    assert "play_requested_music" not in schemas
    result = decode_proposals([("play_music", {})], docs, {}, "bật nhạc", tools)
    assert result.actions[0].arguments.media_query is None


def test_new_navigation_has_no_previous_revision_and_no_unused_null_fields():
    docs = local_tool_documents(catalog(True))
    tasks = {"navigation": {"draft": {"id": "old-plan", "revision": 8}}}
    tools = proposal_tools(docs, tasks, "chỉ đường tới Huế")
    result = decode_proposals([("get_directions", {"destination": "Huế"})], docs, tasks, "chỉ đường tới Huế", tools)
    assert result.actions[0].arguments.plan_id is None
    assert result.actions[0].arguments.route_revision is None
    sparse = result.model_dump(mode="json", exclude_none=True, exclude_defaults=True)
    assert sparse == {
        "act": "request",
        "actions": [
            {
                "capability": "navigation.plan",
                "arguments": {"query": "Huế", "purpose": "navigate", "trip_kind": "one_way"},
            }
        ],
    }


def test_compact_prompt_has_signatures_not_full_internal_schemas_and_only_selected_examples():
    context = {
        "utterance": "bật nhạc",
        "tasks": {},
        "capabilities": catalog(True),
        "recent_turns": [{"answer": "old assistant question"}],
    }
    before = deepcopy(context)
    view = prepare_context(context)
    _, selected, index = select_documents(view)
    docs = local_tool_documents(selected)
    tools = proposal_tools(docs, {}, view["utterance"])
    system, user = operation_prompt(view, docs, index, tools, json_transport=True)
    assert "old assistant question" not in user
    assert "additionalProperties" not in system and "excluded_zones" not in system
    assert "play_music()" in system
    from src.vivi.dialogue.local_operations import proposal_examples

    examples = proposal_examples(tools, docs, {}, json_transport=True)
    assert len(examples) <= 4
    assert context == before


def test_old_destination_cannot_change_a_vehicle_only_prompt():
    context = {"utterance": "mở cửa xe", "tasks": {}, "capabilities": catalog(True)}
    _, selected, index = select_documents(context)
    docs = local_tool_documents(selected)
    tools = proposal_tools(docs, {}, context["utterance"])
    before = operation_prompt(context, docs, index, tools, json_transport=True)
    context["conversation_context"] = {"destination": {"name": "Bảo tàng thử nghiệm", "lat": 21.0, "lng": 105.0}}
    assert operation_prompt(context, docs, index, tools, json_transport=True) == before


OBSERVATION = {"door_open": {"driver": False, "front_passenger": True, "rear_left": True, "rear_right": True}}


def door_turn(utterance, targets, observation=OBSERVATION, name="close_door"):
    docs = local_tool_documents(catalog(True))
    tools = proposal_tools(docs, {}, utterance)
    return proposals_to_turn([(name, {"targets": targets})], docs, {}, utterance, tools, observation=observation)


def test_remaining_doors_are_resolved_by_the_host_not_read_by_the_model():
    context = {
        "utterance": "đóng các cửa còn lại",
        "tasks": {},
        "capabilities": catalog(True),
        "vehicle_observation": OBSERVATION,
    }
    _, selected, index = select_documents(context)
    docs = local_tool_documents(selected)
    tools = proposal_tools(docs, {}, context["utterance"])
    system, user = operation_prompt(context, docs, index, tools, json_transport=True)
    assert '"remaining"' in system and "door_open" not in system + user
    result = door_turn("đóng 3 cửa còn lại", ["remaining"])
    assert result.actions[0].arguments.zones == ["front_passenger", "rear_left", "rear_right"]
    assert result.actions[0].arguments.open is False
    opened = door_turn("mở cửa còn lại", ["remaining"], name="open_door")
    assert opened.actions[0].arguments.zone == "driver" and opened.actions[0].arguments.open is True


@pytest.mark.parametrize(
    ("utterance", "observation", "act", "reason"),
    [
        ("đóng hai cửa còn lại", OBSERVATION, "clarify", "remaining_count_mismatch"),
        ("đóng các cửa còn lại", None, "clarify", "remaining_no_state"),
        (
            "đóng các cửa đang mở",
            {"door_open": dict.fromkeys(OBSERVATION["door_open"], False)},
            "reply",
            "remaining_none",
        ),
    ],
)
def test_remaining_doors_never_guess_without_matching_state(utterance, observation, act, reason):
    from src.vivi.dialogue.clause_guard import HostTurn

    with pytest.raises(HostTurn) as caught:
        door_turn(utterance, ["remaining"], observation)
    assert caught.value.act == act and caught.value.reason == reason
    with pytest.raises(ValueError):
        door_turn(utterance, ["remaining", "driver"], observation)


@pytest.mark.parametrize(("utterance", "offered"), [("đóng cốp giúp tôi", False), ("cảm ơn bạn nhé", True)])
def test_free_text_reply_is_offered_only_for_greetings(utterance, offered):
    tools = proposal_tools(local_tool_documents(catalog(True)), {}, utterance)
    assert ("dialogue_reply" in {t["function"]["name"] for t in tools}) is offered


class ScriptedModel:
    tools_as_json = True

    def __init__(self, *replies):
        self.replies, self.offered = list(replies), []

    async def agenerate_proposals(self, *, tools, **kwargs):
        self.offered.append([t["function"]["name"] for t in tools])
        return self.replies.pop(0)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "utterance",
    ["đừng hạ kính bên phụ", "nếu trời mưa thì nâng kính", "mười phút nữa mở cốp", "không cần mở cốp đâu"],
)
async def test_negated_conditional_or_scheduled_command_is_withheld_without_model(utterance):
    model = ScriptedModel()
    attempts = [{"calls": 0}]
    context = {"utterance": utterance, "tasks": {}, "capabilities": catalog(True)}
    result = await interpret_operations(model, context, attempts)
    assert result.act == "clarify" and not result.actions and not model.offered
    assert attempts[0]["host_veto"] in {"negated", "conditional", "scheduled"}


@pytest.mark.asyncio
async def test_veto_with_live_task_can_only_cancel_or_clarify():
    session, _, tasks = prepared(status="waiting_confirmation", values={"open": True, "zone": "driver"})
    model = ScriptedModel([("dialogue_task", {"operation": "cancel", "task_ref": "active"})])
    context = {"utterance": "thôi đừng mở cửa nữa", "tasks": tasks, "capabilities": catalog(True)}
    result = await interpret_operations(model, context, [{"calls": 0}])
    assert result.act == "cancel"
    assert set(model.offered[0]) <= {"dialogue_clarify", "dialogue_unsupported", "dialogue_task", "dialogue_answer"}


@pytest.mark.asyncio
async def test_questions_with_negative_particles_are_not_vetoed():
    model = ScriptedModel([("weather_get_forecast", {"location": "Hà Nội"})])
    context = {"utterance": "chiều nay Hà Nội có mưa không", "tasks": {}, "capabilities": catalog(True)}
    result = await interpret_operations(model, context, [{"calls": 0}])
    assert result.actions[0].capability == "weather.get_forecast"


@pytest.mark.asyncio
async def test_unsupported_clause_dropped_by_the_model_blocks_the_batch():
    model = ScriptedModel([("play_music", {})])
    context = {"utterance": "bật nhạc rồi tăng âm lượng lên", "tasks": {}, "capabilities": catalog(True)}
    attempts = [{"calls": 0}]
    result = await interpret_operations(model, context, attempts)
    assert result.act == "unsupported" and not result.actions and len(model.offered) == 1
    assert attempts[0]["proposal_attempts"][0]["validation"] == "host_turn"


@pytest.mark.asyncio
async def test_dropped_supported_clause_is_repaired_once_then_withheld():
    window = ("lower_window", {"targets": ["front_passenger"]})
    context = {"utterance": "hạ kính bên phụ rồi bật nhạc", "tasks": {}, "capabilities": catalog(True)}
    repaired = await interpret_operations(
        ScriptedModel([window], [window, ("play_music", {})]), context, [{"calls": 0}]
    )
    assert [a.capability for a in repaired.actions] == ["window.set_position", "media.play"]
    withheld = await interpret_operations(ScriptedModel([window], [window]), context, [{"calls": 0}])
    assert withheld.act == "clarify" and not withheld.actions


@pytest.mark.asyncio
async def test_targets_spanning_two_clauses_count_as_coverage():
    model = ScriptedModel([("open_door", {"targets": ["driver", "front_passenger"]})])
    context = {"utterance": "mở cửa lái và mở cửa phụ", "tasks": {}, "capabilities": catalog(True)}
    result = await interpret_operations(model, context, [{"calls": 0}])
    assert result.actions[0].arguments.zones == ["driver", "front_passenger"]


@pytest.mark.parametrize(
    ("utterance", "capability"),
    [("lịch sử chùa Một Cột là gì", "knowledge.lookup"), ("giá vàng hôm nay bao nhiêu", "web.search")],
)
def test_one_lookup_tool_and_the_information_policy_picks_the_source(utterance, capability):
    docs = local_tool_documents(catalog(True))
    tools = proposal_tools(docs, {}, utterance)
    names = {t["function"]["name"] for t in tools}
    assert "lookup_information" in names and not names & {"web_search", "knowledge_lookup"}
    result = proposals_to_turn([("lookup_information", {})], docs, {}, utterance, tools)
    assert result.actions[0].capability == capability
    assert result.actions[0].arguments.query == utterance


@pytest.mark.parametrize(
    "utterance",
    ["đóng cửa", "mở cốp và bật nhạc", "đặt điều hòa 24 độ", "cảm ơn nhé", "bật đèn trần", "chỉ đường tới Huế"],
)
def test_operation_prompt_never_supplies_canned_user_answer_pairs(utterance):
    from src.vivi.dialogue.local_operations import MAX_EXAMPLES, proposal_examples
    from src.vivi.dialogue.local_schema import LOCAL_EXAMPLES

    context = {"utterance": utterance, "tasks": {}, "capabilities": catalog(True)}
    _, selected, _ = select_documents(context)
    docs = local_tool_documents(selected)
    tools = proposal_tools(docs, {}, utterance)
    for transport in (True, False):
        assert proposal_examples(tools, docs, {}, json_transport=transport) == []
    assert MAX_EXAMPLES == 0 and LOCAL_EXAMPLES == ""


def test_information_question_keeps_destination_reference():
    context = {
        "utterance": "thời tiết ở đó thế nào",
        "tasks": {},
        "capabilities": catalog(True),
        "conversation_context": {"destination": {"name": "Bảo tàng thử nghiệm", "lat": 21.0, "lng": 105.0}},
    }
    _, selected, index = select_documents(context)
    docs = local_tool_documents(selected)
    tools = proposal_tools(docs, {}, context["utterance"])
    _, user = operation_prompt(context, docs, index, tools, json_transport=True)
    assert "Bảo tàng thử nghiệm" in user


def test_empty_catalog_never_restores_full_registry():
    available, selected, _ = select_documents({"utterance": "mở cốp", "tasks": {}, "capabilities": []})
    assert available == selected == []
    assert not any(t["function"]["name"] == "open_trunk" for t in proposal_tools([], {}, "mở cốp"))


@pytest.mark.asyncio
@pytest.mark.parametrize("json_transport", [True, False])
async def test_transports_use_only_model_schemas_and_preserve_truncation_rejection(json_transport):
    tools = proposal_tools(local_tool_documents(catalog(False)), {}, "mở cốp")
    calls = [{"name": "open_trunk", "arguments": {}}]
    truncated = False

    def handler(request):
        payload = json.loads(request.content)
        assert "_binding" not in request.content.decode()
        assert ("response_format" in payload) == json_transport
        if not json_transport:
            assert payload["parallel_tool_calls"] is True
        message = (
            {"content": json.dumps({"calls": calls})}
            if json_transport
            else {
                "tool_calls": [
                    {"function": {"name": c["name"], "arguments": json.dumps(c["arguments"])}} for c in calls
                ]
            }
        )
        return httpx.Response(
            200, json={"choices": [{"finish_reason": "length" if truncated else "stop", "message": message}]}
        )

    client = StructuredChatClient(
        base_url="http://test/v1",
        api_key="",
        model="qwen",
        timeout_seconds=1,
        tools_as_json=json_transport,
        transport=httpx.MockTransport(handler),
    )
    assert await client.agenerate_proposals(system="test", user="test", tools=tools) == [
        (c["name"], c["arguments"]) for c in calls
    ]
    truncated = True
    with pytest.raises(TruncatedCompletionError):
        await client.agenerate_proposals(system="test", user="test", tools=tools)


@pytest.mark.asyncio
async def test_repair_preserves_bad_proposal_and_error_in_audit():
    class Model:
        tools_as_json = True
        n = 0

        async def agenerate_proposals(self, **kwargs):
            self.n += 1
            if self.n == 1:
                return [("nonexistent", {})]
            assert "nonexistent" in kwargs["user"] and "VALIDATION ERROR" in kwargs["user"]
            return [("play_music", {})]

    attempts = [{"calls": 0}]
    model = Model()
    result = await interpret_operations(
        model, {"utterance": "bật nhạc", "tasks": {}, "capabilities": catalog(True)}, attempts
    )
    assert result.actions[0].capability == "media.play" and model.n == 2
    assert attempts[0]["proposal_attempts"][0]["validation"] == "rejected"


@pytest.mark.asyncio
async def test_repair_uses_the_public_parameter_name():
    class Model:
        tools_as_json = True
        n = 0

        async def agenerate_proposals(self, **kwargs):
            self.n += 1
            if self.n == 1:
                return [("set_temperature", {"celsius": 27})]
            assert "Invented celsius=27" in kwargs["user"]
            assert "value_celsius" not in kwargs["user"]
            return [("set_temperature", {"celsius": 25})]

    model = Model()
    value = await interpret_operations(
        model, {"utterance": "đặt điều hòa 25 độ", "tasks": {}, "capabilities": catalog(True)}, [{"calls": 0}]
    )
    assert value.actions[0].arguments.value_celsius == 25


def test_invented_literal_is_rejected_without_copying_utterance():
    docs = local_tool_documents(catalog(False))
    tools = proposal_tools(docs, {}, "đặt 25 độ")
    with pytest.raises(ValueError, match="Invented value_celsius"):
        proposals_to_turn([("set_temperature", {"celsius": 27})], docs, {}, "đặt 25 độ", tools)


def test_suspended_identity_and_explicit_exclusion_are_host_owned():
    _, docs, tasks = prepared(values={"open": False, "zone": "all"})
    task = tasks.pop("active")
    task["handle"] = "suspended:2"
    tasks["suspended"] = [task]
    text = "trừ bên tài"
    tools = proposal_tools(docs, tasks, text)
    result = proposals_to_turn(
        [
            (
                "update_suspended_2_step_0_except",
                {
                    "except_targets": ["driver"],
                },
            )
        ],
        docs,
        tasks,
        text,
        tools,
    )
    assert result.task_ref == "suspended:2"
    assert result.step_updates[0].action.excluded_zones == ["driver"]


def test_examples_are_valid_and_bounded():
    from src.vivi.dialogue.local_operations import MAX_EXAMPLES, proposal_examples

    docs = local_tool_documents(catalog(True))
    tools = proposal_tools(docs, {}, "test")
    examples = proposal_examples(tools, docs, {}, json_transport=False)
    assert sum(m["role"] == "user" for m in examples) <= MAX_EXAMPLES
    schemas = {t["function"]["name"]: t["function"]["parameters"] for t in tools}
    for message in examples:
        for call in message.get("tool_calls", []):
            Draft202012Validator(schemas[call["function"]["name"]]).validate(json.loads(call["function"]["arguments"]))


@pytest.mark.parametrize(("utterance", "offered"), [("hạ hết kính sau phải xuống", False), ("hạ kính 30%", True)])
def test_percentage_tool_needs_a_spoken_amount(utterance, offered):
    tools = proposal_tools(local_tool_documents(catalog(True)), {}, utterance)
    assert ("set_window" in {t["function"]["name"] for t in tools}) is offered


def test_either_lookup_source_offers_the_merged_tool():
    _, selected, _ = select_documents({"utterance": "lịch sử chùa Một Cột", "tasks": {}, "capabilities": catalog(True)})
    assert {"web.search", "knowledge.lookup"} <= {c["name"] for c in selected}


def test_numbers_in_another_operation_do_not_enable_window_percentage_tool():
    tools = proposal_tools(local_tool_documents(catalog(True)), {}, "nâng kính phụ và chỉnh điều hòa 27 độ")
    assert "set_window" not in {tool["function"]["name"] for tool in tools}


def test_memory_key_is_a_typed_category_and_required_fields_survive_simplification():
    tools = proposal_tools(local_tool_documents(catalog(True)), {}, "ghi nhớ")
    schemas = {tool["function"]["name"]: tool["function"]["parameters"] for tool in tools}
    validator = Draft202012Validator(schemas["memory_remember"])
    validator.validate({"memory_key": "display_name", "memory_value": "Ngọc"})
    validator.validate({"memory_key": "note.parking", "memory_value": "Tầng hầm B"})
    assert not validator.is_valid({"memory_key": "Ngọc", "memory_value": "Ngọc"})
    assert not validator.is_valid({"memory_key": "display_name"})
    assert schemas["navigation_select"]["required"] == ["place_handle"]


def test_host_does_not_turn_vocative_into_heat_or_drop_spoken_amount():
    docs = local_tool_documents(catalog(True))
    text = "ViVi ơi tăng lên thêm ba độ"
    tools = proposal_tools(docs, {}, text)
    result = decode_proposals([("increase_temperature", {"degrees": 3})], docs, {}, text, tools)
    assert result.actions[0].arguments.temperature_delta_celsius == 3
    text = "tăng thêm hai độ"
    tools = proposal_tools(docs, {}, text)
    result = decode_proposals([("increase_temperature", {"degrees": 2})], docs, {}, text, tools)
    assert result.actions[0].arguments.temperature_delta_celsius == 2


def test_side_matching_an_explicit_seat_role_preserves_that_role():
    docs = local_tool_documents(catalog(True))
    text = "đóng cánh cửa ghế phụ"
    tools = proposal_tools(docs, {}, text)
    result = decode_proposals([("close_door", {"targets": ["right"]})], docs, {}, text, tools)
    assert result.actions[0].arguments.zone == "front_passenger"
    with pytest.raises(ValueError, match="Vị trí"):
        decode_proposals([("close_door", {"targets": ["left"]})], docs, {}, text, tools)


@pytest.mark.parametrize(
    ("text", "allowed"),
    [
        ("cửa hàng sau bên phải", {"rear_right"}),
        ("ghế tài xế", {"driver"}),
        ("chỗ người lái", {"driver"}),
        ("cửa trước trái", {"driver"}),
        ("đằng trước trái", {"driver"}),
        ("ở đằng trước phía bên tay phải", {"front_passenger"}),
        ("bên trái", {"left"}),
        ("hàng sau", {"rear"}),
        ("mọi cửa", {"all"}),
        ("khóa hết cửa", {"all"}),
        ("hạ hết kính sau phải", {"rear_right"}),
        ("mở hết kính bên phụ", {"front_passenger"}),
    ],
)
def test_generation_grammar_only_offers_grounded_positions(text, allowed):
    from src.vivi.dialogue.local_operations import generation_tools

    docs = local_tool_documents(catalog(True))
    tools = proposal_tools(docs, {}, text)
    before = deepcopy(tools)
    wire = generation_tools(tools, text)
    schema = next(
        t["function"]["parameters"]["properties"]["targets"]
        for t in wire
        if t["function"]["name"] in {"open_door", "close_door", "lock_door", "unlock_door"}
    )
    assert set(schema["items"]["enum"]) == allowed
    assert tools == before


def test_missing_position_grammar_cannot_invent_a_seat_and_repeat_verb_is_not_driver():
    from src.vivi.dialogue.local_operations import generation_tools

    for text in ("mở cửa", "đóng cửa lại", "mở hết"):
        tools = proposal_tools(local_tool_documents(catalog(True)), {}, text)
        wire = generation_tools(tools, text)
        schema = next(
            t["function"]["parameters"]["properties"]["targets"]
            for t in wire
            if t["function"]["name"] in {"open_door", "close_door", "lock_door", "unlock_door"}
        )
        validator = Draft202012Validator(schema)
        assert validator.is_valid([])
        assert not validator.is_valid(["driver"])
        assert not validator.is_valid(["all"])


@pytest.mark.parametrize(
    "text", ["kính bên phụ mở 35%", "cho kính bên tài mở một nửa", "hạ kính sau trái 40 phần trăm"]
)
def test_specific_window_amount_keeps_value_bearing_tool(text):
    from src.vivi.dialogue.local_operations import generation_tools

    tools = proposal_tools(local_tool_documents(catalog(True)), {}, text)
    names = {t["function"]["name"] for t in generation_tools(tools, text)}
    assert "set_window" in names
    assert not names & {"raise_window", "lower_window"}


def test_compound_window_requests_keep_endpoint_and_percentage_tools():
    from src.vivi.dialogue.local_operations import generation_tools

    text = "hạ kính trước trái 35% rồi nâng kính sau phải"
    tools = proposal_tools(local_tool_documents(catalog(True)), {}, text)
    names = {t["function"]["name"] for t in generation_tools(tools, text)}
    assert {"set_window", "raise_window", "lower_window"} <= names


def test_slot_reply_scope_distinguishes_field_values_from_new_commands_and_questions():
    from src.vivi.dialogue.local_operations import slot_reply_kind

    _, docs, tasks = prepared(values={"open": True, "target_row": "front"})
    assert slot_reply_kind("bên phải", tasks, docs) == "targets"
    assert slot_reply_kind("đằng trước trái", tasks, docs) == "targets"
    assert slot_reply_kind("cửa ở đằng trước phía bên tay trái giúp tôi", tasks, docs) == "targets"
    assert slot_reply_kind("à sửa lại hàng sau", tasks, docs) == "targets"
    assert slot_reply_kind("mở cửa sau phải", tasks, docs) is None
    assert slot_reply_kind("thời tiết ở đâu", tasks, docs) is None
    assert slot_reply_kind("thời tiết trước trái", tasks, docs) is None
    assert slot_reply_kind("bên trái hay bên phải?", tasks, docs) is None
    assert slot_reply_kind("đừng bên trái", tasks, docs) is None
    assert slot_reply_kind("3 độ", tasks, docs) is None
    _, climate_docs, climate_tasks = prepared(
        capability="climate.set_temperature", values={"temperature_direction": "increase"}
    )
    assert slot_reply_kind("trước trái", climate_tasks, climate_docs) is None


def test_explicit_accent_difference_is_not_a_seat_alias():
    from src.vivi.vehicle.zones import spoken_zone_aliases

    assert spoken_zone_aliases("đóng cửa lại") == set()
    assert spoken_zone_aliases("cửa lái") == {"driver"}
    assert spoken_zone_aliases("ghe tai xe") == {"driver"}


def test_creative_tool_owns_its_mode_without_answer_examples():
    docs = local_tool_documents(catalog(True))
    tools = proposal_tools(docs, {}, "sáng tác")
    result = decode_proposals([("create_content", {})], docs, {}, "sáng tác một lời chúc sinh nhật", tools)
    assert result.actions[0].arguments.mode == "creative"


def test_clause_coverage_does_not_decide_play_pause_from_discovery_keywords():
    from src.vivi.dialogue.contracts import SemanticTurn
    from src.vivi.dialogue.local_operations import check_coverage

    result = SemanticTurn(act="request", actions=[ActionDraft(capability="media.pause")])
    check_coverage(result, "tạm dừng việc phát nhạc", catalog(True), final=True)


def test_clause_coverage_still_rejects_a_dropped_operation_in_the_same_domain():
    from src.vivi.dialogue.clause_guard import HostTurn
    from src.vivi.dialogue.contracts import SemanticTurn
    from src.vivi.dialogue.local_operations import check_coverage

    result = SemanticTurn(
        act="request", actions=[ActionDraft(capability="door.set_open", arguments={"open": True, "zone": "rear_left"})]
    )
    with pytest.raises(HostTurn, match="missing_clause"):
        check_coverage(result, "mở cửa sau trái rồi đóng cửa sau phải", catalog(True), final=True)


def test_duplicate_supported_actions_do_not_cover_an_unknown_control_clause():
    from src.vivi.dialogue.clause_guard import HostTurn
    from src.vivi.dialogue.contracts import SemanticTurn
    from src.vivi.dialogue.local_operations import check_coverage

    result = SemanticTurn(
        act="request", actions=[ActionDraft(capability="media.play"), ActionDraft(capability="media.pause")]
    )
    with pytest.raises(HostTurn, match="unsupported_clause"):
        check_coverage(result, "phát nhạc rồi bật đèn nội thất", catalog(True), final=True)


@pytest.mark.parametrize(
    ("targets", "utterance", "expected"),
    [
        (["rear_left"], "2 cửa bên trái thôi", {"target_side": "left"}),
        (["rear_left", "left"], "mở 2 cửa phía bên trái", {"target_side": "left"}),
        (["driver", "rear_left"], "hai cửa bên trái", {"target_side": "left"}),
        (["rear_left"], "cửa sau bên trái", {"zone": "rear_left"}),
        (["driver"], "mở cửa bên tài", {"zone": "driver"}),
        (["rear_right"], "bên phải", {"target_side": "right"}),
        # A mis-picked dimension is corrected from a word the utterance does say.
        (["rear_left"], "phía trước bên trái", {"target_side": "left", "target_row": "front"}),
        (["driver", "rear_right"], "cửa lái và cửa sau phải", {"zones": ["driver", "rear_right"]}),
    ],
)
def test_overspecified_targets_are_reduced_to_what_the_utterance_evidences(targets, utterance, expected):
    from src.vivi.dialogue.small_tools import decode_targets, normalize_targets

    assert decode_targets(normalize_targets(targets, utterance)) == expected


def test_a_fully_invented_zone_is_rejected_so_repair_can_retry():
    from src.vivi.dialogue.small_tools import normalize_targets

    with pytest.raises(ValueError, match="không có trong câu"):
        normalize_targets(["driver"], "mở cửa")


def test_left_side_followup_resolves_to_a_side_not_a_single_rear_door():
    # The real 18:44 log: "mở cửa" form, answered "2 cửa bên trái thôi".
    _, docs, tasks = prepared(values={"open": True, "zone": None})
    tools = proposal_tools(docs, tasks, "2 cửa bên trái thôi")
    result = proposals_to_turn(
        [("update_current_request", {"targets": ["rear_left"]})], docs, tasks, "2 cửa bên trái thôi", tools
    )
    assert result.act == "modify"
    assert result.step_updates[0].action.arguments.target_side == "left"
    assert result.step_updates[0].action.arguments.zone is None


@pytest.mark.parametrize(
    ("targets", "utterance", "expected"),
    [
        (["front"], "mở cửa sổ", {}),
        (["left", "front"], "mở cửa sổ", {}),
        (["rear_right"], "đằng sau", {"target_row": "rear"}),
        (["rear"], "phía sau", {"target_row": "rear"}),
        (["right"], "bên phải", {"target_side": "right"}),
    ],
)
def test_unsaid_bare_side_or_row_is_dropped_so_the_form_asks_location(targets, utterance, expected):
    # "mở cửa sổ" must not carry an invented front/left; the form then asks the
    # full "cửa sổ nào" instead of only "bên trái hay bên phải".
    from src.vivi.dialogue.small_tools import decode_targets, normalize_targets

    assert decode_targets(normalize_targets(targets, utterance)) == expected


def test_window_with_no_location_asks_all_positions_not_only_a_side():
    from src.vivi.capabilities.registry import REGISTRY

    draft = ActionDraft(capability="window.set_position", arguments={"position_percent": 100})
    assert REGISTRY["window.set_position"].missing_slots(draft) == ["zone"]
    assert "sau phải" in REGISTRY["window.set_position"].validate(draft)[1]


def test_mid_form_change_of_mind_overrides_the_stale_filled_dimension():
    # Real 19:44 log: "mở cửa ở phía sau" (row=rear), then answered
    # "phía trước bên trái". The model still said rear_left; the host rebuilds
    # from the spoken "trước"/"trái" and the merge must land on the front door.
    from src.vivi.capabilities.registry import REGISTRY, merge_arguments

    _, docs, tasks = prepared(values={"open": True, "target_row": "rear"})
    tools = proposal_tools(docs, tasks, "phía trước bên trái")
    result = proposals_to_turn(
        [("update_current_request", {"targets": ["rear_left"]})], docs, tasks, "phía trước bên trái", tools
    )
    update = result.step_updates[0].action
    assert update.arguments.target_row == "front" and update.arguments.target_side == "left"
    old = ActionDraft(capability="door.set_open", arguments={"open": True, "target_row": "rear"})
    merged = merge_arguments(old, update)
    assert REGISTRY["door.set_open"].validate(ActionDraft(capability="door.set_open", arguments=merged))[0] == {
        "zone": "driver",
        "open": True,
    }


@pytest.mark.parametrize(
    ("utterance", "expected"),
    [
        ("chào bạn, mở cốp", "mở cốp"),
        ("xin chào, đóng cửa sổ bên phải", "đóng cửa sổ bên phải"),
        ("chào bạn, hôm nay thứ mấy", "hôm nay thứ mấy"),
        ("chào bạn", "chào bạn"),
        ("cảm ơn nhé", "cảm ơn nhé"),
        ("mở cốp", "mở cốp"),
        ("chào Vivi buổi sáng", "chào Vivi buổi sáng"),
    ],
)
def test_leading_greeting_is_stripped_only_when_a_request_follows(utterance, expected):
    from src.vivi.dialogue.clause_guard import strip_leading_greeting

    assert strip_leading_greeting(utterance) == expected


def test_reply_is_withheld_when_a_control_clause_rides_with_a_greeting():
    names = {t["function"]["name"] for t in proposal_tools(local_tool_documents(catalog(True)), {}, "chào bạn, mở cốp")}
    assert "dialogue_reply" not in names
    assert {"open_trunk", "close_trunk"} <= names


@pytest.mark.asyncio
async def test_greeting_plus_command_executes_the_command_not_a_reply():
    # Real 19:43 log: "chào bạn, mở cốp" had dropped the command for a reply.
    class Model:
        tools_as_json = True

        async def agenerate_proposals(self, *, tools, **kwargs):
            assert "dialogue_reply" not in {t["function"]["name"] for t in tools}
            return [("open_trunk", {})]

    context = {"utterance": "chào bạn, mở cốp", "tasks": {}, "capabilities": catalog(True)}
    result = await interpret_operations(Model(), context, [{"calls": 0}])
    assert result.act == "request" and result.actions[0].capability == "trunk.set_open"
    assert result.actions[0].arguments.open is True


@pytest.mark.parametrize(
    ("utterance", "direction", "degrees", "expected"),
    [
        ("tăng điều hoà lên 30 độ", "increase", 30, {"value_celsius": 30.0}),
        ("giảm xuống 20 độ", "decrease", 20, {"value_celsius": 20.0}),
        ("tăng thêm 3 độ", "increase", 3, {"temperature_delta_celsius": 3.0}),
        ("giảm bớt 1 độ", "decrease", 1, {"temperature_delta_celsius": -1.0}),
        ("tôi lạnh", "increase", None, {"temperature_direction": "increase"}),
        ("trong xe nóng quá", "decrease", None, {"temperature_direction": "decrease"}),
    ],
)
def test_relative_temperature_splits_target_delta_and_amountless(utterance, direction, degrees, expected):
    from src.vivi.dialogue.local_tools import resolve_relative_temperature

    assert resolve_relative_temperature(direction, degrees, utterance) == expected


def test_amountless_comfort_asks_the_target_with_a_directional_question():
    from src.vivi.capabilities.registry import REGISTRY

    for direction, word in (("increase", "tăng"), ("decrease", "giảm")):
        draft = ActionDraft(capability="climate.set_temperature", arguments={"temperature_direction": direction})
        assert REGISTRY["climate.set_temperature"].missing_slots(draft) == ["value_celsius"]
        _, question = REGISTRY["climate.set_temperature"].validate(draft, observation=None)
        assert word in question and "bao nhiêu độ" in question


def test_a_cold_word_flips_a_mis_picked_cooling_direction():
    from src.vivi.dialogue.local_tools import resolve_relative_temperature

    # Model mislabels "tôi lạnh quá" as decrease; the host corrects to increase.
    assert resolve_relative_temperature("decrease", None, "tôi lạnh quá") == {"temperature_direction": "increase"}


def test_amount_answer_below_cabin_range_is_a_delta_in_the_stored_direction():
    # Turn 45 log: comfort form (increase) answered "tăng thêm 3 độ"/"3 độ".
    from src.vivi.capabilities.registry import REGISTRY, merge_arguments

    class Obs:
        temperature_celsius = 22.0

    step = ActionDraft(capability="climate.set_temperature", arguments={"temperature_direction": "increase"})
    answer = ActionDraft(capability="climate.set_temperature", arguments={"value_celsius": 3.0})
    merged = ActionDraft(capability="climate.set_temperature", arguments=merge_arguments(step, answer))
    assert merged.arguments.temperature_direction == "increase"
    assert REGISTRY["climate.set_temperature"].validate(merged, observation=Obs())[0] == {"value_celsius": 25.0}
    # A plausible cabin target stays a target.
    target = ActionDraft(
        capability="climate.set_temperature",
        arguments={"value_celsius": 25.0, "temperature_direction": "increase"},
    )
    assert REGISTRY["climate.set_temperature"].validate(target, observation=Obs())[0] == {"value_celsius": 25.0}


def test_climate_form_never_lets_the_model_set_or_flip_direction():
    _, docs, tasks = prepared(
        status="waiting_slot", capability="climate.set_temperature", values={"temperature_direction": "increase"}
    )
    for item in proposal_tools(docs, tasks, "3 độ"):
        props = item["function"]["parameters"].get("properties", {})
        assert "temperature_direction" not in props


@pytest.mark.parametrize("utterance", ["giảm 2 độ", "tăng tiếp 2 độ", "điều hoà 25 độ"])
def test_degree_commands_discover_climate(utterance):
    from src.vivi.dialogue.tool_discovery import matched_capabilities

    names = {c["name"] for c in matched_capabilities(utterance, catalog(True))}
    assert "climate.set_temperature" in names


def test_degree_cue_does_not_match_the_word_there():
    from src.vivi.dialogue.tool_discovery import matched_capabilities

    names = {c["name"] for c in matched_capabilities("thời tiết ở đó", catalog(True))}
    assert names == {"weather.get_forecast"}
