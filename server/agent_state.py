from __future__ import annotations

from typing import TypedDict

from .schemas import ActionProposal, Evidence, RiskClass, Route, TraceSpan, VehicleState


class AgentState(TypedDict):
    """Contract shared by orchestration nodes for one user turn."""

    session_id: str
    turn_id: str
    trace_id: str
    transcript: str
    route: Route | None
    intent: str | None
    action: ActionProposal | None
    evidence: list[Evidence]
    vehicle_state: VehicleState
    risk_class: RiskClass | None
    confirmation_id: str | None
    errors: list[str]
    trace: list[TraceSpan]


def initial_state(session_id: str, turn_id: str, trace_id: str, transcript: str, vehicle_state: VehicleState) -> AgentState:
    return {
        "session_id": session_id,
        "turn_id": turn_id,
        "trace_id": trace_id,
        "transcript": transcript,
        "route": None,
        "intent": None,
        "action": None,
        "evidence": [],
        "vehicle_state": vehicle_state,
        "risk_class": None,
        "confirmation_id": None,
        "errors": [],
        "trace": [],
    }
