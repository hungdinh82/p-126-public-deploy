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
        if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 100:
            return SafetyResult(False, "clarify", "Bạn muốn mở cửa sổ ở mức bao nhiêu phần trăm?")
        if vehicle.driving and value > vehicle.window_driver_percent:
            return SafetyResult(False, "blocked", "Mình giữ nguyên cửa sổ vì xe đang ở chế độ lái.")
    if proposal.intent == "door.set_lock":
        locked = proposal.arguments.get("locked")
        if not isinstance(locked, bool):
            return SafetyResult(False, "clarify", "Bạn muốn khóa hay mở khóa cửa xe bên tài?")
        if locked and vehicle.door_driver_open:
            return SafetyResult(False, "blocked", "Cần đóng cửa xe trước khi khóa.")
    if proposal.intent == "door.set_open":
        opening = proposal.arguments.get("open")
        if not isinstance(opening, bool):
            return SafetyResult(False, "clarify", "Bạn muốn mở hay đóng cửa xe bên tài?")
        if opening and (vehicle.driving or vehicle.door_driver_locked):
            return SafetyResult(False, "blocked", "Không thể mở cửa khi xe đang lái hoặc cửa đang khóa.")
    if proposal.intent == "seat.set_heat_level":
        level = proposal.arguments.get("level")
        if isinstance(level, bool) or not isinstance(level, int) or not 0 <= level <= 3:
            return SafetyResult(False, "blocked", "Mức sưởi ghế hợp lệ là 0 đến 3.")
    return SafetyResult(True, "verified", "")
