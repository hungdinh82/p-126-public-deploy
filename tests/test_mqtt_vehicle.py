from __future__ import annotations

import asyncio
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

from server.adapters.llm import RulesAdapter
from server.orchestrator import Orchestrator
from server.schemas import ActionProposal, TurnRequest, VehicleState
from server.vehicle_mqtt import MqttVehicleAdapter, VehicleUnavailableError
from vehicle_simulator.engine import VehicleSimulator
from vehicle_simulator.models import FaultScenario, VehicleCommand, utc_now
from vehicle_simulator.mqtt import MqttVehicleService

ROOT = Path(__file__).resolve().parent.parent
MOSQUITTO = shutil.which("mosquitto") or next(
    (str(path) for path in (Path("/opt/homebrew/opt/mosquitto/sbin/mosquitto"), Path("/usr/local/sbin/mosquitto")) if path.exists()),
    None,
)


@unittest.skipUnless(MOSQUITTO and shutil.which("mosquitto_passwd"), "Mosquitto not installed")
class MqttVehicleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        directory = Path(self.temp.name)
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            self.port = probe.getsockname()[1]
        subprocess.run(
            [sys.executable, str(ROOT / "scripts/setup_mqtt_broker.py"), "--data-dir", str(directory / "mqtt"), "--port", str(self.port)],
            check=True,
            capture_output=True,
        )
        credentials = dict(
            line.split("=", 1)
            for line in (directory / "mqtt/credentials.env").read_text().splitlines()
            if "=" in line
        )
        self.broker = subprocess.Popen(
            [MOSQUITTO, "-c", str(directory / "mqtt/mosquitto.conf")],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        self.addCleanup(self._stop_broker)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if self.broker.poll() is not None:
                raise RuntimeError(f"Mosquitto exited: {self.broker.stderr.read().decode()}")
            try:
                with socket.create_connection(("127.0.0.1", self.port), timeout=0.1):
                    break
            except OSError:
                time.sleep(0.02)
        else:
            self.fail("Mosquitto did not start")
        self.engine = VehicleSimulator(directory / "vehicle.sqlite3")
        self.addCleanup(self.engine.close)
        self.service = MqttVehicleService(
            self.engine,
            "demo-car-1",
            port=self.port,
            username=credentials["MQTT_SIM_USERNAME"],
            password=credentials["MQTT_SIM_PASSWORD"],
        )
        self.service.start()
        self.addCleanup(self.service.stop)
        self.adapter = MqttVehicleAdapter(
            "demo-car-1",
            port=self.port,
            username=credentials["MQTT_API_USERNAME"],
            password=credentials["MQTT_API_PASSWORD"],
            timeout=0.7,
        )
        self.addCleanup(self.adapter.close)

    def _stop_broker(self) -> None:
        self.broker.terminate()
        try:
            self.broker.communicate(timeout=3)
        except subprocess.TimeoutExpired:
            self.broker.kill()
            self.broker.communicate()

    def test_command_ack_state_and_idempotency(self) -> None:
        initial = asyncio.run(self.adapter.get_state("session"))
        action = ActionProposal(intent="climate.set_temperature", arguments={"value_celsius": 25})
        first = asyncio.run(self.adapter.execute("session", "turn-1", action, initial))
        self.assertEqual(first.status, "verified")
        self.assertEqual(first.state.temperature_celsius, 25)
        self.assertEqual(first.state.state_version, initial.state_version + 1)
        repeated = asyncio.run(self.adapter.execute("session", "turn-1", action, first.state))
        self.assertEqual(repeated.status, "verified")
        self.assertEqual(repeated.state.state_version, first.state.state_version)

    def test_door_and_seat_catalog_over_mqtt(self) -> None:
        for action, arguments, field, value in (
            ("door.set_lock", {"locked": True}, "door_driver_locked", True),
            ("seat.set_heat_level", {"level": 2}, "seat_driver_heat_level", 2),
        ):
            with self.subTest(action=action):
                now = utc_now()
                identifier = str(uuid4())
                command = VehicleCommand(
                    command_id=identifier,
                    correlation_id=identifier,
                    idempotency_key=identifier,
                    vehicle_id="demo-car-1",
                    action=action,
                    arguments=arguments,
                    issued_at=now,
                    expires_at=now + timedelta(seconds=5),
                    expected_state_version=self.engine.get_state("demo-car-1").state_version,
                )
                ack, state = self.adapter._exchange(command)
                self.assertEqual(ack.status, "applied")
                self.assertEqual(getattr(state, field), value)

    def test_lost_ack_is_not_reported_as_success(self) -> None:
        initial = asyncio.run(self.adapter.get_state("session"))
        self.engine.set_fault("demo-car-1", FaultScenario(mode="ack_lost_after_apply"))
        action = ActionProposal(intent="media.play")
        result = asyncio.run(self.adapter.execute("session", "turn-lost", action, initial))
        self.assertEqual(result.status, "unverified")
        self.assertTrue(self.engine.get_state("demo-car-1").media_playing)

    def test_rejection_is_blocked(self) -> None:
        initial = asyncio.run(self.adapter.get_state("session"))
        self.engine.set_fault("demo-car-1", FaultScenario(mode="reject"))
        action = ActionProposal(intent="media.play")
        result = asyncio.run(self.adapter.execute("session", "turn-reject", action, initial))
        self.assertEqual(result.status, "blocked")
        self.assertFalse(result.state.media_playing)

    def test_state_mismatch_and_pre_apply_timeout_are_unverified(self) -> None:
        initial = asyncio.run(self.adapter.get_state("session"))
        self.engine.set_fault("demo-car-1", FaultScenario(mode="state_mismatch"))
        mismatch = asyncio.run(
            self.adapter.execute("session", "turn-mismatch", ActionProposal(intent="media.play"), initial)
        )
        self.assertEqual(mismatch.status, "unverified")
        self.assertFalse(mismatch.state.media_playing)
        self.engine.set_fault("demo-car-1", FaultScenario(mode="timeout_before_apply"))
        timeout = asyncio.run(
            self.adapter.execute("session", "turn-timeout", ActionProposal(intent="media.play"), initial)
        )
        self.assertEqual(timeout.status, "unverified")
        self.assertFalse(self.engine.get_state("demo-car-1").media_playing)

    def test_no_simulator_does_not_supply_cached_state(self) -> None:
        self.service.stop()
        with self.assertRaises(VehicleUnavailableError):
            asyncio.run(self.adapter.get_state("session"))

    def test_orchestrator_ignores_browser_state_and_blocks_window(self) -> None:
        class Events:
            def append_event(self, _event):
                pass

        orchestrator = Orchestrator(RulesAdapter(), self.adapter, Events())
        spoofed = VehicleState(temperature_celsius=30, state_version=999)
        response = asyncio.run(
            orchestrator.run(
                TurnRequest(transcript="Tôi hơi lạnh", session_id="session", turn_id="turn-relative", vehicle_state=spoofed)
            )
        )
        self.assertEqual(response.status, "verified")
        self.assertEqual(response.vehicle_state.temperature_celsius, 25)
        window = asyncio.run(
            orchestrator.run(TurnRequest(transcript="Mở cửa sổ bên tài", session_id="session", turn_id="turn-window"))
        )
        self.assertEqual(window.status, "blocked")
        self.assertEqual(self.engine.get_state("demo-car-1").window_driver_percent, 0)
