from __future__ import annotations

import asyncio
from dataclasses import dataclass

from .schemas import ActionProposal, VehicleState


@dataclass
class VehicleOutcome:
    state: VehicleState
    message: str
    status: str = "verified"


class VehicleSimulator:
    name = "memory"

    def __init__(self):
        self._states: dict[str, VehicleState] = {}
        self._executed: dict[tuple[str, str], VehicleOutcome] = {}
        self._lock = asyncio.Lock()

    async def get_state(self, session_id: str, supplied: VehicleState | None = None) -> VehicleState:
        return self.state_for(session_id, supplied)

    def is_connected(self) -> bool:
        return True

    def state_for(self, session_id: str, supplied: VehicleState | None = None) -> VehicleState:
        if supplied is not None:
            self._states[session_id] = supplied.model_copy(deep=True)
        return self._states.setdefault(session_id, VehicleState()).model_copy(deep=True)

    async def execute(
        self, session_id: str, turn_id: str, action: ActionProposal, observed_state: VehicleState
    ) -> VehicleOutcome:
        async with self._lock:
            key = (session_id, turn_id)
            if key in self._executed:
                prior = self._executed[key]
                return VehicleOutcome(prior.state.model_copy(deep=True), prior.message, prior.status)
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
            elif action.intent == "manual.search":
                message = "Mình đã mở cẩm nang minh họa."
            else:
                message = action.spoken_response
            verified = state.model_copy(deep=True)
            outcome = VehicleOutcome(verified, message)
            self._executed[key] = outcome
            return outcome
