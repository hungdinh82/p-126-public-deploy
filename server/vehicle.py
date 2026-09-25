from __future__ import annotations

import threading

from .schemas import ActionProposal, VehicleState


class VehicleSimulator:
    ALLOWED_INTENTS = {
        "climate.set_temperature",
        "window.set_position",
        "door.set_open",
        "media.play",
        "media.pause",
        "vehicle.get_status",
    }

    def __init__(self):
        self._states: dict[str, VehicleState] = {}
        self._executed: dict[str, tuple[ActionProposal, VehicleState, str]] = {}
        self._lock = threading.Lock()

    def state_for(self, session_id: str, supplied: VehicleState | None = None) -> VehicleState:
        if supplied is not None:
            self._states[session_id] = supplied.model_copy(deep=True)
        return self._states.setdefault(session_id, VehicleState()).model_copy(deep=True)

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
            elif action.intent == "door.set_open":
                state.driver_door_open = action.arguments["open"]
                message = "Mình đã mở cửa bên tài." if state.driver_door_open else "Mình đã đóng cửa bên tài."
            elif action.intent == "media.play":
                state.media_playing = True
                message = "Mình đã phát nhạc trong xe mô phỏng."
            elif action.intent == "media.pause":
                state.media_playing = False
                message = "Mình đã dừng nhạc trong xe mô phỏng."
            elif action.intent == "vehicle.get_status":
                message = f"Xe còn {state.battery_percent} phần trăm pin, nhiệt độ {state.temperature_celsius:g} độ."
            else:
                raise ValueError(f"Action is not executable: {action.intent}")
            verified = state.model_copy(deep=True)
            self._executed[execution_key] = (action.model_copy(deep=True), verified, message)
            return verified, message

    async def execute(
        self,
        session_id: str,
        turn_id: str,
        action: ActionProposal,
    ) -> tuple[VehicleState, str]:
        return self.execute_sync(session_id, turn_id, action)
