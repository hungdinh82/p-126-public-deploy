from __future__ import annotations

from src.vivi.agents.contracts import ActionProposal, IntentDecision
from src.vivi.vehicle.zones import selected_zones


def validate_action_arguments(decision: IntentDecision) -> tuple[ActionProposal | None, str | None]:
    """Shared syntax/range validation for direct actions and saved command templates."""
    arguments = decision.arguments.model_dump(exclude_none=True)
    if decision.intent in {"window.set_position", "door.set_open", "door.set_lock", "seat.set_heat_level"}:
        try:
            selected_zones(arguments)
        except ValueError as exc:
            return None, str(exc)
    if decision.intent == "climate.set_temperature":
        value = arguments.get("value_celsius")
        if not isinstance(value, (int, float)):
            return None, "Bạn muốn đặt nhiệt độ bao nhiêu?"
        if not 16 <= float(value) <= 30:
            return None, "Nhiệt độ hỗ trợ nằm trong khoảng 16 đến 30 độ C."
    elif decision.intent == "window.set_position":
        position = arguments.get("position_percent")
        if not isinstance(position, (int, float)):
            return None, "Bạn muốn mở hoặc đóng cửa sổ đến mức nào?"
        if not 0 <= float(position) <= 100:
            return None, "Vị trí cửa sổ phải nằm trong khoảng 0 đến 100 phần trăm."
    elif decision.intent == "door.set_open":
        if not isinstance(arguments.get("open"), bool):
            return None, "Bạn muốn mở hay đóng cửa?"
    elif decision.intent == "door.set_lock":
        if not isinstance(arguments.get("locked"), bool):
            return None, "Bạn muốn khóa hay mở khóa cửa?"
    elif decision.intent == "seat.set_heat_level":
        level = arguments.get("level")
        if not isinstance(level, int) or isinstance(level, bool) or not 0 <= level <= 3:
            return None, "Mức sưởi ghế hợp lệ nằm trong khoảng 0 đến 3."
    return ActionProposal(intent=decision.intent, arguments=arguments, confidence=decision.confidence), None
