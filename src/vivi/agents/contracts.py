from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Intent = Literal[
    "manual.search",
    "climate.set_temperature",
    "window.set_position",
    "door.set_lock",
    "door.set_open",
    "seat.set_heat_level",
    "media.play",
    "media.pause",
    "vehicle.get_status",
    "conversation.respond",
    "conversation.clarify",
    "unsupported.request",
    "vehicle.prohibited",
]
Route = Literal["handbook", "action", "conversation", "clarify", "unsupported"]


class DecisionArguments(BaseModel):
    """Canonical arguments shared by the supported intent set."""

    model_config = ConfigDict(extra="forbid")

    query: str | None = None
    value_celsius: float | None = None
    position_percent: int | None = None
    open: bool | None = None
    locked: bool | None = None
    level: int | None = None
    zone: str | None = None
    media_query: str | None = None


class IntentDecision(BaseModel):
    """Structured result produced by the intent-classification model."""

    route: Route
    intent: Intent
    arguments: DecisionArguments = Field(default_factory=DecisionArguments)
    response_text: str = ""
    needs_clarification: bool = False
    clarification_question: str | None = None
    confidence: float = Field(default=1.0, ge=0, le=1)

    @model_validator(mode="after")
    def validate_route_intent_pair(self) -> IntentDecision:
        expected_routes: dict[str, str] = {
            "manual.search": "handbook",
            "conversation.respond": "conversation",
            "conversation.clarify": "clarify",
            "unsupported.request": "unsupported",
            "vehicle.prohibited": "unsupported",
        }
        expected = expected_routes.get(self.intent, "action")
        if self.route != expected:
            raise ValueError(f"intent {self.intent} must use route {expected}")
        if self.needs_clarification and self.route != "clarify":
            raise ValueError("needs_clarification requires the clarify route")
        return self


class ActionProposal(BaseModel):
    intent: Literal[
        "climate.set_temperature",
        "window.set_position",
        "door.set_lock",
        "door.set_open",
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
