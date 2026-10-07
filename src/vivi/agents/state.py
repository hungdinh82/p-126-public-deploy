from __future__ import annotations

from typing import Any, TypedDict


class AgentState(TypedDict, total=False):
    """State shared by the STT-text → route → response/action graph."""

    session_id: str
    turn_id: str
    input_text: str
    model_input: dict[str, Any]

    query: str
    retrieval_query: str
    vehicle_model: str
    model_year: int
    locale: str
    intent: str
    route: str
    decision: dict[str, Any]
    action_proposal: dict[str, Any] | None
    requires_execution: bool
    execution: dict[str, Any] | None
    confirmation: dict[str, Any] | None
    confirmation_id: str
    confirmation_decision: str
    vehicle_state: dict[str, Any]
    risk_class: str

    conversation_history: list[dict[str, Any]]
    memory_context: list[dict[str, Any]]
    memory_handled: bool
    task_decision: dict[str, Any]
    task_resolution: str
    tool_record: dict[str, Any]
    memory_reset_accepted: bool
    memory_reset_requested: bool
    retrieved_chunks: list[dict[str, Any]]
    accepted_chunks: list[dict[str, Any]]
    citations: list[dict[str, Any]]

    answer: str
    response: str
    response_text: str
    tts_text: str
    output: dict[str, Any]
    status: str
    grounding_status: str
    abstain_reason: str | None
    errors: list[dict[str, str]]
    timings: dict[str, float]
    metadata: dict[str, Any]
