from __future__ import annotations

import threading
from collections import OrderedDict
from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, Field

from src.vivi.domain.models import VehicleState
from src.vivi.vehicle.zones import CABIN_ZONES, ZONE_LABELS

AlertSeverity = Literal["warning", "critical"]
AlertEventType = Literal["triggered", "recovered"]

BATTERY_LOW_PERCENT = 20
TIRE_PRESSURE_MIN_KPA = 220.0
TIRE_PRESSURE_MAX_KPA = 300.0
POWERTRAIN_MAX_CELSIUS = 90.0
TIRE_POSITION_LABELS = {
    "front_left": "trước trái",
    "front_right": "trước phải",
    "rear_left": "sau trái",
    "rear_right": "sau phải",
}


class VehicleAlert(BaseModel):
    alert_id: str
    vehicle_id: str
    code: str
    source: str
    severity: AlertSeverity
    message: str
    observed_value: float | None = None
    threshold: float | None = None
    unit: str | None = None
    state_version: int = Field(ge=0)
    triggered_at: datetime
    updated_at: datetime


class AlertEvent(BaseModel):
    sequence: int = Field(ge=1)
    event_type: AlertEventType
    alert: VehicleAlert
    occurred_at: datetime


class AlertSnapshot(BaseModel):
    vehicle_id: str
    state_version: int = Field(ge=0)
    event_sequence: int = Field(ge=0)
    active_alerts: list[VehicleAlert]


class AlertEngine:
    """Stateful lifecycle manager around pure vehicle safety rules."""

    def __init__(self) -> None:
        self._active: dict[tuple[str, str, str], VehicleAlert] = {}
        self._last_versions: dict[str, int] = {}
        self._sequences: dict[str, int] = {}
        self._lock = threading.RLock()

    def process(self, state: VehicleState) -> list[AlertEvent]:
        with self._lock:
            last_version = self._last_versions.get(state.vehicle_id)
            if last_version is not None and state.state_version <= last_version:
                return []
            self._last_versions[state.vehicle_id] = state.state_version
            desired = {self._key(alert): alert for alert in self._evaluate_rules(state)}
            current_keys = {key for key in self._active if key[0] == state.vehicle_id}
            events: list[AlertEvent] = []

            for key, candidate in desired.items():
                existing = self._active.get(key)
                if existing is None:
                    self._active[key] = candidate
                    events.append(self._event(state.vehicle_id, "triggered", candidate))
                else:
                    refreshed = existing.model_copy(
                        update={
                            "observed_value": candidate.observed_value,
                            "threshold": candidate.threshold,
                            "state_version": state.state_version,
                            "updated_at": state.updated_at,
                            "message": candidate.message,
                        }
                    )
                    self._active[key] = refreshed

            for key in current_keys - desired.keys():
                recovered = self._active.pop(key).model_copy(
                    update={"state_version": state.state_version, "updated_at": state.updated_at}
                )
                events.append(self._event(state.vehicle_id, "recovered", recovered))
            return events

    def evaluate(self, state: VehicleState) -> AlertSnapshot:
        self.process(state)
        return self.snapshot(state.vehicle_id, state.state_version)

    def snapshot(self, vehicle_id: str, state_version: int) -> AlertSnapshot:
        with self._lock:
            alerts = sorted(
                (alert.model_copy(deep=True) for key, alert in self._active.items() if key[0] == vehicle_id),
                key=lambda alert: (alert.severity != "critical", alert.code, alert.source),
            )
            return AlertSnapshot(
                vehicle_id=vehicle_id,
                state_version=state_version,
                event_sequence=self._sequences.get(vehicle_id, 0),
                active_alerts=alerts,
            )

    @staticmethod
    def _key(alert: VehicleAlert) -> tuple[str, str, str]:
        return alert.vehicle_id, alert.code, alert.source

    def _event(self, vehicle_id: str, event_type: AlertEventType, alert: VehicleAlert) -> AlertEvent:
        sequence = self._sequences.get(vehicle_id, 0) + 1
        self._sequences[vehicle_id] = sequence
        return AlertEvent(sequence=sequence, event_type=event_type, alert=alert, occurred_at=alert.updated_at)

    @staticmethod
    def _evaluate_rules(state: VehicleState) -> list[VehicleAlert]:
        alerts: list[VehicleAlert] = []

        def add(
            code: str,
            source: str,
            severity: AlertSeverity,
            message: str,
            observed_value: float | None = None,
            threshold: float | None = None,
            unit: str | None = None,
        ) -> None:
            alerts.append(
                VehicleAlert(
                    alert_id=str(uuid4()),
                    vehicle_id=state.vehicle_id,
                    code=code,
                    source=source,
                    severity=severity,
                    message=message,
                    observed_value=observed_value,
                    threshold=threshold,
                    unit=unit,
                    state_version=state.state_version,
                    triggered_at=state.updated_at,
                    updated_at=state.updated_at,
                )
            )

        if state.battery_percent < BATTERY_LOW_PERCENT:
            add(
                "LOW_BATTERY",
                "battery",
                "warning",
                f"Pin xe chỉ còn {state.battery_percent} phần trăm. Hãy lên kế hoạch sạc xe.",
                state.battery_percent,
                BATTERY_LOW_PERCENT,
                "%",
            )
        for position, pressure in state.tire_pressures_kpa.model_dump().items():
            label = TIRE_POSITION_LABELS[position]
            if pressure < TIRE_PRESSURE_MIN_KPA:
                add(
                    "TIRE_PRESSURE_LOW",
                    position,
                    "warning",
                    f"Áp suất lốp {label} đang thấp, ở mức {pressure:g} kilopascal.",
                    pressure,
                    TIRE_PRESSURE_MIN_KPA,
                    "kPa",
                )
            elif pressure > TIRE_PRESSURE_MAX_KPA:
                add(
                    "TIRE_PRESSURE_HIGH",
                    position,
                    "warning",
                    f"Áp suất lốp {label} đang cao, ở mức {pressure:g} kilopascal.",
                    pressure,
                    TIRE_PRESSURE_MAX_KPA,
                    "kPa",
                )
        if state.powertrain_temperature_celsius > POWERTRAIN_MAX_CELSIUS:
            add(
                "POWERTRAIN_OVERHEAT",
                "powertrain",
                "critical",
                "Nhiệt độ hệ truyền động quá cao. Hãy dừng xe ở nơi an toàn.",
                state.powertrain_temperature_celsius,
                POWERTRAIN_MAX_CELSIUS,
                "°C",
            )
        if state.power_state in {"ready", "driving"}:
            for zone in CABIN_ZONES:
                if getattr(state.door_states, zone).open:
                    add(
                        "DOOR_OPEN_WHEN_READY",
                        zone,
                        "critical",
                        f"Cửa {ZONE_LABELS[zone]} đang mở khi xe sẵn sàng di chuyển.",
                    )
        return alerts


class AlertAnnouncementGate:
    """Deduplicate spoken alerts while never suppressing critical alerts."""

    def __init__(self, cooldown_seconds: float = 30.0, max_seen_ids: int = 1024) -> None:
        if max_seen_ids < 1:
            raise ValueError("max_seen_ids must be positive")
        self.cooldown = timedelta(seconds=cooldown_seconds)
        self.max_seen_ids = max_seen_ids
        self._seen_ids: OrderedDict[str, None] = OrderedDict()
        self._last_announced: dict[tuple[str, str], datetime] = {}

    def should_announce(self, alert: VehicleAlert, now: datetime | None = None) -> bool:
        if alert.alert_id in self._seen_ids:
            self._seen_ids.move_to_end(alert.alert_id)
            return False
        self._seen_ids[alert.alert_id] = None
        while len(self._seen_ids) > self.max_seen_ids:
            self._seen_ids.popitem(last=False)
        now = now or datetime.now(UTC)
        key = (alert.code, alert.source)
        last = self._last_announced.get(key)
        if alert.severity != "critical" and last is not None and now - last < self.cooldown:
            return False
        self._last_announced[key] = now
        return True
