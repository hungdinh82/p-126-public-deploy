"""Host form lifecycle projected from the existing task checkpoint.

No second store: revision, consent and effect ownership remain in the workflow.
Canonical schemas here validate translated values; small_tools defines the
separate, minimal schemas actually shown to the local operations model.
"""

from __future__ import annotations

import copy

from jsonschema import Draft202012Validator

from src.vivi.dialogue.contracts import SemanticTurn
from src.vivi.vehicle.zones import ZONE_LABELS

FORM_CAPABILITIES = {
    "door.set_open",
    "door.set_lock",
    "window.set_position",
    "seat.set_heat_level",
    "hood.set_open",
    "trunk.set_open",
    "body.set_open",
    "climate.set_temperature",
    "media.play",
    "media.pause",
}


def _inline(schema):
    definitions = schema.get("$defs", {})

    def visit(value):
        if isinstance(value, dict):
            if "$ref" in value:
                return visit(definitions[value["$ref"].rsplit("/", 1)[-1]])
            return {k: visit(v) for k, v in value.items() if k != "$defs"}
        if isinstance(value, list):
            return [visit(v) for v in value]
        return value

    return visit(schema)


def _fields(document):
    schema = _inline(document["inputSchema"])
    properties = {}
    for branch in schema.get("oneOf", [schema]):
        properties.update(copy.deepcopy(branch.get("properties", {})))
    result = {"type": "object", "properties": properties, "additionalProperties": False}
    if "$defs" in schema:
        result["$defs"] = copy.deepcopy(schema["$defs"])
    return result


def _operations(document):
    """Intent aliases distinguish opening/closing doors and raising/lowering windows."""
    capability = document.get("_meta", {}).get("capability", "")
    if capability.endswith(".set_open") or capability in {"window.set_position", "door.set_lock"}:
        field = (
            "position_percent"
            if capability == "window.set_position"
            else "locked"
            if capability == "door.set_lock"
            else "open"
        )
        endpoints = [("open", 100), ("close", 0)] if field == "position_percent" else [("open", True), ("close", False)]
        for verb, value in endpoints:
            schema = _inline(document["inputSchema"])
            for branch in schema.get("oneOf", [schema]):
                branch.get("properties", {}).pop(field, None)
                branch["required"] = [key for key in branch.get("required", []) if key != field]
                if "zone" in branch.get("properties", {}):
                    # Explicit unknown is null; the model must decide rather
                    # than silently omit an actually supplied location.
                    branch["required"] = list(dict.fromkeys([*branch["required"], "zone"]))
                    branch["properties"]["zone"]["description"] = (
                        "all=toàn bộ bốn cửa cabin; driver=bên tài/cửa lái; front_passenger=bên phụ; rear_left=sau trái; rear_right=sau phải. Không nói vị trí: null. Có số lượng/còn lại ít hơn bốn cửa: dùng zones, không dùng all."
                    )
                if "zones" in branch.get("properties", {}):
                    branch["properties"]["zones"]["description"] = (
                        "Danh sách các cửa được yêu cầu. Với cửa còn lại, đọc vehicle_observation.door_open: true=đang mở, false=đang đóng. Đóng các cửa còn lại chỉ chọn cửa đang mở; mở các cửa còn lại chỉ chọn cửa đang đóng. Không thêm cửa đã ở trạng thái đích."
                    )
            operation = capability.split(".")[0] + "_" + verb
            if field == "locked":
                operation = "door_lock" if verb == "open" else "door_unlock"
            yield operation, {field: value}, schema
        if field == "position_percent":
            yield document["name"], {}, _inline(document["inputSchema"])
    else:
        yield document["name"], {}, _inline(document["inputSchema"])


def _window_tool_name(operation):
    return {
        "window_close": "form_create_raise_window",
        "window_open": "form_create_lower_window",
        "window_set_position": "form_create_window_position",
    }[operation]


def current_form(tasks, documents):
    task = (tasks or {}).get("active")
    if not task:
        return None
    step = next((s for s in task.get("steps", []) if s["step_index"] == task.get("current_step")), None)
    if (
        not step
        or step["capability"] not in FORM_CAPABILITIES
        or step["status"] not in {"waiting_slot", "waiting_confirmation"}
    ):
        return None
    document = next((d for d in documents if d["name"] == step["capability"].replace(".", "_")), None)
    if not document:
        return None
    values = step.get("resolved_arguments") or step["arguments"]
    operation = document["name"]
    if step["capability"].endswith(".set_open") and isinstance(values.get("open"), bool):
        operation = step["capability"].split(".")[0] + ("_open" if values["open"] else "_close")
    if step["capability"] == "door.set_lock" and isinstance(values.get("locked"), bool):
        operation = "door_lock" if values["locked"] else "door_unlock"
    if step["capability"] == "window.set_position" and values.get("position_percent") in (0, 100):
        operation = "window_close" if values["position_percent"] == 0 else "window_open"
    return {
        "capability": step["capability"],
        "location_choices": ZONE_LABELS if "zone" in _fields(document)["properties"] else {},
        "form_id": f"{task.get('form_identity', 'active')}:step:{step['step_index']}",
        "revision": task["revision"],
        "operation": operation,
        "status": step["status"],
        "values": values,
        "missing_fields": step.get("missing_slots", []),
        "question": step.get("question"),
        "field_schema": _fields(document),
        "field_guide": document["description"],
    }


def lifecycle_tools(documents, tasks, *, utterance=None):
    form = current_form(tasks, documents)
    choices = [
        (d, operation, constants, schema)
        for d in documents
        if d.get("_meta", {}).get("capability") in FORM_CAPABILITIES
        for operation, constants, schema in _operations(d)
    ]
    tools = []
    windows = [choice for choice in choices if choice[0]["_meta"]["capability"] == "window.set_position"]
    choices = [choice for choice in choices if choice not in windows]
    for document, operation, _, schema in windows:
        schema = _fields(document)
        if operation != "window_set_position":
            schema["properties"].pop("position_percent", None)
        schema["properties"]["location_text"] = {
            "type": ["string", "null"],
            "description": "Exact location phrase from the user, e.g. bên tài. No location in input: null, and zone=null. Never infer driver/all.",
        }
        schema["required"] = ["location_text", "zone"]
        if operation == "window_set_position":
            schema["required"].append("position_percent")
        description = {
            "window_close": "LÊN kính, NÂNG kính, ĐÓNG cửa kính: tạo form đóng kính hoàn toàn (0% mở).",
            "window_open": "XUỐNG kính, HẠ kính, MỞ cửa kính: tạo form mở kính hoàn toàn (100% mở).",
            "window_set_position": "Tạo form đặt phần trăm mở kính khi người dùng nói rõ con số phần trăm.",
        }[operation]
        tools.append(
            {
                "type": "function",
                "function": {
                    "name": _window_tool_name(operation),
                    "description": description
                    + " Chỉ điền vị trí được nói rõ. Không nói vị trí: zone=null; không đoán driver hoặc all. Bên tài=driver; bên phụ=front_passenger; sau trái=rear_left; sau phải=rear_right.",
                    "parameters": schema,
                },
            }
        )
    if choices:
        branches = [
            {
                "type": "object",
                "description": d["description"],
                "properties": {"operation": {"type": "string", "enum": [operation]}, "values": schema},
                "required": ["operation", "values"],
                "additionalProperties": False,
            }
            for d, operation, _, schema in choices
        ]
        tools.append(
            {
                "type": "function",
                "function": {
                    "name": "form_create",
                    "description": "Tạo form yêu cầu mới. operation door_open=mở cửa, door_close=đóng cửa; trunk_open/mở cốp, trunk_close/đóng cốp; hood_open/mở capo, hood_close/đóng capo. Nhạc: media_play=bật/mở, media_pause=tắt/dừng; chỉ mở nhạc không nói thể loại thì values={}. Chọn đúng operation theo câu hiện tại. Có tất cả/toàn bộ: zone=all; không bỏ vị trí đã nói rõ. values chỉ chứa vị trí hoặc số được nói; thiếu thì bỏ trường để hỏi thêm. Không tự chọn driver hoặc mọi cửa từ trạng thái xe. Ví dụ mở cửa xe cho tôi => door_open với values={zone:null}; đóng cửa xe cho tôi => door_close với values={zone:null}; thiếu vị trí thì phải để form hỏi. Chỉ suy vị trí từ trạng thái xe khi người dùng nói rõ còn lại/đang mở/đang đóng. Chưa thực hiện thao tác.",
                    "parameters": branches[0] if len(branches) == 1 else {"oneOf": branches},
                },
            }
        )
    if not form:
        return tools
    identity = {
        "form_id": {"type": "string", "enum": [form["form_id"]]},
        "revision": {"type": "integer", "enum": [form["revision"]]},
    }
    patch_schema = copy.deepcopy(form["field_schema"])
    if form["status"] == "waiting_slot" and isinstance(form["values"].get("open"), bool):
        patch_schema["properties"].pop("open", None)
    if form["status"] == "waiting_slot" and isinstance(form["values"].get("locked"), bool):
        patch_schema["properties"].pop("locked", None)
    if form["status"] == "waiting_slot" and form["values"].get("position_percent") is not None:
        patch_schema["properties"].pop("position_percent", None)
    patch_schema["minProperties"] = 1
    tools.append(
        {
            "type": "function",
            "function": {
                "name": "form_update",
                "description": "Điền hoặc sửa form hiện tại bằng câu trả lời người dùng, kể cả không dấu/lỗi gõ nhẹ. Chỉ gửi values thay đổi; giữ thao tác và thông tin khác. Vị trí: driver=bên tài/ghế lái/cửa lái/tài xế; front_passenger=bên phụ/phụ/hành khách; rear_left=sau trái; rear_right=sau phải. Không reply thay cho điền form.",
                "parameters": {
                    "type": "object",
                    "properties": {**identity, "values": patch_schema},
                    "required": ["form_id", "revision", "values"],
                    "additionalProperties": False,
                },
            },
        }
    )
    if form["status"] == "waiting_confirmation":
        full_schema = copy.deepcopy(form["field_schema"])
        full_schema["required"] = list(form["values"])
        tools.append(
            {
                "type": "function",
                "function": {
                    "name": "form_submit",
                    "description": "Chỉ khi người dùng xác nhận OK cho form này: gửi TOÀN BỘ values của form hiện tại. Không sửa giá trị trong lúc xác nhận. Backend kiểm tra quyền xác nhận rồi mới thực hiện.",
                    "parameters": {
                        "type": "object",
                        "properties": {**identity, "values": full_schema},
                        "required": ["form_id", "revision", "values"],
                        "additionalProperties": False,
                    },
                },
            }
        )
    return tools


def _canonical_location(values):
    """Collapse redundant typed coordinates only when they name the same zone."""
    values = dict(values)
    concrete = values.get("zones") or ([values["zone"]] if values.get("zone") is not None else [])
    coordinates = {
        "driver": ("left", "front"),
        "front_passenger": ("right", "front"),
        "rear_left": ("left", "rear"),
        "rear_right": ("right", "rear"),
    }
    if concrete and any(values.get(key) is not None for key in ("target_side", "target_row")):
        targets = list(coordinates) if "all" in concrete else concrete
        if any(
            (values.get("target_side") is not None and values["target_side"] != coordinates[zone][0])
            or (values.get("target_row") is not None and values["target_row"] != coordinates[zone][1])
            for zone in targets
        ):
            raise ValueError("Conflicting concrete zone and partial coordinates; use one consistent location.")
        values.pop("target_side", None)
        values.pop("target_row", None)
    if values.get("zone") is None and values.get("zones"):
        values.pop("zone", None)
    return values


def decode_form_call(calls, documents, tasks, *, allow_submit=False, utterance=None):
    """Validate a model form call and translate it into the existing reducer DTO."""
    if not any(name.startswith("form_create") or name in {"form_update", "form_submit"} for name, _ in calls):
        return None
    if len(calls) > 1 and all(name.startswith("form_create") for name, _ in calls):
        if len(calls) > 5:
            raise ValueError("At most five form requests per turn")
        return [item for call in calls for item in decode_form_call([call], documents, tasks, utterance=utterance)]
    if len(calls) != 1:
        raise ValueError("A form update or submission must be atomic")
    name, payload = calls[0]
    # Flat per-operation tools share the historical create/update adapter.
    if name.startswith("form_create_") and name not in {
        "form_create_raise_window",
        "form_create_lower_window",
        "form_create_window_position",
    }:
        payload = {"operation": name.removeprefix("form_create_"), "values": payload}
        name = "form_create"
    offered = {
        t["function"]["name"]: t["function"]["parameters"]
        for t in lifecycle_tools(documents, tasks, utterance=utterance)
    }
    if name not in offered:
        raise ValueError("Form is no longer available")
    errors = list(Draft202012Validator(offered[name]).iter_errors(payload))
    if errors:
        raise ValueError("Invalid form arguments: " + errors[0].message)
    if name.startswith("form_create"):
        for document in documents:
            for operation, constants, _ in _operations(document):
                if (name == "form_create" and operation == payload["operation"]) or (
                    document["_meta"]["capability"] == "window.set_position" and name == _window_tool_name(operation)
                ):
                    values = payload["values"] if name == "form_create" else payload
                    if document["_meta"]["capability"] == "window.set_position":
                        values = dict(values)
                        evidence = (values.pop("location_text", None) or "").strip()
                        has_location = any(
                            values.get(key) not in (None, [], "")
                            for key in ("zone", "zones", "target_side", "target_row")
                        )
                        if has_location and not evidence:
                            raise ValueError(
                                "No location_text evidence: use zone=null, not an assumed driver/all/side."
                            )
                        if (
                            has_location
                            and evidence
                            and utterance is not None
                            and evidence.casefold() not in utterance.casefold()
                        ):
                            raise ValueError(
                                f"Invented location_text={evidence!r} is absent from user input {utterance!r}. Extract only a location explicitly present. If none is present, set location_text=null and zone=null; omit zones/target_side/target_row. Do not repeat invented location."
                            )
                    return [("create", document["name"], _canonical_location({**values, **constants}))]
        raise ValueError("Unknown form operation")
    form = current_form(tasks, documents)
    if not form or payload["form_id"] != form["form_id"] or payload["revision"] != form["revision"]:
        raise ValueError("Stale form revision")
    values = payload["values"]
    capability = form["capability"]
    if name == "form_submit":
        if not allow_submit or values != form["values"]:
            raise ValueError("Submit requires current user consent and exactly the reviewed values")
        return SemanticTurn(act="confirm", task_ref="active")
    # Null is an unknown slot, not an edit. The reducer merges only non-null
    # arguments; counting null as a change would revoke consent for a no-op.
    values = _canonical_location({key: value for key, value in values.items() if value is not None})
    if values.get("zone") is not None and values.get("zones") is not None:
        raise ValueError("Use zone OR zones, not both")
    changes = {key: value for key, value in values.items() if key not in {"value_text", "location_text"}}
    if all(form["values"].get(key) == value for key, value in changes.items()):
        raise ValueError(
            "No form values changed. Fill a missing field from the current answer, or ask a specific clarification; do not repeat existing values."
        )
    # Action values are validated independently; task/step handles are host-bound.
    from src.vivi.dialogue.contracts import StepUpdate

    index = int(form["form_id"].rsplit(":", 1)[-1])
    from src.vivi.dialogue.local_tools import draft_from_values, validate_literal_numbers

    if utterance is not None:
        validate_literal_numbers(capability, values, utterance)

    action = draft_from_values(capability, values)
    return SemanticTurn(act="modify", task_ref="active", step_updates=[StepUpdate(step_index=index, action=action)])
