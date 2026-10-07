"""Brief spoken replies derived exclusively from verified vehicle results."""

import re

from src.vivi.agents.contracts import ActionProposal
from src.vivi.text import normalize_text
from src.vivi.vehicle.zones import zoned_noun


def status_reply(query: str, state: dict) -> str:
    text = normalize_text(query)
    pieces = []
    if "pin" in text and "nhiet do" in text:
        return "Mình chưa đọc được nhiệt độ pin."
    if "pin" in text or not re.search(r"dieu hoa|nhiet do|ap suat|cua|ghe|nhac|quang duong|bao xa|di duoc", text):
        value = state.get("battery_percent")
        pieces.append(f"Pin của mình còn {value:g} phần trăm." if value is not None else "Mình chưa đọc được mức pin.")
    if re.search(r"quang duong|bao xa|di duoc|di them", text):
        value = state.get("range_km")
        pieces.append(f"Quãng đường còn lại ước tính là {value:g} km." if value is not None else "Mình chưa đọc được quãng đường còn lại.")
    if re.search(r"dieu hoa|nhiet do", text):
        if re.search(r"\b(bat|tat)\b", text):
            return "Mình chưa đọc được trạng thái bật hay tắt điều hoà."
        value = state.get("temperature_celsius")
        pieces.append(f"Điều hoà đang đặt ở {value:g} độ." if value is not None else "Mình chưa đọc được nhiệt độ điều hoà.")
    if "ap suat" in text:
        pressures = state.get("tire_pressures_kpa") or {}
        labels = {"front_left": "trước trái", "front_right": "trước phải", "rear_left": "sau trái", "rear_right": "sau phải"}
        values = [f"{label} {pressures[key]:g}" for key, label in labels.items() if pressures.get(key) is not None]
        pieces.append("Áp suất lốp: " + ", ".join(values) + " kPa." if values else "Mình chưa đọc được áp suất lốp.")
    if re.search(r"cua|ghe", text):
        # A requested side limits the readout; an unspecified side must not be silently assumed.
        from src.vivi.agents.classifier import RulesIntentClassifier

        zone = RulesIntentClassifier._cabin_zone(text)
        zones = [zone] if zone and zone != "all" else ["driver", "front_passenger", "rear_left", "rear_right"]
        labels = {"driver": "bên tài", "front_passenger": "bên phụ", "rear_left": "sau trái", "rear_right": "sau phải"}
        for key in zones:
            if "cua so" in text or "kinh" in text:
                value = (state.get("window_positions") or {}).get(key)
                if value is not None:
                    position = "đang đóng" if value == 0 else "đang mở hết" if value == 100 else f"đang mở {value:g} phần trăm"
                    pieces.append(f"Cửa sổ {labels[key]} {position}.")
            elif "cua" in text:
                door = (state.get("door_states") or {}).get(key) or {}
                field = "locked" if "khoa" in text else "open"
                if field in door:
                    condition = ("đã khoá" if door[field] else "chưa khoá") if field == "locked" else ("đang mở" if door[field] else "đang đóng")
                    pieces.append(f"Cửa {labels[key]} {condition}.")
            elif "ghe" in text:
                value = (state.get("seat_heat_levels") or {}).get(key)
                if value is not None:
                    pieces.append(f"Sưởi ghế {labels[key]} đang ở mức {value:g}.")
    if "nhac" in text and "media_playing" in state:
        pieces.append("Nhạc đang bật." if state["media_playing"] else "Nhạc đang dừng.")
        if re.search(r"bai|nhac gi", text):
            pieces.append("Mình chưa đọc được tên bài hát.")
    return " ".join(pieces) or "Mình chưa đọc được trạng thái bạn hỏi."


def verified_action_reply(proposal: ActionProposal, query: str, state: dict, fallback: str) -> str:
    args = proposal.arguments
    if proposal.intent == "vehicle.get_status":
        return status_reply(query, state)
    if proposal.intent == "climate.set_temperature":
        return f"Mình đã đặt điều hoà ở {float(args['value_celsius']):g} độ nhé."
    if proposal.intent == "window.set_position":
        position = int(args["position_percent"])
        target = zoned_noun("cửa sổ", args)
        if position in {0, 100}:
            return f"Mình đã {'đóng' if position == 0 else 'mở hết'} {target} nhé."
        return f"Mình đã mở {target} ở mức {position} phần trăm."
    if proposal.intent == "door.set_lock":
        return f"Mình đã {'khoá' if args['locked'] else 'mở khoá'} {zoned_noun('cửa', args)} nhé."
    if proposal.intent == "door.set_open":
        return f"Mình đã {'mở' if args['open'] else 'đóng'} {zoned_noun('cửa', args)} nhé."
    if proposal.intent == "seat.set_heat_level":
        return f"Mình đã đặt {zoned_noun('sưởi ghế', args)} ở mức {int(args['level'])}."
    if proposal.intent == "media.play":
        return "Mình đã bật nhạc nhé."
    if proposal.intent == "media.pause":
        return "Mình đã dừng nhạc nhé."
    return fallback
