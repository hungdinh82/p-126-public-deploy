from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import UTC, datetime

from src.vivi.domain.models import ActionProposal, VehicleState
from src.vivi.vehicle.zones import ALL_PANELS_LABEL, BODY_PANELS, CABIN_ZONES, selected_zones, zone_label


@dataclass
class VehicleOutcome:
    state: VehicleState
    message: str
    status: str = "verified"


class VehicleSimulator:
    name = "memory"

    ALLOWED_INTENTS = {
        "climate.set_temperature",
        "window.set_position",
        "door.set_open",
        "door.set_lock",
        "hood.set_open",
        "trunk.set_open",
        "body.set_open",
        "seat.set_heat_level",
        "media.play",
        "media.pause",
        "vehicle.get_status",
    }

    def __init__(self):
        self._states: dict[str, VehicleState] = {}
        self._executed: dict[str, tuple[ActionProposal, VehicleState, str]] = {}
        self._lock = threading.Lock()

    async def get_state(self, session_id: str, supplied: VehicleState | None = None) -> VehicleState:
        return self.state_for(session_id, supplied)

    def is_connected(self) -> bool:
        return True

    def state_for(self, session_id: str, supplied: VehicleState | None = None) -> VehicleState:
        if supplied is not None:
            self._states[session_id] = supplied.model_copy(deep=True)
        return self._states.setdefault(session_id, VehicleState()).model_copy(deep=True)

    def set_driving(self, session_id: str, driving: bool) -> VehicleState:
        """Set the local demo fixture without trusting browser state in a turn."""
        with self._lock:
            state = self._states.setdefault(session_id, VehicleState())
            if driving and any(door.open for _, door in state.door_states):
                raise ValueError("Không thể chuyển sang chế độ lái khi cửa xe đang mở.")
            if driving and (state.hood_open or state.trunk_open):
                raise ValueError("Không thể chuyển sang chế độ lái khi nắp capo hoặc cốp sau đang mở.")
            if state.driving != driving:
                state.driving = driving
                state.power_state = "driving" if driving else "off"
                state.state_version += 1
                state.updated_at = datetime.now(UTC)
            return state.model_copy(deep=True)

    def execute_sync(
        self,
        session_id: str,
        turn_id: str,
        action: ActionProposal,
    ) -> tuple[VehicleState, str]:
        """Execute once per session/turn; safe for sync LangGraph nodes."""
        with self._lock:
            execution_key = f"{session_id}:{turn_id}"
            if execution_key in self._executed:
                original, state, message = self._executed[execution_key]
                if original.model_dump(exclude={"spoken_response", "confidence"}) != action.model_dump(
                    exclude={"spoken_response", "confidence"}
                ):
                    raise ValueError("turn_id was already used for a different action")
                return state.model_copy(deep=True), message
            if action.intent not in self.ALLOWED_INTENTS:
                raise ValueError(f"Action is not executable: {action.intent}")
            state = self._states.setdefault(session_id, VehicleState())
            zones = selected_zones(action.arguments) if action.intent in {
                "window.set_position", "door.set_open", "door.set_lock", "seat.set_heat_level"
            } else ()
            if action.intent == "climate.set_temperature":
                state.temperature_celsius = float(action.arguments["value_celsius"])
                message = f"Mình đã đặt nhiệt độ ở {state.temperature_celsius:g} độ."
            elif action.intent == "window.set_position":
                windows = state.window_positions.model_dump()
                value = int(action.arguments["position_percent"])
                for zone in zones:
                    windows[zone] = value
                state = VehicleState.model_validate({**state.model_dump(), "window_positions": windows})
                message = f"Mình đã đặt cửa sổ {zone_label(action.arguments)} ở mức {value} phần trăm."
            elif action.intent == "media.play":
                state.media_playing = True
                message = "Mình đã phát nhạc trong xe mô phỏng."
            elif action.intent == "media.pause":
                state.media_playing = False
                message = "Mình đã dừng nhạc trong xe mô phỏng."
            elif action.intent == "door.set_lock":
                doors = state.door_states.model_dump()
                locked = bool(action.arguments["locked"])
                for zone in zones:
                    doors[zone]["locked"] = locked
                state = VehicleState.model_validate({**state.model_dump(), "door_states": doors})
                message = f"Mình đã {'khóa' if locked else 'mở khóa'} cửa {zone_label(action.arguments)}."
            elif action.intent == "door.set_open":
                doors = state.door_states.model_dump()
                opening = bool(action.arguments["open"])
                for zone in zones:
                    doors[zone]["open"] = opening
                state = VehicleState.model_validate({**state.model_dump(), "door_states": doors})
                message = f"Mình đã {'mở' if opening else 'đóng'} cửa xe {zone_label(action.arguments)}."
            elif action.intent in BODY_PANELS:
                field, target = BODY_PANELS[action.intent]
                opening = bool(action.arguments["open"])
                if opening and state.driving:
                    raise ValueError(f"Không thể mở {target} khi xe đang ở chế độ lái.")
                setattr(state, field, opening)
                message = f"Mình đã {'mở' if opening else 'đóng'} {target}."
            elif action.intent == "body.set_open":
                opening = bool(action.arguments["open"])
                if opening and state.driving:
                    raise ValueError("Không thể mở cửa, capo hay cốp khi xe đang ở chế độ lái.")
                doors = state.door_states.model_dump()
                for zone in CABIN_ZONES:
                    doors[zone]["open"] = opening
                state = VehicleState.model_validate(
                    {**state.model_dump(), "door_states": doors, "hood_open": opening, "trunk_open": opening}
                )
                message = f"Mình đã {'mở' if opening else 'đóng'} {ALL_PANELS_LABEL}."
            elif action.intent == "seat.set_heat_level":
                seats = state.seat_heat_levels.model_dump()
                level = int(action.arguments["level"])
                for zone in zones:
                    seats[zone] = level
                state = VehicleState.model_validate({**state.model_dump(), "seat_heat_levels": seats})
                message = f"Mình đã đặt sưởi ghế {zone_label(action.arguments)} ở mức {level}."
            elif action.intent == "vehicle.get_status":
                message = f"Xe còn {state.battery_percent} phần trăm pin, nhiệt độ {state.temperature_celsius:g} độ."
            else:
                raise ValueError(f"Action is not executable: {action.intent}")
            if action.intent != "vehicle.get_status":
                state.state_version += 1
                state.updated_at = datetime.now(UTC)
                self._states[session_id] = state
            verified = state.model_copy(deep=True)
            self._executed[execution_key] = (action.model_copy(deep=True), verified, message)
            return verified, message

    async def execute(
        self,
        session_id: str,
        turn_id: str,
        action: ActionProposal,
        observed_state: VehicleState | None = None,
    ) -> VehicleOutcome:
        if observed_state is not None:
            current = self.state_for(session_id)
            if current.state_version != observed_state.state_version:
                return VehicleOutcome(
                    current,
                    "Trạng thái xe đã thay đổi. Hãy gửi lại yêu cầu.",
                    "blocked",
                )
        state, message = self.execute_sync(session_id, turn_id, action)
        return VehicleOutcome(state, message)
