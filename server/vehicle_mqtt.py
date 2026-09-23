from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from datetime import timedelta
from uuid import NAMESPACE_URL, uuid4, uuid5

import paho.mqtt.client as mqtt
from pydantic import ValidationError

from vehicle_simulator.models import VehicleAck, VehicleCommand, utc_now
from vehicle_simulator.models import VehicleState as SimulatorState
from vehicle_simulator.mqtt import topic

from .schemas import ActionProposal, VehicleState
from .vehicle import VehicleOutcome

logger = logging.getLogger(__name__)


class VehicleUnavailableError(RuntimeError):
    pass


class MqttVehicleAdapter:
    name = "mqtt"

    def __init__(
        self,
        vehicle_id: str,
        *,
        host: str = "127.0.0.1",
        port: int = 1883,
        username: str | None = None,
        password: str | None = None,
        timeout: float = 3.0,
    ):
        self.vehicle_id = vehicle_id
        self.host = host
        self.port = port
        self.timeout = timeout
        self._connected = threading.Event()
        self._vehicle_online = threading.Event()
        self._start_lock = threading.Lock()
        self._request_lock = threading.Lock()
        self._condition = threading.Condition()
        self._started = False
        self._state: SimulatorState | None = None
        self._state_sequence = 0
        self._acks: dict[str, VehicleAck] = {}
        self._pending_id: str | None = None
        self.client = mqtt.Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
            client_id=f"vivi-api-{uuid4()}",
            protocol=mqtt.MQTTv311,
        )
        if username:
            self.client.username_pw_set(username, password)
        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect
        self.client.on_message = self._on_message
        self.client.on_subscribe = self._on_subscribe

    def is_connected(self) -> bool:
        return self._connected.is_set() and self._vehicle_online.is_set()

    def close(self) -> None:
        if self._started:
            self.client.disconnect()
            self.client.loop_stop()
            self._started = False

    def _on_connect(self, client, _userdata, _flags, reason_code, _properties) -> None:
        if reason_code.is_failure:
            logger.error("ViVi MQTT connection refused: %s", reason_code)
            return
        result, _mid = client.subscribe([(topic(self.vehicle_id, kind), 1) for kind in ("acks", "state", "availability")])
        if result != mqtt.MQTT_ERR_SUCCESS:
            logger.error("ViVi MQTT subscribe failed: %s", result)

    def _on_subscribe(self, _client, _userdata, _mid, reason_codes, _properties) -> None:
        if any(code.is_failure for code in reason_codes):
            logger.error("ViVi MQTT subscription refused: %s", reason_codes)
            return
        self._connected.set()
        with self._condition:
            self._condition.notify_all()

    def _on_disconnect(self, _client, _userdata, _flags, _reason_code, _properties) -> None:
        self._connected.clear()
        self._vehicle_online.clear()
        with self._condition:
            self._condition.notify_all()

    def _on_message(self, _client, _userdata, message) -> None:
        try:
            if message.topic == topic(self.vehicle_id, "acks"):
                ack = VehicleAck.model_validate_json(message.payload)
                with self._condition:
                    if ack.command_id == self._pending_id:
                        self._acks[ack.command_id] = ack
                        self._condition.notify_all()
            elif message.topic == topic(self.vehicle_id, "state"):
                state = SimulatorState.model_validate_json(message.payload)
                with self._condition:
                    self._state = state
                    self._state_sequence += 1
                    self._condition.notify_all()
            elif message.topic == topic(self.vehicle_id, "availability"):
                availability = json.loads(message.payload)
                if availability.get("vehicle_id") == self.vehicle_id:
                    if availability.get("status") == "online":
                        self._vehicle_online.set()
                    elif availability.get("status") == "offline":
                        self._vehicle_online.clear()
                    with self._condition:
                        self._condition.notify_all()
        except (ValidationError, ValueError):
            logger.warning("Ignoring invalid MQTT vehicle message", exc_info=True)

    def _ensure_connected(self) -> None:
        with self._start_lock:
            if not self._started:
                try:
                    self.client.connect(self.host, self.port, keepalive=30)
                except OSError as exc:
                    raise VehicleUnavailableError("MQTT broker unavailable") from exc
                self.client.loop_start()
                self._started = True
        if not self._connected.wait(self.timeout):
            raise VehicleUnavailableError("MQTT broker not connected")

    def _exchange(self, command: VehicleCommand) -> tuple[VehicleAck, SimulatorState]:
        self._ensure_connected()
        with self._request_lock:
            with self._condition:
                state_sequence = self._state_sequence
                self._acks.pop(command.command_id, None)
                self._pending_id = command.command_id
            info = self.client.publish(topic(self.vehicle_id, "commands"), command.model_dump_json(), qos=1, retain=False)
            if info.rc != mqtt.MQTT_ERR_SUCCESS:
                with self._condition:
                    self._pending_id = None
                raise VehicleUnavailableError("MQTT command was not published")
            deadline = time.monotonic() + self.timeout
            with self._condition:
                while True:
                    ack = self._acks.get(command.command_id)
                    state = self._state
                    if ack and state and self._state_sequence > state_sequence and state.state_version >= ack.state_version:
                        self._acks.pop(command.command_id, None)
                        self._pending_id = None
                        return ack, state
                    remaining = deadline - time.monotonic()
                    if remaining <= 0 or not self._connected.is_set():
                        self._acks.pop(command.command_id, None)
                        self._pending_id = None
                        raise VehicleUnavailableError("No matching MQTT acknowledgement and state")
                    self._condition.wait(remaining)

    async def get_state(self, _session_id: str, _supplied: VehicleState | None = None) -> VehicleState:
        return await asyncio.to_thread(self._get_state)

    def _get_state(self) -> VehicleState:
        now = utc_now()
        identifier = str(uuid4())
        command = VehicleCommand(
            command_id=identifier,
            correlation_id=identifier,
            idempotency_key=f"{self.vehicle_id}:state:{identifier}",
            vehicle_id=self.vehicle_id,
            action="vehicle.get_state",
            issued_at=now,
            expires_at=now + timedelta(seconds=self.timeout + 2),
        )
        ack, state = self._exchange(command)
        if ack.status != "applied" or not self._ack_matches(ack, command):
            raise VehicleUnavailableError("Vehicle state request was rejected")
        return VehicleState.model_validate(state.model_dump())

    async def execute(
        self, session_id: str, turn_id: str, action: ActionProposal, observed_state: VehicleState
    ) -> VehicleOutcome:
        return await asyncio.to_thread(self._execute, session_id, turn_id, action, observed_state)

    def _execute(
        self, session_id: str, turn_id: str, action: ActionProposal, observed_state: VehicleState
    ) -> VehicleOutcome:
        now = utc_now()
        key = f"{self.vehicle_id}:{session_id}:{turn_id}"
        command = VehicleCommand(
            command_id=str(uuid5(NAMESPACE_URL, key)),
            correlation_id=turn_id,
            idempotency_key=key,
            vehicle_id=self.vehicle_id,
            action=action.intent,
            arguments=action.arguments,
            issued_at=now,
            expires_at=now + timedelta(seconds=self.timeout + 2),
            expected_state_version=observed_state.state_version,
        )
        try:
            ack, state = self._exchange(command)
        except VehicleUnavailableError:
            return VehicleOutcome(observed_state, "Chưa xác minh được thao tác với xe mô phỏng.", "unverified")
        current = VehicleState.model_validate(state.model_dump())
        if not self._ack_matches(ack, command):
            return VehicleOutcome(current, "Phản hồi từ xe không khớp lệnh đã gửi.", "unverified")
        if ack.status == "rejected" or (ack.status == "duplicate" and ack.original_status == "rejected"):
            return VehicleOutcome(current, f"Xe mô phỏng từ chối lệnh: {ack.reason_code or 'unknown'}.", "blocked")
        if not self._matches_action(action, current):
            return VehicleOutcome(current, "Xe chưa đạt trạng thái được yêu cầu.", "unverified")
        return VehicleOutcome(current, self._success_message(action, current), "verified")

    @staticmethod
    def _ack_matches(ack: VehicleAck, command: VehicleCommand) -> bool:
        return (
            ack.command_id == command.command_id
            and ack.correlation_id == command.correlation_id
            and ack.idempotency_key == command.idempotency_key
            and ack.vehicle_id == command.vehicle_id
        )

    @staticmethod
    def _matches_action(action: ActionProposal, state: VehicleState) -> bool:
        if action.intent == "climate.set_temperature":
            return state.temperature_celsius == float(action.arguments["value_celsius"])
        if action.intent == "window.set_position":
            return state.window_driver_percent == int(action.arguments["position_percent"])
        if action.intent == "media.play":
            return state.media_playing
        if action.intent == "media.pause":
            return not state.media_playing
        return False

    @staticmethod
    def _success_message(action: ActionProposal, state: VehicleState) -> str:
        if action.intent == "climate.set_temperature":
            return f"Mình đã đặt nhiệt độ ở {state.temperature_celsius:g} độ."
        if action.intent == "window.set_position":
            return f"Mình đã đặt cửa sổ bên tài ở mức {state.window_driver_percent} phần trăm."
        return "Mình đã cập nhật trạng thái nhạc trong xe mô phỏng."
