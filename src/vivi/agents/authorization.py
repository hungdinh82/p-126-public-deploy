"""Deterministic action authorization, independent of the language model's prose."""

import re

from src.vivi.agents.contracts import FollowUp, IntentDecision
from src.vivi.agents.dialogue import clarify, respond
from src.vivi.agents.planner import priority_decision
from src.vivi.agents.slots import cabin_zone
from src.vivi.agents.tasks import acceptance, active_task
from src.vivi.agents.tools import TOOL_REGISTRY
from src.vivi.text import normalize_text


def input_guard(text: str, history: list[dict]) -> IntentDecision | None:
    # A clear stop request is a latency-critical interruption, not small talk.
    if re.fullmatch(r"(?:vivi[, ]+)?(?:dung|tat|tam dung) nhac(?: lai)?(?: di| nhe)?",
                    normalize_text(text).strip(" .!?")):
        return IntentDecision(route="action", intent="media.pause")
    # Exact task responses must not be reinterpreted using an expired offer.
    answer = acceptance(text)
    if answer is not None and active_task(history) is None:
        return clarify("Bạn muốn mình làm gì nhé?") if answer else respond("Được nhé.")
    protected = priority_decision(text)
    if protected and protected.route in {"conversation", "clarify"}:
        return protected
    return None


def authorize_model_decision(decision: IntentDecision, text: str) -> IntentDecision:
    normalized = normalize_text(text)
    if decision.route == "action":
        # Memory command replay already resolves through its own validated path.
        if decision.intent in {"window.set_position", "door.set_open", "door.set_lock", "seat.set_heat_level"}:
            if cabin_zone(normalized) is None:
                arguments = decision.arguments.model_dump(exclude_none=True, exclude={"zone"})
                question = TOOL_REGISTRY[decision.intent].missing_question
                result = clarify(question)
                result.follow_up = FollowUp(intent=decision.intent, arguments=arguments, missing_slot="zone")
                return result
        if decision.intent == "media.play" and not re.search(
            r"\b(?:mo|bat|phat|nghe|choi|play|tiep tuc|thuc hien|chay)\b", normalized
        ):
            return IntentDecision(
                route="conversation", intent="conversation.respond",
                response_text="Mình ở đây với bạn. Bạn muốn nghe nhạc một chút không?",
                follow_up=FollowUp(intent="media.play"),
            )
    if decision.intent == "memory.remember" and not re.search(
        r"\b(?:ghi nho|nho rang|nho giup|luu|tu nay|nho ho)\b", normalized
    ):
        return respond("Mình hiểu rồi.")
    if decision.intent == "memory.forget" and not re.search(r"\b(?:quen|xoa|bo|go)\b", normalized):
        return clarify("Bạn muốn mình quên thông tin nào?")
    return decision
