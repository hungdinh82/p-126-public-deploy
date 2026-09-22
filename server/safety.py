from __future__ import annotations

from dataclasses import dataclass

from .schemas import ActionProposal, VehicleState


@dataclass
class SafetyResult:
    allowed: bool
    status: str
    message: str


def validate(proposal: ActionProposal, vehicle: VehicleState) -> SafetyResult:
    if proposal.needs_clarification or proposal.intent == "conversation.clarify":
        return SafetyResult(False, "clarify", proposal.clarification_question or proposal.spoken_response or "Bạn nói rõ thêm giúp mình nhé.")
    if proposal.intent == "climate.set_temperature":
        value = proposal.arguments.get("value_celsius")
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            return SafetyResult(False, "clarify", "Bạn muốn đặt nhiệt độ bao nhiêu?")
        if not 16 <= float(value) <= 30:
            return SafetyResult(False, "blocked", "Nhiệt độ hợp lệ nằm trong khoảng 16 đến 30 độ.")
    if proposal.intent == "window.set_position":
        value = proposal.arguments.get("position_percent")
        if not isinstance(value, (int, float)) or not 0 <= float(value) <= 100:
            return SafetyResult(False, "clarify", "Bạn muốn mở cửa sổ ở mức bao nhiêu phần trăm?")
        if vehicle.driving and float(value) > vehicle.window_driver_percent:
            return SafetyResult(False, "blocked", "Mình giữ nguyên cửa sổ vì xe đang ở chế độ lái.")
    return SafetyResult(True, "verified", "")

