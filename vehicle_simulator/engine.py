from __future__ import annotations

import json
import logging
import math
import sqlite3
import threading
from datetime import UTC
from pathlib import Path
from typing import Any

from .models import CommandResult, FaultScenario, VehicleAck, VehicleCommand, VehicleFixture, VehicleState, utc_now

logger = logging.getLogger(__name__)
CABIN_ZONES = ("driver", "front_passenger", "rear_left", "rear_right")


class VehicleSimulator:
    """Owns simulated vehicle state and command history across process restarts."""

    def __init__(self, database_path: Path):
        database_path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(database_path, isolation_level=None, check_same_thread=False)
        self._db.execute("PRAGMA busy_timeout = 5000")
        self._db.execute("PRAGMA journal_mode = WAL")
        self._db.execute("CREATE TABLE IF NOT EXISTS vehicles (vehicle_id TEXT PRIMARY KEY, state_json TEXT NOT NULL)")
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS commands ("
            "vehicle_id TEXT NOT NULL, idempotency_key TEXT NOT NULL, command_id TEXT NOT NULL, "
            "fingerprint TEXT NOT NULL, ack_json TEXT NOT NULL, "
            "PRIMARY KEY (vehicle_id, idempotency_key), UNIQUE (vehicle_id, command_id))"
        )
        self._lock = threading.RLock()
        self._faults: dict[str, FaultScenario] = {}

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def fault_for(self, vehicle_id: str) -> FaultScenario:
        with self._lock:
            return self._faults.get(vehicle_id, FaultScenario()).model_copy()

    def set_fault(self, vehicle_id: str, fault: FaultScenario) -> FaultScenario:
        with self._lock:
            self._faults[vehicle_id] = fault.model_copy()
            return fault

    def _state_in_transaction(self, vehicle_id: str) -> VehicleState:
        row = self._db.execute("SELECT state_json FROM vehicles WHERE vehicle_id = ?", (vehicle_id,)).fetchone()
        if row:
            return VehicleState.model_validate_json(row[0])
        state = VehicleState(vehicle_id=vehicle_id)
        self._save_state(state)
        return state

    def _save_state(self, state: VehicleState) -> None:
        self._db.execute(
            "INSERT INTO vehicles (vehicle_id, state_json) VALUES (?, ?) "
            "ON CONFLICT(vehicle_id) DO UPDATE SET state_json = excluded.state_json",
            (state.vehicle_id, state.model_dump_json()),
        )

    def get_state(self, vehicle_id: str) -> VehicleState:
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                state = self._state_in_transaction(vehicle_id)
                self._db.commit()
                return state
            except Exception:
                self._db.rollback()
                raise

    def set_fixture(self, vehicle_id: str, fixture: VehicleFixture) -> VehicleState:
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                state = self._state_in_transaction(vehicle_id)
                updates = fixture.model_dump(exclude_none=True)
                if "power_state" in updates:
                    updates["driving"] = updates["power_state"] == "driving"
                elif "driving" in updates:
                    updates["power_state"] = "driving" if updates["driving"] else "off"
                if "window_driver_percent" in updates and "window_positions" not in updates:
                    windows = state.window_positions.model_dump()
                    windows["driver"] = updates["window_driver_percent"]
                    updates["window_positions"] = windows
                if "door_driver_open" in updates and "door_states" not in updates:
                    doors = state.door_states.model_dump()
                    doors["driver"]["open"] = updates["door_driver_open"]
                    updates["door_states"] = doors
                if "seat_driver_heat_level" in updates and "seat_heat_levels" not in updates:
                    seats = state.seat_heat_levels.model_dump()
                    seats["driver"] = updates["seat_driver_heat_level"]
                    updates["seat_heat_levels"] = seats
                merged = {**state.model_dump(), **updates}
                candidate = VehicleState.model_validate(merged)
                if candidate.driving and any(getattr(candidate.door_states, zone).open for zone in CABIN_ZONES):
                    raise ValueError("Cannot set driving while driver door is open")
                if any(getattr(state, name) != getattr(candidate, name) for name in updates):
                    state = VehicleState.model_validate(
                        {**merged, "state_version": state.state_version + 1, "updated_at": utc_now()}
                    )
                    self._save_state(state)
                self._db.commit()
                return state
            except Exception:
                self._db.rollback()
                raise

    def reset(self, vehicle_id: str) -> VehicleState:
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                current = self._state_in_transaction(vehicle_id)
                self._db.execute("DELETE FROM commands WHERE vehicle_id = ?", (vehicle_id,))
                state = VehicleState(vehicle_id=vehicle_id, state_version=current.state_version + 1)
                self._save_state(state)
                self._db.commit()
                self._faults.pop(vehicle_id, None)
                return state
            except Exception:
                self._db.rollback()
                raise

    @staticmethod
    def _fingerprint(command: VehicleCommand) -> str:
        return json.dumps(
            {"correlation_id": command.correlation_id, "action": command.action, "arguments": command.arguments},
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )

    @staticmethod
    def _ack(command: VehicleCommand, state: VehicleState, status: str, reason: str | None = None) -> VehicleAck:
        return VehicleAck(
            command_id=command.command_id,
            correlation_id=command.correlation_id,
            idempotency_key=command.idempotency_key,
            vehicle_id=command.vehicle_id,
            state_version=state.state_version,
            status=status,
            reason_code=reason,
        )

    def execute(self, command: VehicleCommand, fault: FaultScenario | None = None) -> CommandResult:
        fault = fault or FaultScenario()
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                state = self._state_in_transaction(command.vehicle_id)
                fingerprint = self._fingerprint(command)
                prior = self._db.execute(
                    "SELECT command_id, fingerprint, ack_json FROM commands WHERE vehicle_id = ? AND idempotency_key = ?",
                    (command.vehicle_id, command.idempotency_key),
                ).fetchone()
                if prior:
                    if prior[1] != fingerprint:
                        result = CommandResult(
                            ack=self._ack(command, state, "rejected", "idempotency_conflict"), state=state
                        )
                    else:
                        original = VehicleAck.model_validate_json(prior[2])
                        result = CommandResult(
                            ack=VehicleAck(
                                command_id=command.command_id,
                                correlation_id=command.correlation_id,
                                idempotency_key=command.idempotency_key,
                                vehicle_id=command.vehicle_id,
                                state_version=original.state_version,
                                status="duplicate",
                                original_status=original.status,
                                original_command_id=original.command_id,
                            ),
                            state=state,
                        )
                    self._db.commit()
                    self._log_result(command, result, fault)
                    return result

                reused_id = self._db.execute(
                    "SELECT 1 FROM commands WHERE vehicle_id = ? AND command_id = ?",
                    (command.vehicle_id, command.command_id),
                ).fetchone()
                if reused_id:
                    result = CommandResult(
                        ack=self._ack(command, state, "rejected", "idempotency_conflict"), state=state
                    )
                    self._db.commit()
                    self._log_result(command, result, fault)
                    return result

                reason: str | None = None
                now = utc_now()
                if command.expires_at.astimezone(UTC) <= now:
                    reason = "expired"
                elif command.issued_at.astimezone(UTC) > now:
                    reason = "invalid_arguments"
                elif command.action != "vehicle.get_state" and command.expected_state_version is None:
                    reason = "invalid_arguments"
                elif (
                    command.expected_state_version is not None and command.expected_state_version != state.state_version
                ):
                    reason = "state_version_conflict"
                elif fault.mode == "reject":
                    reason = "simulated_reject"

                updated = state
                if reason is None:
                    updated, reason = self._apply_action(state, command.action, command.arguments)
                    if reason is None and fault.mode == "state_mismatch":
                        updated = state
                    if reason is None and updated != state:
                        updated = updated.model_copy(
                            update={"state_version": state.state_version + 1, "updated_at": now}
                        )
                        self._save_state(updated)

                ack = self._ack(command, updated, "rejected" if reason else "applied", reason)
                self._db.execute(
                    "INSERT INTO commands (vehicle_id, idempotency_key, command_id, fingerprint, ack_json) VALUES (?, ?, ?, ?, ?)",
                    (
                        command.vehicle_id,
                        command.idempotency_key,
                        command.command_id,
                        fingerprint,
                        ack.model_dump_json(),
                    ),
                )
                self._db.commit()
                result = CommandResult(ack=ack, state=updated)
                self._log_result(command, result, fault)
                return result
            except Exception:
                self._db.rollback()
                raise

    @staticmethod
    def _log_result(command: VehicleCommand, result: CommandResult, fault: FaultScenario) -> None:
        logger.info(
            "vehicle_command vehicle_id=%s command_id=%s correlation_id=%s action=%s status=%s reason=%s version=%s fault=%s",
            command.vehicle_id,
            command.command_id,
            command.correlation_id,
            command.action,
            result.ack.status,
            result.ack.reason_code,
            result.state.state_version,
            fault.mode,
        )

    @staticmethod
    def _apply_action(state: VehicleState, action: str, args: dict[str, Any]) -> tuple[VehicleState, str | None]:
        def exact_args(*names: str) -> bool:
            return set(args) == set(names)

        def selected_zones() -> tuple[str, ...] | None:
            zone = args.get("zone", "driver")
            if zone == "all":
                return CABIN_ZONES
            if zone in CABIN_ZONES:
                return (zone,)
            return None

        def zoned_args(value_name: str) -> bool:
            return set(args) in ({value_name}, {value_name, "zone"})

        if action == "vehicle.get_state":
            return (state, None) if exact_args() else (state, "invalid_arguments")
        if action == "climate.set_temperature":
            value = args.get("value_celsius")
            if (
                not exact_args("value_celsius")
                or isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or not 16 <= value <= 30
            ):
                return state, "invalid_arguments"
            return state.model_copy(update={"temperature_celsius": float(value)}), None
        if action == "window.set_position":
            value = args.get("position_percent")
            zones = selected_zones()
            if (
                not zoned_args("position_percent")
                or zones is None
                or isinstance(value, bool)
                or not isinstance(value, int)
                or not 0 <= value <= 100
            ):
                return state, "invalid_arguments"
            if state.driving and any(value > getattr(state.window_positions, zone) for zone in zones):
                return state, "policy_invariant"
            windows = state.window_positions.model_dump()
            for zone in zones:
                windows[zone] = value
            return VehicleState.model_validate({**state.model_dump(), "window_positions": windows}), None
        if action in {"media.play", "media.pause"}:
            if not exact_args():
                return state, "invalid_arguments"
            return state.model_copy(update={"media_playing": action == "media.play"}), None
        if action == "door.set_lock":
            value = args.get("locked")
            zones = selected_zones()
            if not zoned_args("locked") or zones is None or not isinstance(value, bool):
                return state, "invalid_arguments"
            if value and any(getattr(state.door_states, zone).open for zone in zones):
                return state, "policy_invariant"
            doors = state.door_states.model_dump()
            for zone in zones:
                doors[zone]["locked"] = value
            return VehicleState.model_validate({**state.model_dump(), "door_states": doors}), None
        if action == "door.set_open":
            value = args.get("open")
            zones = selected_zones()
            if not zoned_args("open") or zones is None or not isinstance(value, bool):
                return state, "invalid_arguments"
            if value and (state.driving or any(getattr(state.door_states, zone).locked for zone in zones)):
                return state, "policy_invariant"
            doors = state.door_states.model_dump()
            for zone in zones:
                doors[zone]["open"] = value
            return VehicleState.model_validate({**state.model_dump(), "door_states": doors}), None
        if action == "seat.set_heat_level":
            value = args.get("level")
            zones = selected_zones()
            if (
                not zoned_args("level")
                or zones is None
                or isinstance(value, bool)
                or not isinstance(value, int)
                or not 0 <= value <= 3
            ):
                return state, "invalid_arguments"
            seats = state.seat_heat_levels.model_dump()
            for zone in zones:
                seats[zone] = value
            return VehicleState.model_validate({**state.model_dump(), "seat_heat_levels": seats}), None
        return state, "unknown_action"
