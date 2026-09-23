from __future__ import annotations

from .schemas import ActionProposal, Route


_VEHICLE_INTENTS = {
    "climate.set_temperature",
    "window.set_position",
    "door.set_open",
    "media.play",
    "media.pause",
    "vehicle.get_status",
}


def route_action(action: ActionProposal) -> Route:
    """Map a validated typed action to exactly one orchestration branch."""
    if action.needs_clarification or action.intent == "conversation.clarify":
        return "clarify"
    if action.intent == "conversation.respond":
        return "conversation"
    if action.intent == "manual.search":
        return "handbook"
    if action.intent in _VEHICLE_INTENTS:
        return "vehicle"
    return "unsupported"
