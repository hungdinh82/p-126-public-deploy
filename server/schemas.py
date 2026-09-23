from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


Intent = Literal[
    "climate.set_temperature",
    "window.set_position",
    "door.set_open",
    "media.play",
    "media.pause",
    "vehicle.get_status",
    "manual.search",
    "conversation.respond",
    "conversation.clarify",
    "unsupported.request",
    "vehicle.prohibited",
]
Route = Literal["conversation", "handbook", "vehicle", "clarify", "unsupported"]
RiskClass = Literal["R0", "R1", "R2", "R3"]
TurnStatus = Literal["verified", "clarify", "blocked", "confirmation_required", "denied", "expired", "unsupported", "unverified", "error"]


class ActionProposal(BaseModel):
    intent: Intent
    arguments: dict[str, Any] = Field(default_factory=dict)
    needs_clarification: bool = False
    clarification_question: str | None = None
    spoken_response: str = ""
    confidence: float | None = Field(default=None, ge=0, le=1)


ACTION_JSON_SCHEMA = ActionProposal.model_json_schema()


class VehicleState(BaseModel):
    temperature_celsius: float = 23
    window_driver_percent: int = 0
    driver_door_open: bool = False
    media_playing: bool = False
    driving: bool = False
    battery_percent: int = 82
    range_km: int = 328


class Evidence(BaseModel):
    source_id: str
    page: int | None = None
    section: str | None = None
    excerpt: str = ""


class TraceSpan(BaseModel):
    stage: Literal["model", "route", "policy", "tool", "result"]
    outcome: str
    latency_ms: float = 0
    detail: dict[str, Any] = Field(default_factory=dict)


class ConfirmationPreview(BaseModel):
    confirmation_id: str
    preview: str
    action: ActionProposal
    risk_class: RiskClass = "R2"
    expires_at: str


class TurnRequest(BaseModel):
    transcript: str = Field(min_length=1, max_length=500)
    session_id: str = Field(min_length=1, max_length=100)
    turn_id: str = Field(min_length=1, max_length=100)
    vehicle_state: VehicleState | None = None
    confirmation_id: str | None = Field(default=None, max_length=100)
    confirmation_decision: Literal["approve", "deny"] | None = None


class TurnResponse(BaseModel):
    session_id: str
    turn_id: str
    trace_id: str
    transcript: str
    provider: str
    route: Route
    status: TurnStatus
    action: ActionProposal | None = None
    risk_class: RiskClass | None = None
    confirmation: ConfirmationPreview | None = None
    evidence: list[Evidence] = Field(default_factory=list)
    vehicle_state: VehicleState
    message: str
    error: str | None = None
    latency_ms: dict[str, float] = Field(default_factory=dict)
    trace: list[TraceSpan] = Field(default_factory=list)


class STTResponse(BaseModel):
    session_id: str
    turn_id: str
    transcript: str
    provider: str
    audio_path: str | None = None
    latency_ms: float


class TTSRequest(BaseModel):
    text: str = Field(min_length=1, max_length=1000)
    session_id: str
    turn_id: str

