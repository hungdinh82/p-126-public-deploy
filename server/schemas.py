from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator


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

    @model_validator(mode="before")
    @classmethod
    def normalize_intent_aliases(cls, value: Any) -> Any:
        """Repair common non-schema intents produced by non-strict local LLMs.

        Relative climate wording still has to include a safe absolute
        ``value_celsius``. If it does not, the safety layer asks the user for
        clarification instead of rejecting the whole turn at JSON validation.
        """
        if not isinstance(value, dict):
            return value
        aliases = {
            "climate.increase_temperature": "climate.set_temperature",
            "climate.decrease_temperature": "climate.set_temperature",
            "climate.raise_temperature": "climate.set_temperature",
            "climate.lower_temperature": "climate.set_temperature",
        }
        intent = value.get("intent")
        if intent in aliases:
            value = dict(value)
            value["intent"] = aliases[intent]
        return value


ACTION_JSON_SCHEMA = {
    "type": "object",
    "properties": {
        "intent": {"type": "string", "enum": list(Intent.__args__)},
        # Keep spoken_response immediately after intent. Structured-output
        # providers preserve schema order, allowing safe conversational speech
        # to start while the remaining metadata is still arriving.
        "spoken_response": {"type": "string"},
        "arguments": {
            "type": "object",
            "properties": {
                "value_celsius": {"type": ["number", "null"]},
                "position_percent": {"type": ["number", "null"]},
                "query": {"type": ["string", "null"]},
                "open": {"type": ["boolean", "null"]},
                "zone": {"type": ["string", "null"]},
                "media_query": {"type": ["string", "null"]},
            },
            "required": [
                "value_celsius", "position_percent", "query", "open", "zone", "media_query"
            ],
            "additionalProperties": False,
        },
        "needs_clarification": {"type": "boolean"},
        "clarification_question": {"type": ["string", "null"]},
    },
    "required": ["intent", "arguments", "needs_clarification", "clarification_question", "spoken_response"],
    "additionalProperties": False,
}


class VehicleState(BaseModel):
    vehicle_id: str = "demo-car-1"
    state_version: int = 0
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
    source_url: str | None = None
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
    llm_provider: Literal["rules", "openai", "google", "local"] | None = None


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
    grounding_status: str = "not_applicable"
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
