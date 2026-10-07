"""Cabin command parsers. Knowledge/status/dialogue decisions belong to the planner."""

from __future__ import annotations

import re
from typing import Any

from src.vivi.agents.contracts import IntentDecision
from src.vivi.agents.slots import cabin_zone, temperature_number
from src.vivi.text import normalize_text


class CabinCommandParser:
    def parse(self, input_text: str, vehicle_state: dict[str, Any] | None) -> IntentDecision | None:
        text = normalize_text(input_text)
        climate_request = re.search(
            r"nhiet do|dieu hoa|nong|lanh|mat hon|am hon|"
            r"\b(?:tang|giam|ha|them|bot)\b.{0,20}(?:do|°)",
            text,
        )
        if climate_request:
            if re.search(r"\b(bat|tat)\b", text) and not re.search(r"\d|\b(?:mot|hai|ba|bon|nam)\b", text):
                return IntentDecision(
                    route="unsupported",
                    intent="unsupported.request",
                    response_text="Mình chưa bật hay tắt điều hoà bằng giọng nói được. Mình có thể chỉnh nhiệt độ.",
                )
            amount = temperature_number(text)
            if re.search(r"toi da|cao nhat|max\b", text):
                amount = 30.0
            elif re.search(r"toi thieu|thap nhat|min\b", text):
                amount = 16.0
            relative_cue = bool(
                re.search(r"\b(tang|giam|ha|them|bot)\b", text)
                or re.search(r"\b(hoi|qua|them)\s+(lanh|nong)\b|\b(mat|am)\s+hon\b", text)
                or (amount is None and re.search(r"\b(lanh|nong)\b", text))
            )
            # A number in the valid cabin range is overwhelmingly a target
            # ("tăng lên 25 độ"), while small numbers are deltas
            # ("tăng thêm 2 độ").
            explicit_delta = bool(re.search(r"\b(?:them|bot)\b|\b(?:tang|giam|ha)\s+(?:di\s+)?\d", text))
            explicit_target = bool(
                re.search(r"\b(?:len|xuong|ve|den)\s+(?:muc\s+)?\d|toi da|toi thieu|cao nhat|thap nhat", text)
            )
            relative = relative_cue and not explicit_target and (explicit_delta or amount is None or amount < 16)
            if amount is None and not relative:
                return IntentDecision(
                    route="clarify",
                    intent="conversation.clarify",
                    needs_clarification=True,
                    clarification_question="Bạn muốn đặt nhiệt độ bao nhiêu?",
                    response_text="Bạn muốn đặt nhiệt độ bao nhiêu?",
                    follow_up={"intent": "climate.set_temperature", "missing_slot": "value_celsius"},
                )
            if relative:
                if vehicle_state is None or vehicle_state.get("temperature_celsius") is None:
                    return IntentDecision(
                        route="clarify",
                        intent="conversation.clarify",
                        needs_clarification=True,
                        clarification_question="Bạn muốn đặt điều hoà bao nhiêu độ?",
                        follow_up={"intent": "climate.set_temperature", "missing_slot": "value_celsius"},
                    )
                current = float(vehicle_state["temperature_celsius"])
                delta = amount if amount is not None else 2.0
                lower = bool(re.search(r"\b(giam|ha|bot)\b|\bmat hon\b", text)) or (
                    not re.search(r"\b(tang|them)\b|\bam hon\b", text) and "nong" in text
                )
                value = min(30.0, max(16.0, current + (-delta if lower else delta)))
            else:
                value = amount
            assert value is not None
            return IntentDecision(
                route="action",
                intent="climate.set_temperature",
                arguments={"value_celsius": value},
                response_text=f"Mình sẽ đặt điều hoà ở {value:g} độ nhé.",
            )
        if re.search(r"cua so|cua kinh|\bkinh\b", text):
            opening = bool(re.search(r"\b(mo|ha)\b", text))
            closing = bool(re.search(r"\b(dong|len)\b", text))
            if opening == closing:
                return IntentDecision(
                    route="clarify",
                    intent="conversation.clarify",
                    needs_clarification=True,
                    clarification_question="Bạn muốn mở hay đóng cửa sổ?",
                    response_text="Bạn muốn mở hay đóng cửa sổ?",
                )
            zone = cabin_zone(text)
            position = (int(percent.group(1)) if (percent := re.search(r"(\d+)\s*(?:%|phan tram)", text))
                        else 50 if "mot nua" in text else 100 if opening else 0)
            if zone is None:
                action = "mở" if opening else "đóng"
                question = f"Bạn muốn {action} cửa sổ bên nào?"
                return IntentDecision(
                    route="clarify",
                    intent="conversation.clarify",
                    needs_clarification=True,
                    clarification_question=question,
                    response_text=question,
                    follow_up={"intent": "window.set_position", "arguments": {"position_percent": position}, "missing_slot": "zone"},
                )
            return IntentDecision(
                route="action",
                intent="window.set_position",
                arguments={
                    "position_percent": position,
                    "zone": zone,
                },
                response_text="Mình sẽ điều chỉnh cửa sổ nhé.",
            )
        door_request = re.search(
            r"\b(?:mo|dong)\s+(?:(?:tat ca|toan bo)\s+)?cua\b|"
            r"\b(?:mo\s+khoa|khoa)\s+(?:(?:tat ca|toan bo)\s+)?cua\b",
            text,
        )
        if door_request:
            if re.search(r"mo khoa|\bkhoa\s+(?:(?:tat ca|toan bo)\s+)?cua", text):
                zone = cabin_zone(text)
                if zone is None:
                    question = "Bạn muốn điều chỉnh khóa cửa ở vị trí nào?"
                    return IntentDecision(
                        route="clarify",
                        intent="conversation.clarify",
                        needs_clarification=True,
                        clarification_question=question,
                        response_text=question,
                        follow_up={"intent": "door.set_lock", "arguments": {"locked": not bool(re.search(r"mo khoa", text))}, "missing_slot": "zone"},
                    )
                return IntentDecision(
                    route="action",
                    intent="door.set_lock",
                    arguments={"locked": not bool(re.search(r"mo khoa", text)), "zone": zone},
                    response_text="Mình sẽ điều chỉnh khoá cửa nhé.",
                )
            opening = bool(re.search(r"\bmo\b", text))
            closing = bool(re.search(r"\bdong\b", text))
            if opening == closing:
                return IntentDecision(
                    route="clarify",
                    intent="conversation.clarify",
                    needs_clarification=True,
                    clarification_question="Bạn muốn mở hay đóng cửa xe bên tài?",
                )
            zone = cabin_zone(text)
            if zone is None:
                action = "mở" if opening else "đóng"
                question = f"Bạn muốn {action} cửa bên nào?"
                return IntentDecision(
                    route="clarify",
                    intent="conversation.clarify",
                    needs_clarification=True,
                    clarification_question=question,
                    response_text=question,
                    follow_up={"intent": "door.set_open", "arguments": {"open": opening}, "missing_slot": "zone"},
                )
            return IntentDecision(
                route="action",
                intent="door.set_open",
                arguments={"open": opening, "zone": zone},
                response_text="Mình sẽ điều chỉnh cửa xe nhé.",
            )
        if re.search(r"suoi(?: (?:tat ca|toan bo))? ghe|ghe suoi|lam am ghe", text):
            match = re.search(r"(?:muc|cap)\s*(\d+)", text)
            level = 0 if re.search(r"\b(tat|dung)\b", text) else int(match.group(1)) if match else 1
            zone = cabin_zone(text)
            if zone is None:
                question = "Bạn muốn điều chỉnh sưởi ghế ở vị trí nào?"
                return IntentDecision(
                    route="clarify",
                    intent="conversation.clarify",
                    needs_clarification=True,
                    clarification_question=question,
                    response_text=question,
                    follow_up={"intent": "seat.set_heat_level", "arguments": {"level": level}, "missing_slot": "zone"},
                )
            return IntentDecision(
                route="action",
                intent="seat.set_heat_level",
                arguments={"level": level, "zone": zone},
                response_text=f"Mình sẽ đặt sưởi ghế ở mức {level} nhé.",
            )
        if re.search(r"phat nhac|mo nhac", text):
            media_query = re.split(r"phát nhạc|mở nhạc|phat nhac|mo nhac", input_text, flags=re.IGNORECASE)[-1].strip(
                " .?!"
            )
            media_query = re.sub(r"(?:\s|^)(?:(?:luôn|luon)\s+)?(?:đi|di|nhé|nhe|giúp tôi|giup toi)$", "", media_query).strip()
            return IntentDecision(
                route="action",
                intent="media.play",
                arguments={"media_query": media_query or None},
                response_text="Mình sẽ bật nhạc nhé.",
            )
        if re.search(r"dung nhac|tat nhac", text):
            return IntentDecision(route="action", intent="media.pause", response_text="Mình sẽ dừng nhạc nhé.")
        return None
