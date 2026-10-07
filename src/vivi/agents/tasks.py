"""Typed, expiring dialogue tasks. No tool execution and no inference from prose.

The last committed turn is the session checkpoint. Every new turn either
consumes, replaces or clears its pending task; unrelated turns cannot resurrect it.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from typing import Literal

from pydantic import BaseModel, ConfigDict, ValidationError

from src.vivi.agents.contracts import FollowUp, IntentDecision
from src.vivi.agents.slots import cabin_zone, is_cabin_zone_reply, temperature_number
from src.vivi.agents.tools import TOOL_REGISTRY, validate_tool_arguments
from src.vivi.text import normalize_text


class PendingTask(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["offer", "slot", "vehicle_confirmation", "memory_reset"]
    origin_turn: str
    expires_at: datetime
    follow_up: FollowUp | None = None
    confirmation_id: str | None = None


def active_task(history: list[dict], now: datetime | None = None) -> PendingTask | None:
    if not history:
        return None
    value = history[-1].get("diagnostics", {}).get("pending_task")
    if not value:
        return None
    try:
        task = PendingTask.model_validate(value)
        if task.expires_at.tzinfo and task.expires_at > (now or datetime.now(UTC)):
            return task
    except (ValidationError, TypeError):
        pass
    return None


def acceptance(text: str) -> bool | None:
    """Only an unambiguous response to a live proposal is consent."""
    text = normalize_text(text).strip(" .!?,")
    if re.fullmatch(r"(?:co|dong y|duoc|ok|oke|okay|vang|uh|u|lam di|thuc hien)(?: nhe| di| a| luon)?", text):
        return True
    if re.fullmatch(r"(?:khong|khong can|thoi|thoi khoi|bo qua|huy|dung lam)(?: nhe| nua| dau| di| a)?", text):
        return False
    return None


def resume_task(text: str, history: list[dict]) -> dict:
    task = active_task(history)
    if task is None:
        return {"task_resolution": "expired"} if history and history[-1].get("diagnostics", {}).get("pending_task") else {}
    consent = acceptance(text)
    if task.kind == "vehicle_confirmation" and consent is not None:
        return {"confirmation_id": task.confirmation_id,
                "task_resolution": "confirmed" if consent else "cancelled",
                "confirmation_decision": "approve" if consent else "deny"}
    if consent is False:
        return {"task_resolution": "cancelled", "task_decision": IntentDecision(route="conversation", intent="conversation.respond",
                                                response_text="Được nhé, mình bỏ qua thao tác đó.").model_dump()}
    if task.kind == "memory_reset" and consent is True:
        return {"memory_reset_accepted": True, "task_resolution": "confirmed"}
    follow = task.follow_up
    if follow is None:
        return {"task_resolution": "superseded"}
    arguments = follow.arguments.model_dump(exclude_none=True)
    if task.kind == "slot":
        normalized = normalize_text(text).strip(" .!?")
        if follow.missing_slot == "zone" and is_cabin_zone_reply(normalized):
            arguments["zone"] = cabin_zone(normalized)
        elif follow.missing_slot == "value_celsius" and re.fullmatch(
            r"\d+(?:[.,]\d+)?(?: do(?: c)?)?", normalized
        ):
            arguments["value_celsius"] = temperature_number(f"nhiet do {normalized} do")
        elif follow.missing_slot == "level" and re.fullmatch(r"(?:muc )?[0-3]", normalized):
            arguments["level"] = int(normalized[-1])
        else:
            return {"task_resolution": "superseded"}
    elif consent is not True:
        return {"task_resolution": "superseded"}
    valid, error = validate_tool_arguments(follow.intent, arguments)
    if error or follow.intent.startswith(("memory.", "manual.")):
        return {}
    return {"task_resolution": "resumed", "task_decision": IntentDecision(route="action", intent=follow.intent,
                                            arguments=valid).model_dump()}


def pending_for_turn(state: dict) -> dict | None:
    """Persist structured offers only; fabricated prose is never executable."""
    now = datetime.now(UTC)
    common = {"origin_turn": state["turn_id"], "expires_at": now + timedelta(minutes=3)}
    confirmation = state.get("confirmation")
    if confirmation and state.get("status") == "confirmation_required":
        return PendingTask(kind="vehicle_confirmation", **{**common, "expires_at": confirmation["expires_at"]},
                           confirmation_id=confirmation["confirmation_id"]).model_dump(mode="json")
    raw = state.get("decision", {}).get("follow_up")
    if not raw or state.get("route") not in {"conversation", "clarify"}:
        return None
    follow = FollowUp.model_validate(raw)
    if follow.intent == "memory.reset":
        # Only the memory executor can authorize this frame, never model prose.
        if not state.get("memory_reset_requested"):
            return None
        return PendingTask(kind="memory_reset", **common).model_dump(mode="json")
    spec = TOOL_REGISTRY.get(follow.intent)
    if spec is None or follow.intent.startswith(("memory.", "manual.")):
        return None
    arguments = follow.arguments.model_dump(exclude_none=True)
    if follow.missing_slot:
        # Partial validation: the declared missing field must be the only omission.
        placeholders = {"zone": "driver", "value_celsius": 23, "level": 1}
        if follow.missing_slot in arguments:
            return None
        arguments[follow.missing_slot] = placeholders[follow.missing_slot]
    if validate_tool_arguments(follow.intent, arguments)[1]:
        return None
    return PendingTask(kind="slot" if follow.missing_slot else "offer", follow_up=follow,
                       **common).model_dump(mode="json")
