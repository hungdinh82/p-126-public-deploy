from __future__ import annotations

import asyncio

from .schemas import ActionProposal, VehicleState


class VehicleSimulator:
    def __init__(self):
        self._states: dict[str, VehicleState] = {}
        self._executed: dict[str, tuple[VehicleState, str]] = {}
        self._lock = asyncio.Lock()

    def state_for(self, session_id: str, supplied: VehicleState | None = None) -> VehicleState:
        if supplied is not None:
            self._states[session_id] = supplied.model_copy(deep=True)
        return self._states.setdefault(session_id, VehicleState()).model_copy(deep=True)

    async def execute(self, session_id: str, turn_id: str, action: ActionProposal) -> tuple[VehicleState, str]:
        async with self._lock:
            if turn_id in self._executed:
                state, message = self._executed[turn_id]
                return state.model_copy(deep=True), message
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
            elif action.intent == "vehicle.get_status":
                message = f"Xe còn {state.battery_percent} phần trăm pin, nhiệt độ {state.temperature_celsius:g} độ."
            elif action.intent == "manual.search":
                message = "Mình đã mở cẩm nang minh họa."
            else:
                message = action.spoken_response
            verified = state.model_copy(deep=True)
            self._executed[turn_id] = (verified, message)
            return verified, message

