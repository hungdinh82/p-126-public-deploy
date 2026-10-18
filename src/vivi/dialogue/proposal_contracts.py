"""Conservative proposal constraints, never an action generator.

Vocabulary belongs to tool operations and measurements, not complete user
sentences. Ambiguous/unrecognized language leaves the model's choices intact.
"""

import re

from src.vivi.text import normalize_text

FAMILIES = {
    "door": {
        "open_door": ("mo",),
        "close_door": ("dong", "khep"),
        "lock_door": ("khoa",),
        "unlock_door": ("mo khoa", "bo khoa", "thao khoa"),
    },
    "window": {"lower_window": ("ha", "xuong", "mo"), "raise_window": ("nang", "len", "dong", "khep kin")},
    "music": {
        "play_music": ("bat", "mo", "phat", "play", "tiep tuc"),
        "pause_music": ("tat", "dung", "ngung", "ngung", "stop", "pause"),
    },
    "hood": {"open_hood": ("mo",), "close_hood": ("dong", "khep")},
    "trunk": {"open_trunk": ("mo",), "close_trunk": ("dong", "khep")},
    "body": {"open_body": ("mo",), "close_body": ("dong", "khep")},
    "temperature": {
        "increase_temperature": ("tang", "am hon"),
        "decrease_temperature": ("giam", "ha", "mat hon", "mat them", "bot"),
    },
}
SCOPES = {
    "window": r"\b(?:kinh|cua so)\b",
    "music": r"\b(?:nhac|music)\b",
    "hood": r"\b(?:capo|ca po)\b",
    "trunk": r"\bcop\b",
    "temperature": r"\b(?:nhiet do|dieu hoa|cabin|do)\b",
}
MEASUREMENTS = {
    "get_battery_level": r"\bpin\b",
    "get_tire_pressure": r"\b(?:lop|ap suat)\b",
    "get_remaining_range": r"\b(?:quang duong|bao xa)\b",
    "get_remaining_drive_time": r"\b(?:bao lau|so gio|thoi gian|may gio)\b",
    "get_vehicle_diagnostics": r"\bma loi\b",
    "get_cabin_temperature": r"\b(?:nhiet do|dieu hoa|cabin|may do)\b",
    "get_window_positions": r"\b(?:kinh|cua so)\b",
    "get_hood_status": r"\b(?:capo|ca po)\b",
    "get_trunk_status": r"\bcop\b",
    "get_door_status": r"\bcua\b",
}
READ_NAMES = set(MEASUREMENTS) | {"get_body_status", "get_vehicle_overview"}
SEPARATOR = re.compile(r"[,;]|\b(?:va|roi|sau do|xong|dong thoi|nhung)\b")


def replacement_text(text):
    """Only an explicit correction limits subsequent slot evidence."""
    folded = normalize_text(text)
    markers = list(
        re.finditer(r"\b(?:a khong|thoi doi sang|thoi (?=bat|tat|mo|dong|khoa|nang|ha))\b\s*[,;]?\s*", folded)
    )
    if markers and len(folded) == len(text):
        suffix = text[markers[-1].end() :].strip()
        if suffix:
            return suffix
    return text


def is_status_question(text):
    value = normalize_text(text)
    return bool(
        re.search(
            r"\b(?:trang thai|tinh trang|kiem tra|cho biet|toi muon xem|doc|do mo|may do|bao nhieu do|bao xa|bao lau|so gio|ma loi)\b"
            r"|\b(?:cua|kinh|cop|capo)\b.*\b(?:dang|da)\b|\bcua nao\b"
            r"|\b(?:cop|capo|cua|kinh)\s+(?:co\s+)?(?:mo|dong)\s+(?:khong|chua)\b",
            value,
        )
    )


def clauses(text):
    value = normalize_text(replacement_text(text))
    return [part.strip() for part in SEPARATOR.split(value) if part.strip()]


def family_clauses(text, family):
    result = []
    for part in clauses(text):
        window = bool(re.search(SCOPES["window"], part))
        if family == "door":
            relevant = bool(re.search(r"\bcua\b", part)) and not window and not re.search(r"\b(?:cop|capo)\b", part)
        elif family == "body":
            relevant = all(re.search(r"\b" + word + r"\b", part) for word in ("cua", "capo", "cop"))
        else:
            relevant = bool(re.search(SCOPES[family], part))
        if relevant:
            result.append(part)
    return result


def recognized_operations(part, operations):
    hits = [
        (m.start(), m.end(), name)
        for name, terms in operations.items()
        for term in terms
        for m in re.finditer(r"(?<!\w)" + re.escape(term) + r"(?!\w)", part)
    ]
    return {
        name
        for start, end, name in hits
        if not any(lo <= start and end <= hi and hi - lo > end - start for lo, hi, _ in hits)
    }


def permitted_tools(tools, text):
    """Remove only choices contradicted by explicit operation/measurement words."""
    blocked = set()
    question = is_status_question(text)
    for family, operations in FAMILIES.items():
        parts = family_clauses(text, family)
        recognized = [recognized_operations(part, operations) for part in parts]
        if (
            question
            and parts
            and all(is_status_question(part) or not names for part, names in zip(parts, recognized, strict=True))
        ):
            blocked.update(operations)
        elif recognized and all(len(names) == 1 for names in recognized):
            allowed = set().union(*recognized)
            blocked.update(set(operations) - allowed)
    window_parts = family_clauses(text, "window")
    if window_parts and all(re.search(r"%|\b(?:phan tram|mot nua|phan nua)\b", part) for part in window_parts):
        blocked.update({"raise_window", "lower_window"})
    value = normalize_text(text)
    if question:
        read_parts = [
            part
            for part in clauses(text)
            if is_status_question(part)
            or not any(recognized_operations(part, operations) for operations in FAMILIES.values())
        ]
        read_value = " ".join(read_parts)
        wanted = {name for name, pattern in MEASUREMENTS.items() if re.search(pattern, read_value)}
        if "get_window_positions" in wanted and not re.search(r"\bcanh cua\b", value):
            # "cửa kính" is one object; a separately named cabin door remains.
            without_windows = re.sub(r"\bcua\s+(?:kinh|so)\b", "kinh", value)
            if not re.search(r"\bcua\b", without_windows):
                wanted.discard("get_door_status")
        body = {"get_door_status", "get_hood_status", "get_trunk_status"}
        if body <= wanted:
            wanted = (wanted - body) | {"get_body_status"}
        if re.search(
            r"\b(?:tong quan|thong tin chung|thong tin (?:cua )?xe|tat ca thong tin|toan bo thong tin)\b", value
        ):
            wanted.add("get_vehicle_overview")
        if wanted:
            blocked.update(READ_NAMES - wanted)
    return [tool for tool in tools if tool["function"]["name"] not in blocked]


def continuation_stop_tools(text, capabilities):
    value = normalize_text(text)
    if not re.search(r"\b(?:dung|khong|cho)\b.+\bnua\b", value):
        return None
    names = {item["name"] for item in capabilities}
    if names and names <= {"media.play", "media.pause"}:
        return {"pause_music"}
    if names and names <= {"memory.remember", "memory.recall", "memory.forget", "memory.reset"}:
        return {"memory_reset"} if re.search(r"\b(?:het|tat ca|toan bo)\b", value) else {"memory_forget"}
    return None


def validate_operation_choices(calls, tools, text):
    allowed = {tool["function"]["name"] for tool in permitted_tools(tools, text)}
    declared = {tool["function"]["name"] for tool in tools}
    rejected = {name for name, _ in calls if name in declared and name not in allowed}
    if rejected:
        raise ValueError("Proposal trái với động từ/phạm vi thông số của câu hiện tại: " + ", ".join(sorted(rejected)))
    encoded = [repr((name, sorted(args.items()))) for name, args in calls]
    if len(encoded) != len(set(encoded)):
        raise ValueError("Proposal trùng lặp; chỉ giữ mỗi thao tác được yêu cầu một lần")
