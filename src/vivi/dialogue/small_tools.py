"""Small model-facing operations; domain DTOs stay behind the adapter.

Each operation exposes only its own values. Target spelling, signed deltas,
form identities and execution booleans are translated by deterministic bindings.
"""

import re
import unicodedata
from copy import deepcopy

ZONES = ("driver", "front_passenger", "rear_left", "rear_right")
TARGETS = (*ZONES, "all", "left", "right", "front", "rear", "same")
# Spoken phrase first, then value: a small model looks up the phrase it heard.
TARGET_GUIDE = (
    "targets: chỉ nói bên trái/bên phải=left/right; chỉ nói trước/sau=front/rear; "
    "ghế lái/cửa lái/bên tài/bên lái=driver; ghế phụ/bên phụ=front_passenger; "
    "sau trái/sau bên trái=rear_left; sau phải/sau bên phải=rear_right; "
    "tất cả/mọi cửa=all; bên đó=same. Không nói vị trí: []. Không tự thêm vị trí. "
    '"hạ hết/mở hết" là mức mở hoàn toàn, không phải all.'
)
# A concrete door is (side, row); the words that evidence each dimension, plus
# the names that pick a seat outright. A 1.7B model over-specifies ("bên trái"
# -> rear_left); the host keeps only the evidenced dimension (-> left), which
# removes a hallucinated row/seat without ever inventing a position.
ZONE_COORDS = {
    "driver": ("left", "front"),
    "front_passenger": ("right", "front"),
    "rear_left": ("left", "rear"),
    "rear_right": ("right", "rear"),
}
SIDE_EVIDENCE = {"left": r"\btrai\b", "right": r"\bphai\b"}
ROW_EVIDENCE = {"front": r"\btruoc\b", "rear": r"\bsau\b"}
ZONE_DIRECT = {
    "driver": r"\b(?:lai|tai xe|ben tai|ghe lai)\b",
    "front_passenger": r"\b(?:phu|hanh khach)\b",
}
COUNT_WORDS = {"một": 1, "hai": 2, "ba": 3, "bốn": 4}
NUMBER_WORDS = r"(?<!\w)(?:một|hai|ba|bốn|tư|năm|sáu|bảy|tám|chín|mười|mươi|trăm|lăm)(?!\w)"


def target_schema(*, remaining=False):
    values = [*TARGETS, "remaining"] if remaining else list(TARGETS)
    return {"type": "array", "items": {"type": "string", "enum": values}, "maxItems": 4}


def number_schema(description, minimum, maximum, *, nullable=False, integer=False):
    schema = {"type": "integer" if integer else "number", "minimum": minimum, "maximum": maximum}
    if nullable:
        schema = {"anyOf": [schema, {"type": "null"}]}
    return {**schema, "description": description}


# Canonical lifecycle tools are host implementation details, not the public
# model vocabulary. Each entry is (name, description, parameter schema, binding).
def operation_variants(function, *, door_filter=False):
    name = function["name"]
    zoned = "zone" in function["parameters"].get("properties", {})
    simple = {
        # "Remaining" doors are a filter the host resolves from telemetry.
        "form_create_door_open": (
            "open_door",
            'Mở cánh cửa cabin; khác mở khóa và cửa kính. Các cửa còn lại/đang đóng: targets=["remaining"].',
        ),
        "form_create_door_close": (
            "close_door",
            'Đóng cánh cửa cabin. Các cửa còn lại/đang mở: targets=["remaining"].',
        ),
        "form_create_door_lock": ("lock_door", "Khóa cửa cabin."),
        "form_create_door_unlock": ("unlock_door", "Mở khóa cửa cabin; không mở cánh cửa."),
        "form_create_trunk_open": ("open_trunk", "Mở cốp sau, không phải cửa ghế sau."),
        "form_create_trunk_close": ("close_trunk", "Đóng cốp sau."),
        "form_create_hood_open": ("open_hood", "Mở nắp capo phía trước."),
        "form_create_hood_close": ("close_hood", "Đóng nắp capo phía trước."),
        "form_create_body_open": ("open_body", "Mở đồng thời mọi cửa cabin, capo và cốp. Chỉ khi yêu cầu đủ ba loại."),
        "form_create_body_close": ("close_body", "Đóng đồng thời mọi cửa cabin, capo và cốp."),
        "form_create_raise_window": ("raise_window", "Lên/nâng/đóng kính hoàn toàn. Không dùng khi có phần trăm."),
        "form_create_lower_window": ("lower_window", "Xuống/hạ/mở kính hoàn toàn. Có phần trăm dùng set_window."),
        "form_create_media_pause": ("pause_music", "Tắt/dừng/tạm dừng nhạc. Không điều khiển loa trả lời của trợ lý."),
    }
    if name in simple:
        title, description = simple[name]
        remaining = door_filter and name in {"form_create_door_open", "form_create_door_close"}
        fields = {"targets": target_schema(remaining=remaining)} if zoned else {}
        binding = {"targets": zoned}
        if remaining:
            binding["remaining"] = name.removeprefix("form_create_door_")
        yield title, description, fields, binding
    elif name == "form_create_media_play":
        yield "play_music", "Bật/phát/tiếp tục nhạc chung; không có tên bài hoặc thể loại.", {}, {}
        yield (
            "play_requested_music",
            "Phát nhạc có tên bài, ca sĩ hoặc thể loại được nói rõ.",
            {
                "query": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 200,
                    "description": "Chỉ tên bài/ca sĩ/thể loại được yêu cầu.",
                },
            },
            {"rename": {"query": "media_query"}},
        )
    elif name == "form_create_climate_set_temperature":
        yield (
            "set_temperature",
            "Đặt nhiệt độ ĐÍCH. 'lên 25 độ' là đích 25; không dùng cho than nóng/lạnh.",
            {
                "celsius": number_schema("Nhiệt độ đích °C được nói; chưa biết: null.", 16, 30, nullable=True),
            },
            {"rename": {"celsius": "value_celsius"}},
        )
    elif name == "form_create_climate_adjust_temperature":
        for operation, direction, description in [
            (
                "increase_temperature",
                "increase",
                "Khi thấy lạnh (kể cả hơi lạnh/lành lạnh) hoặc muốn ấm/tăng nhiệt THÊM.",
            ),
            (
                "decrease_temperature",
                "decrease",
                "Khi thấy nóng (kể cả hơi nóng/oi) hoặc muốn mát/giảm nhiệt BỚT.",
            ),
        ]:
            yield (
                operation,
                description,
                {
                    "degrees": {
                        "anyOf": [{"type": "number", "exclusiveMinimum": 0, "maximum": 30}, {"type": "null"}],
                        "description": "Số độ thay đổi nếu người dùng nói rõ; không nói mức thì null (backend sẽ hỏi).",
                    },
                },
                {"rename": {"degrees": "amount"}, "constants": {"direction": direction}},
            )
    elif name == "form_create_window_position":
        yield (
            "set_window",
            "Đặt phần trăm ĐỘ MỞ kính được nói rõ; 0=đóng, 100=mở hoàn toàn.",
            {
                "targets": target_schema(),
                "percent": number_schema(
                    "Phần trăm mở kính được nói, không phải chiều cao kính.", 0, 100, integer=True
                ),
            },
            {"targets": True, "rename": {"percent": "position_percent"}},
        )
    elif name == "form_create_seat_set_heat_level":
        yield (
            "set_seat_heat",
            "Đặt sưởi ghế. 0=tắt, 1–3=mức sưởi; khác nhiệt độ điều hòa.",
            {
                "targets": target_schema(),
                "level": number_schema(
                    "Mức sưởi ghế; chưa biết: null để backend hỏi.", 0, 3, nullable=True, integer=True
                ),
            },
            {"targets": True},
        )
    elif name in {"navigation_plan_directions", "navigation_plan_itinerary"}:
        directions = name.endswith("directions")
        operation = "get_directions" if directions else "calculate_trip"
        description = (
            "CHỈ ĐƯỜNG tới điểm đến, chưa khởi hành." if directions else "Chỉ TÍNH HÀNH TRÌNH, chưa khởi hành."
        )
        destination = {"type": ["string", "null"], "maxLength": 200, "description": "Địa điểm được nói; thiếu: null."}
        for round_trip in (False, True):
            title = operation + ("_round_trip" if round_trip else "")
            constants = {"trip_kind": "round_trip" if round_trip else "one_way"}
            yield (
                title,
                description + (" Khứ hồi." if round_trip else " Một chiều."),
                {
                    "destination": destination,
                },
                {"rename": {"destination": "query"}, "constants": constants},
            )
            yield (
                title + "_from",
                description + " Có nơi xuất phát được nói rõ.",
                {
                    "origin": {"type": "string", "minLength": 2, "maxLength": 200},
                    "destination": destination,
                },
                {
                    "rename": {"destination": "query", "origin": "origin_query"},
                    "constants": constants,
                    "needs_origin": True,
                },
            )
    else:
        yield from generic_variants(function)


def generic_variants(function):
    name, schema = function["name"], deepcopy(function["parameters"])
    properties = schema.get("properties", {})
    binding = {}
    # Context-bound updates expose the changed values directly, not values/ID/
    # revision wrappers. A waiting-slot form cannot flip the known operation.
    if name == "form_update":
        properties = deepcopy(properties["values"]["properties"])
        binding["update"] = True
        properties.pop("open", None)
        properties.pop("locked", None)
    targets = bool(set(properties).intersection({"zone", "zones", "target_side", "target_row"}))
    if targets:
        for key in (
            "zone",
            "zones",
            "target_side",
            "target_row",
            "excluded_zones",
            "exclusion_mode",
            "referenced_target",
        ):
            properties.pop(key, None)
        properties["targets"] = target_schema()
        binding["targets"] = True
    for key in ("source_text", "location_text", "value_text"):
        properties.pop(key, None)
    for canonical, short in (("value_celsius", "celsius"), ("position_percent", "percent")):
        if canonical in properties:
            properties[short] = properties.pop(canonical)
            binding.setdefault("rename", {})[short] = canonical
    # Query is already attached from the request's source text by the host.
    if name in {"conversation_respond", "web_search", "knowledge_lookup", "manual_search", "weather_get_forecast"}:
        properties.pop("query", None)
    description = function["description"]
    if name == "form_update":
        name, description = (
            "update_current_request",
            "Điền/sửa thông tin của yêu cầu hiện hành, giữ nguyên các trường không đổi.",
        )
    # Normalized names and two fields suffice for ordinary geographic queries.
    descriptions = {
        "conversation_respond": "Trò chuyện, kể chuyện, làm thơ, giới thiệu bản thân trợ lý, giờ/thứ hiện tại. Không thay cho lệnh xe.",
        "web_search": "Tra thông tin hiện tại có nguồn: tin tức, giá, lịch hôm nay, du lịch. Không chỉ đường.",
        "knowledge_lookup": "Tra kiến thức nền: lịch sử, văn hóa, địa danh; không tra thông tin hiện tại.",
        "weather_get_forecast": "Hỏi thời tiết: giữ địa điểm và ngày được hỏi. Không phải nhiệt độ cabin.",
        "manual_search": "Tra cẩm nang/hướng dẫn/thông số xe, giữ model/năm/AC-DC khi có yêu cầu.",
        "navigation_plan_directions": "Chuẩn bị tuyến CHỈ ĐƯỜNG. query=địa điểm; origin_query chỉ khi nói nơi xuất phát. Chưa khởi hành.",
        "navigation_plan_itinerary": "TÍNH HÀNH TRÌNH, chưa khởi hành. Giữ nơi xuất phát/đích và khứ hồi khi được nói.",
    }
    yield name, descriptions.get(name, description), properties, binding


def simplify_tools(canonical_tools, utterance):
    """Build independent closed schemas; keep implementation metadata off wire."""
    from src.vivi.text import normalize_text

    folded = normalize_text(utterance)
    exclusion_requested = "tru" in folded.split()
    # Like exclusions, the state filter is offered only when a filter is said;
    # otherwise "remaining" attracted plain "đóng cửa" requests.
    spoken_amount = bool(
        re.search(r"\d|%|phần trăm|nửa|" + NUMBER_WORDS, unicodedata.normalize("NFC", utterance).casefold())
    )
    door_filter = bool(re.search(r"\b(?:con lai|dang mo|dang dong|chua dong|chua mo|cua khac)\b", folded))
    tools = []
    for canonical in canonical_tools:
        function = canonical["function"]
        if function["name"].startswith("dialogue_"):
            if function["name"] in {"dialogue_clarify", "dialogue_unsupported"}:
                message = (
                    "Bạn nói rõ yêu cầu muốn thực hiện nhé."
                    if function["name"] == "dialogue_clarify"
                    else "Mình chưa hỗ trợ đầy đủ yêu cầu này."
                )
                tools.append(
                    {
                        "type": "function",
                        "function": {
                            "name": function["name"],
                            "description": function["description"],
                            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
                        },
                        "_binding": {"canonical": function, "dialogue_text": message},
                    }
                )
                continue
            tools.append(canonical)
            continue
        for name, description, fields, binding in operation_variants(function, door_filter=door_filter):
            if binding.get("needs_origin") and "tu" not in normalize_text(utterance).split():
                continue
            if name == "set_window" and not spoken_amount:
                # Without a spoken percentage the endpoint tools carry the meaning;
                # offering set_window invited invented 0/100 values.
                continue
            fields = deepcopy(fields)
            schema = {
                "type": "object",
                "properties": fields,
                "required": list(fields)
                if function["name"].startswith("form_create_") or "destination" in fields
                else [],
                "additionalProperties": False,
            }
            if binding.get("update") or function["name"].startswith("update_"):
                schema["minProperties"] = 1
            tools.append(
                {
                    "type": "function",
                    "function": {"name": name, "description": description, "parameters": schema},
                    "_binding": {**binding, "canonical": function},
                }
            )
            if binding.get("targets") and exclusion_requested:
                extended = deepcopy(tools[-1])
                extended["function"]["name"] += "_except"
                extended["function"]["description"] += " Trừ các vị trí được nói rõ."
                extended["function"]["parameters"]["properties"]["except_targets"] = {
                    "type": "array",
                    "items": {"type": "string", "enum": list(ZONES)},
                    "minItems": 1,
                    "maxItems": 4,
                }
                extended["function"]["parameters"]["required"].append("except_targets")
                extended["_binding"]["exclusion"] = True
                tools.append(extended)
    return merge_information_tools(tools)


def merge_information_tools(tools):
    """One lookup tool: the information policy, not a 1.7B model, picks the source.

    Web and encyclopedia differ by freshness, a distinction the small model got
    wrong; the host already reroutes by the requested fact in information.policy.
    """
    sources = {t["function"]["name"]: t for t in tools if t["function"]["name"] in {"web_search", "knowledge_lookup"}}
    if len(sources) < 2:
        return tools
    merged = {
        "type": "function",
        "function": {
            "name": "lookup_information",
            "description": "Tra cứu thông tin có nguồn: tin tức, giá cả, ngày âm lịch, sự kiện, du lịch, lịch sử, "
            "văn hóa, địa danh, kiến thức. Không phải trò chuyện/sáng tác, chỉ đường hay thời tiết.",
            "parameters": {"type": "object", "properties": {}, "required": [], "additionalProperties": False},
        },
        "_binding": {
            "canonical": sources["web_search"]["_binding"]["canonical"],
            "information": {
                "web.search": sources["web_search"]["_binding"]["canonical"],
                "knowledge.lookup": sources["knowledge_lookup"]["_binding"]["canonical"],
            },
        },
    }
    position = min(tools.index(t) for t in sources.values())
    rest = [t for t in tools if t["function"]["name"] not in sources]
    return [*rest[:position], merged, *rest[position:]]


def normalize_targets(targets, utterance):
    """Keep only the target dimensions the utterance evidences; never add one.

    Each door the model names is rebuilt from what the utterance actually says:
    a seat named outright stays; a concrete seat whose two dimensions both match
    the spoken side/row stays; otherwise it is rebuilt from the side/row words
    the utterance does contain. This drops a hallucinated dimension AND corrects
    a mis-picked one ("phía trước bên trái" -> model rear_left -> front+left),
    always using only words literally present, never a position the user did not
    give. A target with no supporting word is rejected so repair can retry.
    """
    from src.vivi.text import normalize_text

    folded = normalize_text(utterance)
    said_side = next((s for s, pat in SIDE_EVIDENCE.items() if re.search(pat, folded)), None)
    said_row = next((r for r, pat in ROW_EVIDENCE.items() if re.search(pat, folded)), None)
    resolved = []
    for target in targets:
        # A bare side/row the model guessed ("mở cửa sổ" -> front) is dropped
        # when unsaid, so the form asks for the location instead of acting on it.
        if target in SIDE_EVIDENCE:
            if said_side == target:
                resolved.append(target)
            continue
        if target in ROW_EVIDENCE:
            if said_row == target:
                resolved.append(target)
            continue
        if target not in ZONE_COORDS:
            resolved.append(target)
            continue
        if ZONE_DIRECT.get(target) and re.search(ZONE_DIRECT[target], folded):
            resolved.append(target)
            continue
        side, row = ZONE_COORDS[target]
        if said_side == side and said_row == row:
            resolved.append(target)
            continue
        spoken = [dim for dim in (said_side, said_row) if dim]
        if not spoken:
            raise ValueError(
                f"Vị trí '{target}' không có trong câu. Chỉ nói bên dùng left/right; "
                "chỉ nói hàng dùng front/rear; không nói vị trí dùng []."
            )
        resolved.extend(spoken)
    return list(dict.fromkeys(resolved))


def decode_targets(targets):
    if not targets:
        return {}
    if len(set(targets)) != len(targets):
        raise ValueError("Duplicate targets")
    if targets == ["same"]:
        return {"referenced_target": True}
    if targets == ["all"]:
        return {"zone": "all"}
    if all(target in ZONES for target in targets):
        return {"zone": targets[0]} if len(targets) == 1 else {"zones": targets}
    side = [t for t in targets if t in {"left", "right"}]
    row = [t for t in targets if t in {"front", "rear"}]
    if len(side) > 1 or len(row) > 1 or len(side) + len(row) != len(targets):
        raise ValueError("Use concrete targets OR one side/row; all/same must be alone")
    return {**({"target_side": side[0]} if side else {}), **({"target_row": row[0]} if row else {})}


def merge_target_calls(calls):
    """Join same-operation calls that differ only by concrete zones.

    "mở cửa lái và cửa phụ" as two open_door calls is one door request; merging
    keeps one consent form instead of two. Nothing else is rewritten.
    """
    merged = []
    for name, arguments in calls:
        targets = arguments.get("targets")
        rest = {k: v for k, v in arguments.items() if k != "targets"}
        previous = next(
            (
                item
                for item in merged
                if item[0] == name
                and isinstance(targets, list)
                and targets
                and set(targets) <= set(ZONES)
                and isinstance(item[1].get("targets"), list)
                and item[1]["targets"]
                and set(item[1]["targets"]) <= set(ZONES)
                and {k: v for k, v in item[1].items() if k != "targets"} == rest
            ),
            None,
        )
        if previous:
            previous[1]["targets"] = list(dict.fromkeys([*previous[1]["targets"], *targets]))
        else:
            merged.append((name, dict(arguments)))
    return merged


def resolve_remaining(operation, observation, utterance):
    """Doors not yet in the requested state, computed from fresh telemetry only."""
    import re

    from src.vivi.dialogue.clause_guard import HostTurn, spelling

    doors = (observation or {}).get("door_open")
    if not isinstance(doors, dict) or not all(isinstance(doors.get(zone), bool) for zone in ZONES):
        raise HostTurn("clarify", "Mình chưa đọc được trạng thái cửa. Bạn nói rõ cửa nào nhé.", "remaining_no_state")
    wanted = operation == "open"
    zones = [zone for zone in ZONES if doors[zone] != wanted]
    if not zones:
        state = "mở" if wanted else "đóng"
        raise HostTurn("reply", f"Các cửa đều đã {state} rồi.", "remaining_none")
    count = re.search(r"(?<!\w)(\d|một|hai|ba|bốn)\s+cửa", spelling(utterance))
    if count:
        spoken = int(count.group(1)) if count.group(1).isdigit() else COUNT_WORDS[count.group(1)]
        if spoken != len(zones):
            raise HostTurn(
                "clarify",
                f"Mình thấy có {len(zones)} cửa cần {'mở' if wanted else 'đóng'}, khác số bạn nói. Bạn nói rõ cửa nào nhé.",
                "remaining_count_mismatch",
            )
    return {"zone": "all"} if len(zones) == len(ZONES) else {"zone": zones[0]} if len(zones) == 1 else {"zones": zones}


def canonical_calls(calls, tools, utterance, observation=None):
    """Return host calls and schemas after model-facing validation succeeds."""
    offered = {t["function"]["name"]: t for t in tools}
    decoded, canonical = [], []
    for name, arguments in calls:
        spec = offered[name]
        binding = spec.get("_binding")
        if not binding:
            decoded.append((name, arguments))
            canonical.append(spec)
            continue
        original = binding["canonical"]
        if "information" in binding:
            from src.vivi.information.policy import information_decision

            decision = information_decision(utterance)
            source = decision.capability if decision and decision.capability in binding["information"] else None
            # Undecided questions use cited web search, which also covers stable facts.
            original = binding["information"][source or "web.search"]
        if "dialogue_text" in binding:
            decoded.append((original["name"], {"text": binding["dialogue_text"]}))
            canonical.append({"type": "function", "function": original})
            continue
        values = dict(arguments)
        text = utterance
        targets = values.pop("targets", [])
        excluded = values.pop("except_targets", None)
        values = {
            binding.get("rename", {}).get(k, k): v
            for k, v in values.items()
            if v is not None or binding.get("rename", {}).get(k, k) == "value_celsius"
        }
        values.update(binding.get("constants", {}))
        if binding.get("constants", {}).get("direction"):
            from src.vivi.dialogue.local_tools import temperature_target, validate_literal_numbers

            if temperature_target(utterance) is not None:
                # "lên/xuống X độ": the number is a target, resolved downstream;
                # any amount the model also emitted is spurious, so drop it.
                values.pop("amount", None)
            elif values.get("amount") == 2 and not re.search(r"\d", utterance):
                # The unspoken default restated by the model is no new value.
                values.pop("amount")
            if "amount" in values:
                try:
                    validate_literal_numbers(
                        "climate.set_temperature",
                        {"value_celsius": values["amount"], "value_text": utterance},
                        utterance,
                    )
                except ValueError as exc:
                    raise ValueError(str(exc).replace("value_celsius", "degrees")) from exc
        if binding.get("remaining") and "remaining" in targets:
            if targets != ["remaining"]:
                raise ValueError("remaining must be the only target")
            values.update(resolve_remaining(binding["remaining"], observation, utterance))
        elif binding.get("targets"):
            values.update(decode_targets(normalize_targets(targets, text)))
        if excluded:
            values["excluded_zones"] = excluded
        properties = original["parameters"]["properties"]
        fields = properties.get("values", {}).get("properties", {}) if binding.get("update") else properties
        if "location_text" in fields:
            values["location_text"] = text if targets else None
        if "value_text" in fields:
            values["value_text"] = text
        if "zone" in original["parameters"].get("required", []) and "zone" not in values:
            values["zone"] = None
        args = {"values": values} if binding.get("update") else values
        args["source_text"] = text
        decoded.append((original["name"], args))
        canonical.append({"type": "function", "function": original})
    return decoded, canonical
