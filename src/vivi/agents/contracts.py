from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

Route = Literal["handbook", "action", "conversation", "clarify", "unsupported"]


class ActionProposal(BaseModel):
    intent: Literal[
        "memory.reset",
        "climate.set_temperature",
        "window.set_position",
        "door.set_lock",
        "door.set_open",
        "hood.set_open",
        "trunk.set_open",
        "body.set_open",
        "seat.set_heat_level",
        "media.play",
        "media.pause",
        "vehicle.get_status",
    ]
    arguments: dict[str, Any] = Field(default_factory=dict)
    confidence: float = Field(default=1.0, ge=0, le=1)


class ActionExecution(BaseModel):
    allowed: bool
    executed: bool = False
    verified: bool = False
    risk_class: str | None = None
    message: str = ""
    error: str | None = None


class ConfirmationOutput(BaseModel):
    confirmation_id: str
    preview: str
    expires_at: str


class AssistantOutput(BaseModel):
    session_id: str
    turn_id: str
    route: Route | Literal["invalid"]
    intent: str | None = None
    status: str
    response_text: str
    tts_text: str
    action_proposal: ActionProposal | None = None
    requires_execution: bool = False
    execution: ActionExecution | None = None
    confirmation: ConfirmationOutput | None = None
    vehicle_state: dict[str, Any] | None = None
    citations: list[dict[str, Any]] = Field(default_factory=list)
    grounding_status: str = "not_applicable"
    errors: list[dict[str, str]] = Field(default_factory=list)
    timings: dict[str, float] = Field(default_factory=dict)
    diagnostics: dict[str, Any] = Field(default_factory=dict)
    steps: list[dict[str, Any]] = Field(default_factory=list)
