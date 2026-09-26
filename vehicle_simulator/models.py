from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


def utc_now() -> datetime:
    return datetime.now(UTC)


class VehicleState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    vehicle_id: str = Field(min_length=1, max_length=100)
    state_version: int = Field(default=0, ge=0)
    updated_at: datetime = Field(default_factory=utc_now)
    temperature_celsius: float = Field(default=23, ge=16, le=30)
    window_driver_percent: int = Field(default=0, ge=0, le=100)
    media_playing: bool = False
    driving: bool = False
    battery_percent: int = Field(default=82, ge=0, le=100)
    range_km: int = Field(default=328, ge=0)
    door_driver_locked: bool = False
    door_driver_open: bool = False
    seat_driver_heat_level: int = Field(default=0, ge=0, le=3)


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
    battery_percent: int | None = Field(default=None, ge=0, le=100)
    range_km: int | None = Field(default=None, ge=0)
