"""Short spoken labels; full vendor POIs and calculation data stay untouched."""

import math
import re
from collections import Counter

from src.vivi.navigation.destinations import administrative_match


def short_name(name):
    # VietMap may append the street to the business name. This only formats a
    # label; handles/coordinates still identify the original, complete POI.
    for match in re.finditer(r"\s+(?:Ngõ|Ngách|Đường|Phố|Hẻm)\s+", name, re.I):
        label = name[: match.start()].strip()
        if label and not re.fullmatch(
            r"(?:Quán(?: Cà Phê| Phở)?|Cà Phê|Caf[eé]|Nhà Thuốc|Hiệu Thuốc|Trạm Sạc)", label, re.I
        ):
            return label
    return name


def locality(address):
    parts = [part.strip() for part in address.split(",") if part.strip()]
    return next((part for part in parts if re.match(r"^(?:phường|xã|thị trấn)\b", part, re.I)), "")


def short_address(address):
    parts = [part.strip() for part in address.split(",") if part.strip()]
    area = locality(address)
    if area:
        return ", ".join(parts[: parts.index(area) + 1])
    return ", ".join(parts[:2])


def place_label(place, *, full_name=False):
    if administrative_match(place, place["name"]):
        name = place["name"]
        parent = next((b for b in place.get("boundaries", []) if b.get("type") == 0), None)
        if parent and parent.get("name", "").casefold() not in name.casefold():
            return f"{name}, {parent.get('full_name') or parent['name']}"
        return name
    name = place["name"] if full_name else short_name(place["name"])
    area = locality(place.get("address", ""))
    return f"{name}, {area}" if area and area.casefold() not in name.casefold() else name


def station_label(station):
    name = station["name"]
    rest = re.search(r"trạm dừng nghỉ\b", name, re.I)
    if rest:
        name = name[rest.start() :]
    elif station.get("rest_area_name"):
        name = "Trạm sạc gần " + station["rest_area_name"]
    return place_label({"name": name, "address": station.get("address", "")})


def route_summary(nav, *, ask_start=True):
    energy = nav.energy or {}
    plan = energy.get("selected_plan")
    stops = [
        {**s, "label": station_label({**s, "address": ""}), "minutes": math.ceil(s["charge_minutes"])}
        for s in (plan or {}).get("stops", [])
    ]
    seconds = (plan or {}).get("total_seconds", nav.route["duration_s"])
    minutes = math.ceil(seconds / 60)
    duration = f"{minutes // 60} giờ {minutes % 60} phút" if minutes >= 60 else f"{minutes} phút"
    label = nav.selected.get("spoken_label") or place_label(nav.selected)
    prefix = "Khứ hồi đến" if nav.trip_kind == "round_trip" else "Tuyến đến"
    speech = f"{prefix} {label}, khoảng {round(nav.route['distance_m'] / 1000)} km, {duration}."
    if plan:
        if not stops:
            speech += " Không cần dừng sạc."
        else:
            speech += f" Đã gồm thời gian sạc, dự tính dừng sạc {len(stops)} lần."
    else:
        speech += " " + energy.get("message", "")
    if ask_start:
        speech += " Bạn xác nhận bắt đầu nhé."
    return {
        "distance_m": nav.route["distance_m"],
        "total_seconds": seconds,
        "arrival_soc": math.floor(plan["arrival_soc"]) if plan else None,
        "stop_count": len(stops),
        "stops": stops,
        "speech": speech,
    }


def next_leg_message(nav):
    stops = ((nav.energy or {}).get("selected_plan") or {}).get("stops", [])
    if nav.stop_index < len(stops):
        stop = stops[nav.stop_index]
        return f"Điểm đến tiếp theo là trạm sạc thứ {nav.stop_index + 1}: {station_label(stop)}."
    if nav.trip_kind == "round_trip":
        traveled = 0
        for leg in ((nav.energy or {}).get("selected_plan") or {}).get("legs", []):
            traveled += leg["distance_m"]
            if leg.get("to") == "destination":
                if nav.progress_m >= traveled:
                    return "Tiếp tục lộ trình khứ hồi về điểm xuất phát."
                break
    label = nav.selected.get("spoken_label") or place_label(nav.selected)
    return f"Tiếp tục lộ trình đến {label}."


def charging_message(nav, *, resuming=False):
    stop = nav.energy["selected_plan"]["stops"][nav.stop_index]
    minutes = math.ceil(max(0, stop["charge_minutes"] * 60 - nav.charge_elapsed_s) / 60)
    prefix = "Tiếp tục sạc tại" if resuming else "Đã đến"
    return f"{prefix} trạm sạc thứ {nav.stop_index + 1}: {station_label(stop)}. Dự kiến dừng sạc khoảng {minutes} phút rồi đi tiếp."


def resume_message(nav):
    stops = ((nav.energy or {}).get("selected_plan") or {}).get("stops", [])
    if nav.status == "charging":
        return charging_message(nav, resuming=True) if nav.stop_index < len(stops) else "Tiếp tục sạc theo kế hoạch."
    message = "Đã chuyển D, tiếp tục hành trình."
    if stops:
        message += " " + next_leg_message(nav)
    return message


def choice_message(candidates):
    labels = [place_label(place) for place in candidates]
    counts = Counter(label.casefold() for label in labels)
    # Retain street-bearing names when shortening would hide the distinction
    # between branches. Choice numbers always refer to server-owned handles.
    labels = [
        place_label(place, full_name=True) if counts[label.casefold()] > 1 else label
        for place, label in zip(candidates, labels)
    ]
    if len(labels) == 1:
        return labels[0] + ". Đúng chỗ này chứ?"
    return "; ".join(f"{index + 1}: {label}" for index, label in enumerate(labels)) + ". Bạn chọn chỗ nào?"


def result_messages(results):
    """Suppress a repeated choice list by its typed POI identities, not text."""

    def choices(result):
        if not any(event.get("type") == "navigation.results" for event in result.get("ui_events", [])):
            return None
        candidates = (result.get("draft_update") or result.get("navigation_update") or {}).get("candidates", [])
        return tuple(place["handle"] for place in candidates) or None

    messages = []
    for index, result in enumerate(results):
        handles = choices(result)
        if handles and any(choices(later) == handles for later in results[index + 1 :]):
            continue
        if result.get("message"):
            messages.append(result["message"])
    return messages
