from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


def utc_now() -> datetime:
    return datetime.now(UTC)


PowerState = Literal["off", "accessory", "ready", "driving"]
CabinZone = Literal["driver", "front_passenger", "rear_left", "rear_right"]


class TirePressures(BaseModel):
    model_config = ConfigDict(extra="forbid")

    front_left: float = Field(default=250, ge=0, le=500)
    front_right: float = Field(default=250, ge=0, le=500)
    rear_left: float = Field(default=250, ge=0, le=500)
    rear_right: float = Field(default=250, ge=0, le=500)


class WindowPositions(BaseModel):
    model_config = ConfigDict(extra="forbid")

    driver: int = Field(default=0, ge=0, le=100)
    front_passenger: int = Field(default=0, ge=0, le=100)
    rear_left: int = Field(default=0, ge=0, le=100)
    rear_right: int = Field(default=0, ge=0, le=100)


class DoorState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    open: bool = False
    locked: bool = False


class DoorStates(BaseModel):
    model_config = ConfigDict(extra="forbid")

    driver: DoorState = Field(default_factory=DoorState)
    front_passenger: DoorState = Field(default_factory=DoorState)
    rear_left: DoorState = Field(default_factory=DoorState)
    rear_right: DoorState = Field(default_factory=DoorState)


class SeatHeatLevels(BaseModel):
    model_config = ConfigDict(extra="forbid")

    driver: int = Field(default=0, ge=0, le=3)
    front_passenger: int = Field(default=0, ge=0, le=3)
    rear_left: int = Field(default=0, ge=0, le=3)
    rear_right: int = Field(default=0, ge=0, le=3)


class VehicleState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    vehicle_id: str = Field(min_length=1, max_length=100)
    state_version: int = Field(default=0, ge=0)
    updated_at: datetime = Field(default_factory=utc_now)
    temperature_celsius: float = Field(default=23, ge=16, le=30)
    window_driver_percent: int = Field(default=0, ge=0, le=100)
    media_playing: bool = False
    driving: bool = False
    power_state: PowerState = "off"
    battery_percent: int = Field(default=82, ge=0, le=100)
    range_km: int = Field(default=328, ge=0)
    tire_pressures_kpa: TirePressures = Field(default_factory=TirePressures)
    powertrain_temperature_celsius: float = Field(default=45, ge=-50, le=250)
    door_driver_locked: bool = False
    door_driver_open: bool = False
    seat_driver_heat_level: int = Field(default=0, ge=0, le=3)
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


class VehicleCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    command_id: str = Field(min_length=1, max_length=100)
    correlation_id: str = Field(min_length=1, max_length=100)
    idempotency_key: str = Field(min_length=1, max_length=200)
    vehicle_id: str = Field(min_length=1, max_length=100)
    action: str = Field(min_length=1, max_length=100)
    arguments: dict[str, Any] = Field(default_factory=dict)
    issued_at: datetime
    expires_at: datetime
    expected_state_version: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def validate_times(self) -> VehicleCommand:
        if self.issued_at.tzinfo is None or self.expires_at.tzinfo is None:
            raise ValueError("issued_at and expires_at must include a timezone")
        if self.expires_at <= self.issued_at:
            raise ValueError("expires_at must be after issued_at")
        return self


class VehicleAck(BaseModel):
    command_id: str
    correlation_id: str
    idempotency_key: str
    vehicle_id: str
    state_version: int
    status: Literal["applied", "rejected", "duplicate"]
    reason_code: str | None = None
    original_status: Literal["applied", "rejected"] | None = None
    original_command_id: str | None = None
    processed_at: datetime = Field(default_factory=utc_now)


class CommandResult(BaseModel):
    ack: VehicleAck
    state: VehicleState


class FaultScenario(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Literal[
        "success",
        "delay",
        "timeout_before_apply",
        "ack_lost_after_apply",
        "reject",
        "disconnected",
        "state_mismatch",
    ] = "success"
    delay_ms: int = Field(default=0, ge=0, le=30_000)


class VehicleFixture(BaseModel):
    model_config = ConfigDict(extra="forbid")

    driving: bool | None = None
    power_state: PowerState | None = None
    battery_percent: int | None = Field(default=None, ge=0, le=100)
    range_km: int | None = Field(default=None, ge=0)
    tire_pressures_kpa: TirePressures | None = None
    powertrain_temperature_celsius: float | None = Field(default=None, ge=-50, le=250)
    door_driver_open: bool | None = None
    window_positions: WindowPositions | None = None
    door_states: DoorStates | None = None
    hood_open: bool | None = None
    trunk_open: bool | None = None
    seat_heat_levels: SeatHeatLevels | None = None

    @model_validator(mode="after")
    def validate_power_fields(self) -> VehicleFixture:
        if self.power_state is not None and self.driving is not None:
            if self.driving != (self.power_state == "driving"):
                raise ValueError("driving must match power_state")
        if self.door_driver_open is not None and self.door_states is not None:
            if self.door_driver_open != self.door_states.driver.open:
                raise ValueError("door_driver_open must match door_states.driver.open")
        return self
