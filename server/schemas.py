from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

Intent = Literal[
    "climate.set_temperature",
    "window.set_position",
    "media.play",
    "media.pause",
    "vehicle.get_status",
    "manual.search",
    "conversation.clarify",
]


class ActionProposal(BaseModel):
    intent: Intent
    arguments: dict[str, Any] = Field(default_factory=dict)
    needs_clarification: bool = False
    clarification_question: str | None = None
    spoken_response: str = ""


ACTION_JSON_SCHEMA = ActionProposal.model_json_schema()


class VehicleState(BaseModel):
    vehicle_id: str = "demo-car-1"
    state_version: int = 0
    temperature_celsius: float = 23
    window_driver_percent: int = 0
    media_playing: bool = False
    driving: bool = False
    battery_percent: int = 82
    range_km: int = 328
    door_driver_locked: bool = False
    door_driver_open: bool = False
    seat_driver_heat_level: int = 0


class TurnRequest(BaseModel):
    transcript: str = Field(min_length=1, max_length=500)
    session_id: str = Field(min_length=1, max_length=100)
    turn_id: str = Field(min_length=1, max_length=100)
    vehicle_state: VehicleState | None = None


class TurnResponse(BaseModel):
    session_id: str
    turn_id: str
    transcript: str
    provider: str
    status: Literal["verified", "clarify", "blocked", "unverified", "error"]
    action: ActionProposal
    vehicle_state: VehicleState | None
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
