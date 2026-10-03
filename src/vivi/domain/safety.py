from __future__ import annotations

from dataclasses import dataclass

from src.vivi.vehicle.zones import selected_zones, zoned_noun

from .models import ActionProposal, VehicleState


@dataclass
class SafetyResult:
    allowed: bool
    status: str
    message: str
    risk_class: str
    requires_confirmation: bool = False
    preview: str | None = None


RISK_BY_INTENT = {
    "vehicle.get_status": "R0",
    "manual.search": "R0",
    "conversation.respond": "R0",
    "conversation.clarify": "R0",
    "climate.set_temperature": "R1",
    "media.play": "R1",
    "media.pause": "R1",
    "window.set_position": "R2",
    "door.set_lock": "R2",
    "door.set_open": "R2",
    "seat.set_heat_level": "R1",
    "unsupported.request": "R3",
    "vehicle.prohibited": "R3",
}


def _r2_preview(proposal: ActionProposal) -> str:
    if proposal.intent == "window.set_position":
        target = zoned_noun("cửa sổ", proposal.arguments)
        return f"Xác nhận đặt {target} ở mức {int(proposal.arguments['position_percent'])} phần trăm?"
    if proposal.intent == "door.set_lock":
        target = zoned_noun("cửa", proposal.arguments)
        return f"Xác nhận khóa {target}?" if proposal.arguments.get("locked") else f"Xác nhận mở khóa {target}?"
    target = zoned_noun("cửa", proposal.arguments)
    return f"Xác nhận mở {target}?" if proposal.arguments.get("open") else f"Xác nhận đóng {target}?"


def validate(proposal: ActionProposal, vehicle: VehicleState, *, confirmed: bool = False) -> SafetyResult:
    risk = RISK_BY_INTENT.get(proposal.intent, "R3")
    if proposal.needs_clarification or proposal.intent == "conversation.clarify":
        message = proposal.clarification_question or proposal.spoken_response or "Bạn nói rõ thêm giúp mình nhé."
        return SafetyResult(False, "clarify", message, "R0")
    if proposal.intent == "conversation.respond":
        if not proposal.spoken_response.strip():
            return SafetyResult(False, "clarify", "Bạn nói rõ hơn giúp mình nhé.", "R0")
        return SafetyResult(True, "verified", "", "R0")
    if proposal.intent in {"unsupported.request", "vehicle.prohibited"}:
        return SafetyResult(False, "blocked", "Yêu cầu này không nằm trong nhóm thao tác được ViVi cho phép.", risk)
    if proposal.intent == "climate.set_temperature":
        value = proposal.arguments.get("value_celsius")
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            return SafetyResult(False, "clarify", "Bạn muốn đặt nhiệt độ bao nhiêu?", risk)
        if not 16 <= float(value) <= 30:
            return SafetyResult(False, "blocked", "Nhiệt độ hợp lệ nằm trong khoảng 16 đến 30 độ.", risk)
    if proposal.intent == "window.set_position":
        value = proposal.arguments.get("position_percent")
        if not isinstance(value, (int, float)) or isinstance(value, bool) or not 0 <= float(value) <= 100:
            return SafetyResult(False, "clarify", "Bạn muốn mở cửa sổ ở mức bao nhiêu phần trăm?", risk)
        try:
            zones = selected_zones(proposal.arguments)
        except ValueError as exc:
            return SafetyResult(False, "clarify", str(exc), risk)
        if vehicle.driving and any(float(value) > getattr(vehicle.window_positions, zone) for zone in zones):
            return SafetyResult(False, "blocked", "Mình giữ nguyên cửa sổ vì xe đang ở chế độ lái.", risk)
        if all(float(value) <= getattr(vehicle.window_positions, zone) for zone in zones):
            risk = "R1"
    if proposal.intent == "door.set_open":
        opening = proposal.arguments.get("open")
        if not isinstance(opening, bool):
            return SafetyResult(False, "clarify", "Bạn muốn mở hay đóng cửa nào?", risk)
        try:
            zones = selected_zones(proposal.arguments)
        except ValueError as exc:
            return SafetyResult(False, "clarify", str(exc), risk)
        if vehicle.driving and opening:
            return SafetyResult(False, "blocked", "Không thể mở cửa khi xe đang ở chế độ lái.", risk)
        if opening and any(getattr(vehicle.door_states, zone).locked for zone in zones):
            return SafetyResult(False, "blocked", "Hãy mở khóa cửa trước khi mở cửa.", risk)
    if proposal.intent == "door.set_lock":
        locked = proposal.arguments.get("locked")
        if not isinstance(locked, bool):
            return SafetyResult(False, "clarify", "Bạn muốn khóa hay mở khóa cửa nào?", risk)
        try:
            zones = selected_zones(proposal.arguments)
        except ValueError as exc:
            return SafetyResult(False, "clarify", str(exc), risk)
        if locked and any(getattr(vehicle.door_states, zone).open for zone in zones):
            return SafetyResult(False, "blocked", "Không thể khóa khi cửa đang mở.", risk)
    if proposal.intent == "seat.set_heat_level":
        level = proposal.arguments.get("level")
        if not isinstance(level, int) or isinstance(level, bool) or not 0 <= level <= 3:
            return SafetyResult(False, "blocked", "Mức sưởi ghế hợp lệ nằm trong khoảng 0 đến 3.", risk)
        try:
            selected_zones(proposal.arguments)
        except ValueError as exc:
            return SafetyResult(False, "clarify", str(exc), risk)
    if risk == "R2" and not confirmed:
        preview = _r2_preview(proposal)
        return SafetyResult(False, "confirmation_required", preview, risk, True, preview)

    return SafetyResult(True, "verified", "", risk)
