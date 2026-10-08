from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

Intent = Literal[
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
                "locked": {"type": ["boolean", "null"]},
                "level": {"type": ["integer", "null"]},
                "zone": {
                    "type": ["string", "null"],
                    "enum": ["driver", "front_passenger", "rear_left", "rear_right", "all", None],
                },
                "media_query": {"type": ["string", "null"]},
            },
            "required": [
                "value_celsius", "position_percent", "query", "open", "locked", "level",
                "zone", "media_query"
            ],
            "additionalProperties": False,
        },
        "needs_clarification": {"type": "boolean"},
        "clarification_question": {"type": ["string", "null"]},
    },
    "required": ["intent", "arguments", "needs_clarification", "clarification_question", "spoken_response"],
    "additionalProperties": False,
}


PowerState = Literal["off", "accessory", "ready", "driving"]
CabinZone = Literal["driver", "front_passenger", "rear_left", "rear_right"]


class TirePressures(BaseModel):
    front_left: float = Field(default=250, ge=0, le=500)
    front_right: float = Field(default=250, ge=0, le=500)
    rear_left: float = Field(default=250, ge=0, le=500)
    rear_right: float = Field(default=250, ge=0, le=500)


class WindowPositions(BaseModel):
    driver: int = Field(default=0, ge=0, le=100)
    front_passenger: int = Field(default=0, ge=0, le=100)
    rear_left: int = Field(default=0, ge=0, le=100)
    rear_right: int = Field(default=0, ge=0, le=100)


class DoorState(BaseModel):
    open: bool = False
    locked: bool = False


class DoorStates(BaseModel):
    driver: DoorState = Field(default_factory=DoorState)
    front_passenger: DoorState = Field(default_factory=DoorState)
    rear_left: DoorState = Field(default_factory=DoorState)
    rear_right: DoorState = Field(default_factory=DoorState)


class SeatHeatLevels(BaseModel):
    driver: int = Field(default=0, ge=0, le=3)
    front_passenger: int = Field(default=0, ge=0, le=3)
    rear_left: int = Field(default=0, ge=0, le=3)
    rear_right: int = Field(default=0, ge=0, le=3)


class VehicleState(BaseModel):
    vehicle_id: str = "demo-car-1"
    state_version: int = 0
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    temperature_celsius: float = 23
    window_driver_percent: int = 0
    door_driver_locked: bool = False
    door_driver_open: bool = False
    seat_driver_heat_level: int = Field(default=0, ge=0, le=3)
    media_playing: bool = False
    driving: bool = False
    power_state: PowerState = "off"
    battery_percent: int = 82
    range_km: int = 328
    tire_pressures_kpa: TirePressures = Field(default_factory=TirePressures)
    powertrain_temperature_celsius: float = Field(default=45, ge=-50, le=250)
    window_positions: WindowPositions = Field(default_factory=WindowPositions)
    door_states: DoorStates = Field(default_factory=DoorStates)
    hood_open: bool = False
    trunk_open: bool = False
    seat_heat_levels: SeatHeatLevels = Field(default_factory=SeatHeatLevels)

    @model_validator(mode="before")
    @classmethod
    def migrate_power_state(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        migrated = dict(value)
        if "power_state" not in migrated:
            migrated["power_state"] = "driving" if migrated.get("driving", False) else "off"
        migrated["driving"] = migrated["power_state"] == "driving"
        windows = migrated.get("window_positions") or {
            "driver": migrated.get("window_driver_percent", 0)
        }
        doors = migrated.get("door_states") or {
            "driver": {
                "open": migrated.get("door_driver_open", False),
                "locked": migrated.get("door_driver_locked", False),
            }
        }
        seats = migrated.get("seat_heat_levels") or {
            "driver": migrated.get("seat_driver_heat_level", 0)
        }
        if isinstance(windows, BaseModel):
            windows = windows.model_dump()
        if isinstance(doors, BaseModel):
            doors = doors.model_dump()
        if isinstance(seats, BaseModel):
            seats = seats.model_dump()
        migrated["window_positions"] = windows
        migrated["door_states"] = doors
        migrated["seat_heat_levels"] = seats
        migrated["window_driver_percent"] = windows.get("driver", 0)
        driver_door = doors.get("driver", {})
        migrated["door_driver_open"] = driver_door.get("open", False)
        migrated["door_driver_locked"] = driver_door.get("locked", False)
        migrated["seat_driver_heat_level"] = seats.get("driver", 0)
        return migrated


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
    vehicle_state: VehicleState | None = Field(
        default=None,
        deprecated=True,
        description="Compatibility context only; ignored at the policy boundary.",
    )
    confirmation_id: str | None = Field(default=None, max_length=100)
    confirmation_decision: Literal["approve", "deny"] | None = None
    llm_provider: Literal["rules", "openai", "google", "local", "openrouter"] | None = None


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
    # Engine picked in the settings panel; None uses TTS_PROVIDER.
    tts_provider: Literal["zerotts", "vieneu"] | None = None


class ConfirmationDecisionRequest(BaseModel):
    session_id: str = Field(min_length=1, max_length=100)
    turn_id: str = Field(min_length=1, max_length=100)
    decision: Literal["approve", "deny"]
    llm_provider: Literal["rules", "openai", "google", "local", "openrouter"] | None = None


class DemoDrivingRequest(BaseModel):
    session_id: str = Field(min_length=1, max_length=100)
    driving: bool
