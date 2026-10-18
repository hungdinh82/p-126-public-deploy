"""Typed proposal documentation, adapted from Jetson commit 5d47f6c.

Documents and schemas propose meaning only. The existing reducer and policy
remain the only owners of task state, consent and vehicle effects.
"""

from __future__ import annotations

import re
from copy import deepcopy

from jsonschema import Draft202012Validator

from src.vivi.agents.tools import TOOL_REGISTRY
from src.vivi.dialogue.contracts import ActionDraft, SemanticTurn, StepUpdate
from src.vivi.dialogue.form_tools import FORM_CAPABILITIES, current_form, decode_form_call, lifecycle_tools
from src.vivi.dialogue.local_schema import local_semantic_schema

GUIDANCE = {
    "trunk.set_open": "Dùng cho cốp xe/cốp sau/cửa hậu. Không phải cửa ghế sau, cửa kính hay nắp capo. Chỉ open=true (mở) hoặc false (đóng).",
    "hood.set_open": "Dùng cho nắp capo/khoang phía trước. Không phải cốp sau hoặc cửa cabin.",
    "window.set_position": "Dùng cho cửa kính/cửa sổ. Bên tài=driver; bên phụ=front_passenger; sau trái=rear_left; sau phải=rear_right. Xuống/hạ/mở kính dùng window_open=100% mở; lên/nâng/đóng kính dùng window_close=0% mở. Không nói vị trí: zone=null, hỏi vị trí, không tự chọn bên tài. Giữ phần trăm được nói. Không mở cửa cabin.",
    "door.set_open": "Mở cửa: open=true. Đóng cửa: open=false. Bên tài/cửa lái/tài xế là driver. Cửa còn lại phải căn cứ trạng thái cửa đang được cung cấp; không đoán vị trí. Chỉ mở/đóng cánh cửa cabin. Không dùng cho cửa sổ/cửa kính, cốp, capo hoặc mở khóa.",
    "door.set_lock": "Chỉ khóa/mở khóa cửa cabin; không mở cánh cửa hoặc kính.",
    "climate.set_temperature": "Đặt nhiệt độ dùng value_celsius; thay đổi tương đối dùng delta. Nóng cần giảm, lạnh cần tăng. Không nói số độ thay đổi thì để trống để backend hỏi, không tự chọn số độ.",
    "media.play": "Phát/bật/mở nhạc hoặc tiếp tục nghe nhạc. Nhạc chung gọi với {}. Chỉ điền media_query khi có tên bài, ca sĩ, thể loại hoặc tâm trạng được nói rõ. Không tự đổi bài hoặc âm lượng. Demo UI phát nhạc mô phỏng, không khẳng định đã tìm được bản thu của ca sĩ.",
    "media.pause": "Dừng/tắt/tạm dừng nhạc, arguments={}. Không tắt loa trả lời của trợ lý; bật/tắt loa là cài đặt UI.",
    "body.set_open": "Chỉ dùng khi người dùng yêu cầu cùng lúc TẤT CẢ cửa cabin, capo VÀ cốp. Nếu chỉ nói tất cả cửa cabin: door_set_open(zone=all); tất cả cửa kính: window_set_position(zone=all).",
    "seat.set_heat_level": "Sưởi ghế với level 0=tắt, 1–3=mức sưởi. Giữ vị trí ghế được nói, thiếu vị trí thì để backend hỏi. Không chỉnh nhiệt độ điều hòa, vị trí ghế hay bật làm mát ghế.",
    "manual.search": "Tra hướng dẫn/thông số/cách dùng từ cẩm nang; không đọc số đo live hay điều khiển xe. Giữ query hiện tại. Chỉ điền model/năm/AC/DC khi có căn cứ.",
    "conversation.respond": "Chỉ dùng khi hiểu được yêu cầu trò chuyện/kiến thức/truyện. Không dùng cho câu STT không hiểu hoặc câu hỏi trợ lý cũ; câu nghe không rõ dùng dialogue_clarify. Backend tự gắn query hiện tại, model chỉ chọn mode nếu cần.",
    "navigation.set_gear": "Chỉ đề xuất số P hoặc D được yêu cầu. Chuyển D không chấp thuận tuyến mới; backend kiểm tra cửa/capo/cốp và tuyến trước khi đổi số.",
    "navigation.search": "Tìm địa điểm và chờ người dùng chọn, không tự khởi hành. Không tạo place_handle hoặc tọa độ. pharmacy/charging là bộ lọc; tìm thông tin chung trên mạng dùng web_search.",
    "navigation.plan": "Lập tuyến xem trước tới điểm đến và nơi xuất phát nếu được nói. Không tự bắt đầu dẫn đường hoặc đoán route_revision.",
    "navigation.select": "Chỉ dùng place_handle có trong các kết quả của biểu mẫu hiện tại; không tự tạo ID hoặc chọn hộ khi người dùng chưa chọn.",
    "navigation.refine": "Bổ sung/chỉnh điểm đến của biểu mẫu hiện tại: một trong query/destination, choice_handle được cấp hoặc reject_candidates=true. Không lấy điểm đến từ chủ đề đã đóng.",
    "navigation.start": "Chỉ bắt đầu tuyến người dùng đang xác nhận với route_revision do backend cung cấp. Không suy quyền xác nhận từ một yêu cầu mới.",
    "navigation.cancel": "Đóng bản nháp tìm/chọn tuyến đang chờ theo route_revision; giữ hành trình đã bắt đầu.",
    "navigation.decline": "Từ chối bắt đầu tuyến đang được hỏi; dùng đúng route_revision, không tự huỷ hành trình khác.",
    "navigation.stop": "Dừng hành trình đang chạy. Không phải dừng nhạc hoặc đóng một biểu mẫu xe.",
    "navigation.end": "Kết thúc hành trình theo yêu cầu; khác tạm dừng để tiếp tục sau.",
    "vehicle.get_status": "Chỉ dùng khi hỏi/kiểm tra trạng thái hoặc số đo. Không tự thêm summary khi yêu cầu mở cốp, cửa kính hoặc điều chỉnh nhiệt độ.",
}


def inline_schema(value, definitions):
    if isinstance(value, dict):
        if "$ref" in value:
            return inline_schema(definitions[value["$ref"].rsplit("/", 1)[-1]], definitions)
        return {key: inline_schema(item, definitions) for key, item in value.items()}
    if isinstance(value, list):
        return [inline_schema(item, definitions) for item in value]
    return value


def local_tool_documents(capabilities):
    """Generate sparse interpretation slots from the authoritative registry."""
    schema = local_semantic_schema()
    definitions = schema["$defs"]
    branches = {v["properties"]["capability"]["const"]: v for v in definitions["ActionDraft"]["anyOf"]}
    docs = []
    for item in capabilities:
        capability = item["name"]
        if capability not in branches:
            continue
        slots = inline_schema(branches[capability]["properties"]["arguments"], definitions)
        if capability not in FORM_CAPABILITIES:
            # Sparse domain drafts permit missing values for dialogue, whereas
            # these direct model tools need their actual mandatory arguments.
            slots["required"] = [
                name for name, field in TOOL_REGISTRY[capability].parameters.model_fields.items()
                if field.is_required() and name in slots["properties"]
            ]
        # Nullable unknown targets are explicit in the historical form contract.
        if "zone" in slots["properties"]:
            slots["properties"]["zone"] = {"anyOf": [slots["properties"]["zone"], {"type": "null"}]}
            for field in ("excluded_zones", "exclusion_mode", "referenced_target"):
                slots["properties"][field] = inline_schema(branches[capability]["properties"][field], definitions)
        if set(slots["properties"]).intersection({"value_celsius", "position_percent", "level"}):
            slots["properties"]["value_text"] = {
                "type": "string",
                "description": "Nguyên văn số/mức được nói, nhất là số viết bằng chữ.",
            }
        # IDs belong to request-bound adapter state, never model guesses.
        for field in ("plan_id", "route_revision"):
            slots["properties"].pop(field, None)
        if capability == "climate.set_temperature":
            slots["properties"].pop("temperature_delta_celsius", None)
            # Direction is host-owned (set from the hot/cold complaint); the model
            # must never set or flip it when answering the amount question.
            slots["properties"].pop("temperature_direction", None)
            slots["properties"]["value_celsius"] = {"anyOf": [slots["properties"]["value_celsius"], {"type": "null"}]}
            slots["required"] = ["value_celsius"]
        if capability == "conversation.respond":
            slots["properties"].pop("query", None)
        if capability.startswith("navigation."):
            # DestinationSpec is a backend/domain representation. Ask the small
            # model for literal destination text instead of provenance DTOs.
            slots["properties"].pop("destination", None)
        docs.append(
            {
                "name": capability.replace(".", "_"),
                "description": item.get("description", "") + " " + GUIDANCE.get(capability, ""),
                "inputSchema": slots,
                "annotations": {"readOnlyHint": TOOL_REGISTRY[capability].read_only},
                "_meta": {"capability": capability, "proposalOnly": True},
            }
        )
        if capability == "navigation.plan":
            base = docs.pop()
            base["inputSchema"]["properties"].pop("purpose", None)
            for field in ("category", "nearest", "destination_scope", "use_search_results"):
                base["inputSchema"]["properties"].pop(field, None)
            for purpose, suffix, description in [
                ("navigate", "directions", "Chỉ đường/dẫn đường tới địa điểm. Chỉ chuẩn bị tuyến, chưa bắt đầu đi."),
                ("itinerary", "itinerary", "Tính/lập kế hoạch hành trình; chưa bắt đầu đi."),
            ]:
                document = deepcopy(base)
                document["name"] = "navigation_plan_" + suffix
                document["description"] = (
                    description + " query giữ nguyên địa điểm được nói; origin_query chỉ khi nói nơi xuất phát."
                )
                document["_meta"]["constants"] = {"purpose": purpose}
                docs.append(document)
        if capability == "climate.set_temperature":
            docs.append(
                {
                    "name": "climate_adjust_temperature",
                    "description": "Tăng/giảm tương đối. Lạnh: increase; nóng: decrease. amount là số độ dương, bỏ trống mặc định 2. Nhiệt độ đích dùng climate_set_temperature.",
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "direction": {"type": "string", "enum": ["increase", "decrease"]},
                            "amount": {"type": "number", "exclusiveMinimum": 0, "maximum": 30},
                        },
                        "required": ["direction"],
                        "additionalProperties": False,
                    },
                    "annotations": {"readOnlyHint": False},
                    "_meta": {"capability": capability, "relativeTemperature": True, "proposalOnly": True},
                }
            )
    for document in docs:
        # Public documentation also exposes final execution bounds/units. The
        # interpretation schema intentionally permits missing slots for forms.
        document["executionSchema"] = TOOL_REGISTRY[document["_meta"]["capability"]].parameters.model_json_schema()
    return docs


def tool(name, description, properties=None, required=()):
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties or {},
                "required": list(required),
                "additionalProperties": False,
            },
        },
    }


def proposal_tools(documents, tasks, utterance):
    from src.vivi.dialogue.form_tools import FORM_CAPABILITIES

    forms = lifecycle_tools(documents, tasks, utterance=None)
    # Literal consent is handled before inference. Model-only consent remains
    # bound to the host's current question via dialogue_answer, never submit.
    forms = [t for t in forms if t["function"]["name"] != "form_submit"]
    tools = []
    for item in forms:
        function = item["function"]
        if function["name"] == "form_update":
            item = deepcopy(item)
            function = item["function"]
            for field in ("form_id", "revision"):
                function["parameters"]["properties"].pop(field, None)
                function["parameters"]["required"].remove(field)
        if function["name"] != "form_create":
            tools.append(item)
            continue
        schema = function["parameters"]
        for branch in schema.get("oneOf", [schema]):
            operation = branch["properties"]["operation"]["enum"][0]
            tools.append(
                {
                    "type": "function",
                    "function": {
                        "name": "form_create_" + operation,
                        "description": operation_description(operation) + " " + branch["description"],
                        "parameters": branch["properties"]["values"],
                    },
                }
            )
    for doc in documents:
        name, meta = doc["name"], doc["_meta"]
        if meta["capability"] in FORM_CAPABILITIES:
            continue
        tools.append(
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": doc["description"],
                    "parameters": doc["inputSchema"],
                },
            }
        )
    # An editable step gets a typed update tool bound on the host. No arbitrary
    # task IDs, completed steps or capabilities are accepted as a patch.
    for handle, task in [("active", tasks.get("active")), *[(t["handle"], t) for t in tasks.get("suspended", [])]]:
        if not task:
            continue
        for step in task.get("steps", []):
            if step["step_index"] not in task.get("editable_steps", []):
                continue
            if handle == "active" and current_form(tasks, documents) and step["step_index"] == task["current_step"]:
                continue
            doc = document_for_step(documents, step)
            if doc:
                tools.append(
                    {
                        "type": "function",
                        "function": {
                            "name": update_name(handle, step["step_index"]),
                            "description": "Sửa riêng bước " + str(step["step_index"]) + ": " + doc["description"],
                            "parameters": doc["inputSchema"],
                        },
                    }
                )
    from src.vivi.dialogue.clause_guard import action_clauses, greeting

    # A free-text reply was the small model's escape from commands ("đóng cốp
    # giúp tôi" -> "Cảm ơn bạn!"). Offer it only for a bare greeting/thanks; when
    # a control clause rides along ("chào bạn, mở cốp"), withhold reply so the
    # command cannot be dropped for a pleasantry.
    if utterance is None or (greeting(utterance) and not action_clauses(utterance)):
        tools.append(
            tool(
                "dialogue_reply",
                "Chỉ chào/cảm ơn; không thay cho lệnh xe hoặc câu hỏi.",
                {"text": {"type": "string", "maxLength": 120}},
                ["text"],
            )
        )
    tools += [
        tool(
            "dialogue_clarify",
            "Chỉ khi câu nói vô nghĩa/nghe không rõ, không tool nào phù hợp nghĩa. Câu lệnh hoặc câu hỏi rõ thì gọi tool tương ứng.",
            {"text": {"type": "string"}},
            ["text"],
        ),
        tool(
            "dialogue_unsupported",
            "Hiểu rõ nhưng không có chức năng. Không tự thay bằng thao tác khác.",
            {"text": {"type": "string"}},
            ["text"],
        ),
        tool(
            "dialogue_find_tools",
            "Lấy tài liệu tool còn thiếu. Chỉ gọi riêng, tối đa một lần.",
            {"query": {"type": "string", "minLength": 1, "maxLength": 200}},
            ["query"],
        ),
    ]
    question = tasks.get("pending_question")
    if question:
        props = {"value": {"type": "string", "maxLength": 200}}
        choices = [o["handle"] for o in question.get("choices", [])]
        choices += [key for key, field in [("yes", "accepts_yes"), ("no", "accepts_no")] if question.get(field)]
        if choices:
            props["option_id"] = {"type": "string", "enum": choices}
        tools.append(
            tool(
                "dialogue_answer",
                "Chỉ trả lời câu hỏi hiện hành. Không dùng cho yêu cầu mới hoặc câu vừa đồng ý vừa sửa.",
                props,
            )
        )
    handles = [t["handle"] for t in [tasks.get("active"), *tasks.get("suspended", [])] if t]
    if handles:
        tools.append(
            tool(
                "dialogue_task",
                "Hủy/tạm giữ/tiếp tục tác vụ đang được cung cấp.",
                {
                    "operation": {"type": "string", "enum": ["cancel", "suspend", "resume"]},
                    "task_ref": {"type": "string", "enum": handles},
                },
                ["operation", "task_ref"],
            )
        )
    for item in tools:
        function = item["function"]
        if not function["name"].startswith("dialogue_"):
            function["parameters"] = deepcopy(function["parameters"])
            function["parameters"]["properties"]["source_text"] = {
                "type": "string",
                "minLength": 1,
                "description": "Nguyên văn vế câu hiện tại mà thao tác này diễn giải. Không bỏ vế khác.",
            }
            function["parameters"].setdefault("required", []).append("source_text")
    from src.vivi.dialogue.small_tools import simplify_tools

    return simplify_tools(tools, utterance)


def operation_description(operation):
    return {
        "door_open": "MỞ cánh cửa cabin. Không mở khóa hay cửa kính.",
        "door_close": "ĐÓNG cánh cửa cabin.",
        "door_lock": "KHÓA tất cả hoặc cửa được nói.",
        "door_unlock": "MỞ KHÓA cửa; không mở cánh cửa.",
        "trunk_open": "MỞ cốp sau.",
        "trunk_close": "ĐÓNG cốp sau.",
        "hood_open": "MỞ nắp capo.",
        "hood_close": "ĐÓNG nắp capo.",
        "body_open": "MỞ mọi cửa, capo và cốp.",
        "body_close": "ĐÓNG mọi cửa, capo và cốp.",
        "media_play": "BẬT/PHÁT nhạc. Nhạc chung arguments={}; không dùng toàn bộ lệnh làm media_query.",
        "media_pause": "DỪNG/TẮT nhạc; arguments={}.",
        "climate_set_temperature": "ĐẶT nhiệt độ ĐÍCH được nói rõ.",
        "climate_adjust_temperature": "TĂNG/GIẢM tương đối; lạnh=increase, nóng=decrease; amount mặc định 2.",
    }.get(operation, "Đề xuất thao tác " + operation)


def update_name(handle, index):
    return "update_" + handle.replace(":", "_") + "_step_" + str(index)


def document_for_step(documents, step):
    """Find a bound step's schema even when its capability has intent aliases."""
    candidates = [
        d
        for d in documents
        if d["_meta"]["capability"] == step["capability"] and not d["_meta"].get("relativeTemperature")
    ]
    values = step.get("resolved_arguments") or step.get("arguments", {})
    return next(
        (
            d
            for d in candidates
            if all(values.get(key, value) == value for key, value in d["_meta"].get("constants", {}).items())
        ),
        candidates[0] if candidates else None,
    )


def canonical_arguments(values):
    from src.vivi.dialogue.form_tools import _canonical_location

    values = _canonical_location({k: v for k, v in values.items() if v is not None})
    if values.get("zone") is not None and values.get("zones") is not None:
        raise ValueError("Use zone OR zones")
    return values


def proposals_to_turn(calls, documents, tasks, utterance, tools, observation=None):
    """A valid model block vetoes the batch; validate every executable proposal."""
    if not 1 <= len(calls) <= 5:
        raise ValueError("One to five proposals required")
    offered = {t["function"]["name"]: t["function"]["parameters"] for t in tools}
    # A partial inability is a whole-request veto. Asking the model to repair
    # an action+block mixture can silently discard the block and execute only
    # the understood clause. Never reinterpret an explicit, valid block.
    blocks = [
        (name, args)
        for name, args in calls
        if name in {"dialogue_clarify", "dialogue_unsupported"}
        and name in offered
        and not list(Draft202012Validator(offered[name]).iter_errors(args))
    ]
    if blocks:
        calls = blocks[:1]
    for name, args in calls:
        if name not in offered:
            raise ValueError("Tool was not offered: " + name)
        errors = list(Draft202012Validator(offered[name]).iter_errors(args))
        if errors:
            raise ValueError("Invalid " + name + ": " + errors[0].message)
    from src.vivi.dialogue.small_tools import canonical_calls, merge_target_calls

    calls, tools = canonical_calls(merge_target_calls(calls), tools, utterance, observation)
    if any(name.startswith("dialogue_") for name, _ in calls):
        if len(calls) != 1:
            raise ValueError("Dialogue answers/blocks/discovery cannot mix with actions; preserve the whole request")
        name, args = calls[0]
        if name == "dialogue_answer":
            question = tasks.get("pending_question")
            if not question:
                raise ValueError("No current question")
            if question.get("purpose") == "approve_execution":
                # Short consent was already handled before inference. A new
                # operational sentence cannot become approval via model output.
                raise ValueError("Execution approval requires an explicit host-validated confirmation")
            return SemanticTurn(act="answer", question_answer={"question_id": question["id"], **args})
        if name == "dialogue_task":
            return SemanticTurn(act=args["operation"], task_ref=args["task_ref"])
        if name == "dialogue_find_tools":
            raise ValueError("Discovery must be resolved before interpretation")
        return SemanticTurn(act=name.removeprefix("dialogue_"), text=args["text"])
    docs = {d["name"]: d for d in documents}
    actions, patches, patch_handle = [], [], None
    for name, args in calls:
        args = dict(args)
        source = args.pop("source_text")
        implicit_window = name in {"form_create_raise_window", "form_create_lower_window"}
        # Explicit percentages need a spoken number; endpoints have their own tools.
        explicit_window = name == "form_create_window_position"
        if name.startswith("form_"):
            if name == "form_update":
                form = current_form(tasks, documents)
                if not form:
                    raise ValueError("No live form to update")
                args.update(form_id=form["form_id"], revision=form["revision"])
            decoded = decode_form_call([(name, args)], documents, tasks, utterance=utterance)
            if isinstance(decoded, SemanticTurn):
                if len(calls) != 1:
                    raise ValueError("Form updates are atomic")
                return decoded
            _, name, args = decoded[0]
        patch = None
        for task in [tasks.get("active"), *tasks.get("suspended", [])]:
            if task:
                patch = next(
                    ((task, s) for s in task.get("steps", []) if name == update_name(task["handle"], s["step_index"])),
                    None,
                )
                if patch:
                    break
        if patch:
            task, step = patch
            if patch_handle and patch_handle != task["handle"]:
                raise ValueError("Cannot update multiple tasks")
            patch_handle = task["handle"]
            doc = document_for_step(documents, step)
            if doc is None:
                raise ValueError("No documentation for the editable step")
        else:
            doc = docs[name]
        args = canonical_arguments(args)
        if not implicit_window:
            validate_literal_numbers(doc["_meta"]["capability"], args, utterance, strict_endpoints=explicit_window)
        if doc["_meta"].get("relativeTemperature"):
            args = resolve_relative_temperature(args["direction"], args.get("amount"), utterance)
        args.update(doc["_meta"].get("constants", {}))
        capability = doc["_meta"]["capability"]
        if capability == "navigation.start":
            raise ValueError("Navigation start requires explicit host-bound consent")
        if capability in {
            "conversation.respond",
            "weather.get_forecast",
            "web.search",
            "knowledge.lookup",
            "manual.search",
        }:
            args["query"] = source
        # A fresh request never inherits the previous draft's identity.
        if capability.startswith("navigation.") and capability.split(".")[1] in {
            "select",
            "refine",
            "start",
            "cancel",
            "decline",
        }:
            draft = (tasks.get("navigation") or {}).get("draft")
            if not draft:
                raise ValueError("No current navigation draft")
            args.update(plan_id=draft["id"], route_revision=draft["revision"])
        action = draft_from_values(capability, args)
        if patch:
            patches.append(StepUpdate(step_index=patch[1]["step_index"], action=action))
        else:
            actions.append(action)
    if patches:
        return SemanticTurn(act="modify", task_ref=patch_handle, step_updates=patches, append_actions=actions)
    readonly = all(
        TOOL_REGISTRY[a.capability].read_only and not a.capability.startswith("navigation.") for a in actions
    )
    return SemanticTurn(act="question" if readonly else "request", actions=actions)


def draft_from_values(capability, values):
    values = dict(values)
    values.pop("value_text", None)
    meta = {key: values.pop(key) for key in ("excluded_zones", "exclusion_mode", "referenced_target") if key in values}
    return ActionDraft(capability=capability, arguments=values, **meta)


# Hot/cold is unambiguous physics; a small model sometimes flips it. The host
# corrects the direction from a comfort word actually in the utterance, the way
# it rebuilds a target from words actually said, never guessing one.
COLD_WORDS = re.compile(r"(?<!\w)(?:lanh|ret|buot|lanh lanh)(?!\w)")
HOT_WORDS = re.compile(r"(?<!\w)(?:nong|oi buc|nuc|buc)(?!\w)")


def temperature_target(utterance):
    """The target temperature named by "lên/xuống/về/đến X độ", else None."""
    from src.vivi.text import normalize_text

    match = re.search(r"(?<!\w)(?:len|xuong|ve|den)\s+(\d+(?:[.,]\d+)?)\s*do", normalize_text(utterance))
    return float(match.group(1).replace(",", ".")) if match else None


def resolve_relative_temperature(direction, amount, utterance):
    """Map a hot/cold request to a target, a delta, or a question for the amount.

    "lên/xuống/về/đến X độ" names a target temperature (not a change); an
    explicit amount is a signed delta; a bare complaint ("tôi lạnh") carries only
    the direction, so the host asks how many degrees rather than guessing ±2.
    """
    from src.vivi.text import normalize_text

    target = temperature_target(utterance)
    if target is not None:
        return {"value_celsius": target}
    folded = normalize_text(utterance)
    cold, hot = COLD_WORDS.search(folded), HOT_WORDS.search(folded)
    if cold and not hot:
        direction = "increase"
    elif hot and not cold:
        direction = "decrease"
    if amount is None:
        return {"temperature_direction": direction}
    return {"temperature_delta_celsius": amount if direction == "increase" else -amount}


def validate_literal_numbers(capability, values, utterance, *, strict_endpoints=False):
    """Reject an invented literal, never choose or overwrite a model value."""
    field = {
        "climate.set_temperature": "value_celsius",
        "window.set_position": "position_percent",
        "seat.set_heat_level": "level",
    }.get(capability)
    if not field or field not in values:
        return
    # Window endpoints are declared operation constants, not literal numbers.
    if (
        capability == "window.set_position"
        and not strict_endpoints
        and values[field] in (0, 100)
        and not re.search(r"\d", utterance)
    ):
        return
    numbers = [float(n.replace(",", ".")) for n in re.findall(r"(?<!\w)\d+(?:[.,]\d+)?", utterance)]
    if numbers and float(values[field]) not in numbers:
        raise ValueError(f"Invented {field}={values[field]}; literal numbers in CURRENT utterance are {numbers}")
    if not numbers:
        evidence = values.get("value_text", "")
        # Accented words: folded "sau" (behind) and "nam" (south) are not numbers.
        number_words = r"(?<!\w)(?:một|hai|ba|bốn|tư|năm|sáu|bảy|tám|chín|mười|mươi|trăm|lăm|mốt)(?!\w)"
        if (
            not evidence
            or evidence.casefold() not in utterance.casefold()
            or not re.search(number_words, evidence.casefold())
        ):
            raise ValueError("A literal numeric value needs value_text from the current utterance")
