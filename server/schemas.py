from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator


Intent = Literal[
    "climate.set_temperature",
    "window.set_position",
    "media.play",
    "media.pause",
    "vehicle.get_status",
    "manual.search",
    "conversation.clarify",
    "conversation.respond",
]


class ActionProposal(BaseModel):
    intent: Intent
    arguments: dict[str, Any] = Field(default_factory=dict)
    needs_clarification: bool = False
    clarification_question: str | None = None
    spoken_response: str = ""

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
            },
            "required": ["value_celsius", "position_percent", "query"],
            "additionalProperties": False,
        },
        "needs_clarification": {"type": "boolean"},
        "clarification_question": {"type": ["string", "null"]},
    },
    "required": ["intent", "arguments", "needs_clarification", "clarification_question", "spoken_response"],
    "additionalProperties": False,
}


class VehicleState(BaseModel):
    temperature_celsius: float = 23
    window_driver_percent: int = 0
    media_playing: bool = False
    driving: bool = False
    battery_percent: int = 82
    range_km: int = 328


class TurnRequest(BaseModel):
    transcript: str = Field(min_length=1, max_length=500)
    session_id: str = Field(min_length=1, max_length=100)
    turn_id: str = Field(min_length=1, max_length=100)
    vehicle_state: VehicleState | None = None
    llm_provider: Literal["rules", "openai", "google", "local"] | None = None


class TurnResponse(BaseModel):
    session_id: str
    turn_id: str
    transcript: str
    provider: str
    status: Literal["verified", "clarify", "blocked", "error"]
    action: ActionProposal
    vehicle_state: VehicleState
    message: str
    latency_ms: dict[str, float] = Field(default_factory=dict)


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
