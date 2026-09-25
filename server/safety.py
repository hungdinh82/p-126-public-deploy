from __future__ import annotations

from dataclasses import dataclass

from .schemas import ActionProposal, VehicleState


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
    "door.set_open": "R2",
    "unsupported.request": "R3",
    "vehicle.prohibited": "R3",
}


def _r2_preview(proposal: ActionProposal) -> str:
    if proposal.intent == "window.set_position":
        return f"Xác nhận đặt cửa sổ bên tài ở mức {int(proposal.arguments['position_percent'])} phần trăm?"
    return "Xác nhận mở cửa bên tài?" if proposal.arguments.get("open") else "Xác nhận đóng cửa bên tài?"


def validate(proposal: ActionProposal, vehicle: VehicleState, *, confirmed: bool = False) -> SafetyResult:
    risk = RISK_BY_INTENT.get(proposal.intent, "R3")
    if proposal.needs_clarification or proposal.intent == "conversation.clarify":
        message = proposal.clarification_question or proposal.spoken_response or "Bạn nói rõ thêm giúp mình nhé."
        return SafetyResult(False, "clarify", message, "R0")
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
        if vehicle.driving and float(value) > vehicle.window_driver_percent:
            return SafetyResult(False, "blocked", "Mình giữ nguyên cửa sổ vì xe đang ở chế độ lái.", risk)
        if float(value) <= vehicle.window_driver_percent:
            risk = "R1"
    if proposal.intent == "door.set_open":
        opening = proposal.arguments.get("open")
        if not isinstance(opening, bool):
            return SafetyResult(False, "clarify", "Bạn muốn mở hay đóng cửa bên tài?", risk)
        if vehicle.driving and opening:
            return SafetyResult(False, "blocked", "Không thể mở cửa khi xe đang ở chế độ lái.", risk)
        if not opening:
            risk = "R1"
    if risk == "R2" and not confirmed:
        preview = _r2_preview(proposal)
        return SafetyResult(False, "confirmation_required", preview, risk, True, preview)

    return SafetyResult(True, "verified", "", risk)
