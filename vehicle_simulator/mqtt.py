from __future__ import annotations

import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import paho.mqtt.client as mqtt
from pydantic import ValidationError

from .engine import VehicleSimulator
from .models import VehicleAck, VehicleCommand, VehicleState, utc_now

logger = logging.getLogger(__name__)


def topic(vehicle_id: str, kind: str) -> str:
    return f"vivi/v1/vehicles/{vehicle_id}/{kind}"


class MqttVehicleService:
    """MQTT transport around the durable simulator engine."""

    def __init__(
        self,
        simulator: VehicleSimulator,
        vehicle_id: str,
        *,
        host: str = "127.0.0.1",
        port: int = 1883,
        username: str | None = None,
        password: str | None = None,
    ):
        self.simulator = simulator
        self.vehicle_id = vehicle_id
        self.host = host
        self.port = port
        self.connected = Event()
        self._worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="vehicle-mqtt")
        self.client = mqtt.Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
            client_id=f"vivi-simulator-{vehicle_id}",
            protocol=mqtt.MQTTv311,
        )
        if username:
            self.client.username_pw_set(username, password)
        self.client.will_set(
            topic(vehicle_id, "availability"),
            json.dumps({"vehicle_id": vehicle_id, "status": "offline"}),
            qos=1,
            retain=True,
        )
        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect
        self.client.on_message = self._on_message
        self.client.on_subscribe = self._on_subscribe

    def start(self, timeout: float = 5.0) -> None:
        self.client.connect(self.host, self.port, keepalive=30)
        self.client.loop_start()
        if not self.connected.wait(timeout):
            self.client.loop_stop()
            raise TimeoutError("MQTT broker did not accept simulator connection")

    def stop(self) -> None:
        self._worker.shutdown(wait=True)
        if self.connected.is_set():
            info = self._publish(
                "availability",
                {"vehicle_id": self.vehicle_id, "status": "offline", "updated_at": utc_now().isoformat()},
                retain=True,
            )
            if info is not None:
                try:
                    info.wait_for_publish(timeout=2)
                except (RuntimeError, ValueError):
                    logger.warning("Could not confirm offline availability publish")
        self.client.disconnect()
        self.client.loop_stop()

    def publish_state(self, state: VehicleState | None = None):
        """Publish the latest state as a retained MQTT message."""
        state = state or self.simulator.get_state(self.vehicle_id)
        if state.vehicle_id != self.vehicle_id:
            raise ValueError("vehicle_id does not match MQTT service")
        return self._publish("state", state.model_dump(mode="json"), retain=True)

    def _on_connect(self, client, _userdata, _flags, reason_code, _properties) -> None:
        if reason_code.is_failure:
            logger.error("Simulator MQTT connection refused: %s", reason_code)
            return
        result, _mid = client.subscribe(topic(self.vehicle_id, "commands"), qos=1)
        if result != mqtt.MQTT_ERR_SUCCESS:
            logger.error("Simulator MQTT subscribe failed: %s", result)

    def _on_subscribe(self, _client, _userdata, _mid, reason_codes, _properties) -> None:
        if any(code.is_failure for code in reason_codes):
            logger.error("Simulator MQTT subscription refused: %s", reason_codes)
            return
        self.publish_state()
        self._publish(
            "availability",
            {"vehicle_id": self.vehicle_id, "status": "online", "updated_at": utc_now().isoformat()},
            retain=True,
        )
        self.connected.set()

    def _on_disconnect(self, _client, _userdata, _flags, _reason_code, _properties) -> None:
        self.connected.clear()

    def _on_message(self, _client, _userdata, message) -> None:
        if message.topic != topic(self.vehicle_id, "commands") or message.retain:
            return
        try:
            command = VehicleCommand.model_validate_json(message.payload)
        except ValidationError:
            logger.warning("Ignoring malformed vehicle command", exc_info=True)
            return
        self._worker.submit(self._process, command)

    def _process(self, command: VehicleCommand) -> None:
        fault = self.simulator.fault_for(self.vehicle_id)
        if command.vehicle_id != self.vehicle_id:
            state = self.simulator.get_state(self.vehicle_id)
            ack = VehicleAck(
                command_id=command.command_id,
                correlation_id=command.correlation_id,
                idempotency_key=command.idempotency_key,
                vehicle_id=self.vehicle_id,
                state_version=state.state_version,
                status="rejected",
                reason_code="vehicle_mismatch",
            )
            self._publish("acks", ack.model_dump(mode="json"))
            return
        if fault.mode in {"timeout_before_apply", "disconnected"}:
            return
        if fault.mode == "delay" and fault.delay_ms:
            time.sleep(fault.delay_ms / 1000)
        try:
            result = self.simulator.execute(command, fault)
        except Exception:
            logger.exception("Vehicle command failed unexpectedly: %s", command.command_id)
            return
        self.publish_state(result.state)
        if fault.mode != "ack_lost_after_apply" or result.ack.status != "applied":
            self._publish("acks", result.ack.model_dump(mode="json"))

    def _publish(self, kind: str, payload: dict, *, retain: bool = False) -> mqtt.MQTTMessageInfo | None:
        info = self.client.publish(
            topic(self.vehicle_id, kind),
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            qos=1,
            retain=retain,
        )
        if info.rc != mqtt.MQTT_ERR_SUCCESS:
            logger.error("MQTT publish failed for %s: %s", kind, info.rc)
            return None
        return info
