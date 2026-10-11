"""Shared fixture helpers for the unified parser and semantic model contract."""
from types import SimpleNamespace

from src.vivi.capabilities.registry import REGISTRY
from src.vivi.dialogue.contracts import SessionState
from src.vivi.dialogue.parser import OfflineParser


def parse(text, session=None, history=None, observation=None):
    frame = OfflineParser().interpret(text, session or SessionState(), history=history or [])
    action = frame.actions[0] if frame.actions else None
    if action:
        intent = action.capability
        route = "handbook" if intent == "manual.search" else "conversation" if intent.startswith("memory.") or frame.act == "offer" else "action"
        args, error = REGISTRY[intent].validate(action, observation)
        return SimpleNamespace(frame=frame, act=frame.act, route=route, intent=intent, arguments=action.arguments, resolved=args, error=error)
    route = "clarify" if frame.act == "clarify" else "unsupported" if frame.act == "unsupported" else "conversation"
    return SimpleNamespace(frame=frame, act=frame.act, route=route, intent="conversation.clarify" if route == "clarify" else "unsupported.request" if route == "unsupported" else "conversation.respond", response_text=frame.text)
