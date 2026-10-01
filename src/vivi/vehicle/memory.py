from __future__ import annotations

import threading
from dataclasses import dataclass

from src.vivi.domain.models import ActionProposal, VehicleState


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
            if driving and state.door_driver_open:
                raise ValueError("Không thể chuyển sang chế độ lái khi cửa xe đang mở.")
            if state.driving != driving:
                state.driving = driving
                state.state_version += 1
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
            if action.intent == "climate.set_temperature":
                state.temperature_celsius = float(action.arguments["value_celsius"])
                message = f"Mình đã đặt nhiệt độ ở {state.temperature_celsius:g} độ."
            elif action.intent == "window.set_position":
                state.window_driver_percent = int(action.arguments["position_percent"])
                message = f"Mình đã đặt cửa sổ bên tài ở mức {state.window_driver_percent} phần trăm."
            elif action.intent == "media.play":
                state.media_playing = True
                message = "Mình đã phát nhạc trong xe mô phỏng."
            elif action.intent == "media.pause":
                state.media_playing = False
                message = "Mình đã dừng nhạc trong xe mô phỏng."
            elif action.intent == "door.set_lock":
                state.door_driver_locked = bool(action.arguments["locked"])
                message = "Mình đã khóa cửa bên tài." if state.door_driver_locked else "Mình đã mở khóa cửa bên tài."
            elif action.intent == "door.set_open":
                state.door_driver_open = bool(action.arguments["open"])
                message = "Mình đã mở cửa xe bên tài." if state.door_driver_open else "Mình đã đóng cửa xe bên tài."
            elif action.intent == "seat.set_heat_level":
                state.seat_driver_heat_level = int(action.arguments["level"])
                message = f"Mình đã đặt sưởi ghế bên tài ở mức {state.seat_driver_heat_level}."
            elif action.intent == "vehicle.get_status":
                message = f"Xe còn {state.battery_percent} phần trăm pin, nhiệt độ {state.temperature_celsius:g} độ."
            else:
                raise ValueError(f"Action is not executable: {action.intent}")
            if action.intent != "vehicle.get_status":
                state.state_version += 1
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
