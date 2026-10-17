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
from unittest.mock import patch
from uuid import uuid4

from fastapi.testclient import TestClient

from src.vivi.config import Settings
from src.vivi.domain.models import ActionProposal, TurnRequest, VehicleState
from src.vivi.orchestration import create_langgraph_orchestrator
from src.vivi.persistence.event_store import EventStore
from src.vivi.vehicle.alerts import AlertEngine
from src.vivi.vehicle.mqtt import MqttVehicleAdapter, VehicleUnavailableError
from vehicle_simulator.app import create_app
from vehicle_simulator.engine import VehicleSimulator
from vehicle_simulator.models import FaultScenario, VehicleCommand, VehicleFixture, utc_now
from vehicle_simulator.mqtt import MqttVehicleService

ROOT = Path(__file__).resolve().parents[2]
MOSQUITTO = shutil.which("mosquitto") or next(
    (
        str(path)
        for path in (Path("/opt/homebrew/opt/mosquitto/sbin/mosquitto"), Path("/usr/local/sbin/mosquitto"))
        if path.exists()
    ),
    None,
)


@unittest.skipUnless(MOSQUITTO and shutil.which("mosquitto_passwd"), "Mosquitto not installed")
class MqttVehicleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        directory = Path(self.temp.name)
        self.directory = directory
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            self.port = probe.getsockname()[1]
        subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts/setup_mqtt_broker.py"),
                "--data-dir",
                str(directory / "mqtt"),
                "--port",
                str(self.port),
            ],
            check=True,
            capture_output=True,
        )
        credentials = dict(
            line.split("=", 1) for line in (directory / "mqtt/credentials.env").read_text().splitlines() if "=" in line
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

    def _orchestrator(self):
        config = Settings(
            llm_provider="google",
            data_dir=self.directory / "data",
            rag_handbook_db=self.directory / "handbook.sqlite3",
            rag_history_db=self.directory / "history.sqlite3",
            store_transcripts=False,
        )
        return create_langgraph_orchestrator(self.adapter, EventStore(config), config)

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
        with patch.object(self.adapter, "_get_state", wraps=self.adapter._get_state) as read_after_ack:
            first = asyncio.run(self.adapter.execute("session", "turn-1", action, initial))
        self.assertEqual(read_after_ack.call_count, 1)
        self.assertEqual(first.status, "verified")
        self.assertEqual(first.state.temperature_celsius, 25)
        self.assertEqual(first.state.state_version, initial.state_version + 1)
        repeated = asyncio.run(self.adapter.execute("session", "turn-1", action, first.state))
        self.assertEqual(repeated.status, "verified")
        self.assertEqual(repeated.state.state_version, first.state.state_version)

    def test_unreadable_state_after_ack_is_unverified(self) -> None:
        initial = asyncio.run(self.adapter.get_state("session"))
        action = ActionProposal(intent="media.play")
        with patch.object(self.adapter, "_get_state", side_effect=VehicleUnavailableError("read failed")):
            result = asyncio.run(self.adapter.execute("session", "turn-read-failure", action, initial))
        self.assertEqual(result.status, "unverified")
        self.assertTrue(self.engine.get_state("demo-car-1").media_playing)

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

    def test_delay_over_broker_keeps_verified_result(self) -> None:
        initial = asyncio.run(self.adapter.get_state("session"))
        self.engine.set_fault("demo-car-1", FaultScenario(mode="delay", delay_ms=25))
        result = asyncio.run(
            self.adapter.execute("session", "turn-delay", ActionProposal(intent="media.play"), initial)
        )
        self.assertEqual(result.status, "verified")
        self.assertTrue(result.state.media_playing)

    def test_no_simulator_does_not_supply_cached_state(self) -> None:
        self.service.stop()
        deadline = time.monotonic() + 0.5
        while self.adapter.is_connected() and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertFalse(self.adapter.is_connected())
        with self.assertRaises(VehicleUnavailableError):
            asyncio.run(self.adapter.get_state("session"))

    def test_orchestrator_ignores_browser_state_and_confirms_window(self) -> None:
        orchestrator = self._orchestrator()
        self.addCleanup(orchestrator.graph.close)
        from src.vivi.dialogue.contracts import ActionDraft, QuestionReply

        spoofed = VehicleState(temperature_celsius=30, state_version=999)
        response = asyncio.run(
            orchestrator.run(
                TurnRequest(
                    transcript="Thao tác",
                    session_id="session",
                    turn_id="relative",
                    action=ActionDraft(
                        capability="climate.set_temperature", arguments={"temperature_delta_celsius": 2}
                    ),
                    vehicle_state=spoofed,
                )
            )
        )
        self.assertEqual(response.vehicle_state.temperature_celsius, 25)
        quote = asyncio.run(
            orchestrator.run(
                TurnRequest(
                    transcript="Thao tác",
                    session_id="session",
                    turn_id="window",
                    action=ActionDraft(
                        capability="window.set_position", arguments={"zone": "driver", "position_percent": 100}
                    ),
                )
            )
        )
        self.assertEqual(quote.status, "confirmation_required")
        reply = QuestionReply(question_id=quote.pending_question["id"], option_id="yes")
        done = asyncio.run(
            orchestrator.run(
                TurnRequest(transcript="Trả lời", session_id="session", turn_id="approve", question_reply=reply)
            )
        )
        self.assertEqual(done.vehicle_state.window_driver_percent, 100)
        stale = asyncio.run(
            orchestrator.run(
                TurnRequest(transcript="Trả lời", session_id="session", turn_id="stale", question_reply=reply)
            )
        )
        self.assertEqual(stale.status, "clarify")

    def test_door_confirmation_and_seat_intent_over_mqtt(self) -> None:
        from src.vivi.dialogue.contracts import ActionDraft, QuestionReply

        orchestrator = self._orchestrator()
        self.addCleanup(orchestrator.graph.close)

        def action(capability, arguments, turn):
            return asyncio.run(
                orchestrator.run(
                    TurnRequest(
                        transcript="Thao tác",
                        session_id="session",
                        turn_id=turn,
                        action=ActionDraft(capability=capability, arguments=arguments),
                    )
                )
            )

        def approve(quote, turn):
            return asyncio.run(
                orchestrator.run(
                    TurnRequest(
                        transcript="Trả lời",
                        session_id="session",
                        turn_id=turn,
                        question_reply=QuestionReply(question_id=quote.pending_question["id"], option_id="yes"),
                    )
                )
            )

        opening = action("door.set_open", {"zone": "driver", "open": True}, "open")
        self.assertEqual(opening.status, "confirmation_required")
        self.assertTrue(approve(opening, "approve-open").vehicle_state.door_driver_open)
        self.assertEqual(action("door.set_lock", {"zone": "driver", "locked": True}, "lock-open").status, "blocked")
        closing = action("door.set_open", {"zone": "driver", "open": False}, "close")
        self.assertEqual(approve(closing, "approve-close").status, "verified")
        locking = action("door.set_lock", {"zone": "driver", "locked": True}, "lock")
        self.assertEqual(approve(locking, "approve-lock").status, "verified")
        self.assertEqual(action("door.set_open", {"zone": "driver", "open": True}, "open-locked").status, "blocked")
        seat = action("seat.set_heat_level", {"zone": "front_passenger", "level": 2}, "seat")
        self.assertEqual(seat.vehicle_state.seat_heat_levels.front_passenger, 2)

    def test_confirmation_rejects_changed_vehicle_state_over_mqtt(self) -> None:
        from src.vivi.dialogue.contracts import ActionDraft, QuestionReply

        orchestrator = self._orchestrator()
        self.addCleanup(orchestrator.graph.close)
        quote = asyncio.run(
            orchestrator.run(
                TurnRequest(
                    transcript="Thao tác",
                    session_id="stale-session",
                    turn_id="open",
                    action=ActionDraft(capability="door.set_open", arguments={"zone": "driver", "open": True}),
                )
            )
        )
        self.assertEqual(quote.status, "confirmation_required")
        self.engine.set_fixture("demo-car-1", VehicleFixture(driving=True))
        confirmed = asyncio.run(
            orchestrator.run(
                TurnRequest(
                    transcript="Trả lời",
                    session_id="stale-session",
                    turn_id="approve",
                    question_reply=QuestionReply(question_id=quote.pending_question["id"], option_id="yes"),
                )
            )
        )
        self.assertEqual(confirmed.status, "blocked")
        self.assertFalse(self.engine.get_state("demo-car-1").door_driver_open)

    def test_http_fixture_publishes_state_to_adapter_and_alert_engine(self) -> None:
        asyncio.run(self.adapter.get_state("fixture-session"))
        with self.adapter._condition:
            before = self.adapter._state_sequence
        app = create_app(
            self.directory / "unused.sqlite3",
            enable_test_control=True,
            simulator=self.engine,
            publish_state=self.service.publish_state,
        )
        with TestClient(app) as client:
            response = client.put(
                "/api/v1/test/vehicles/demo-car-1/fixture",
                json={"battery_percent": 10},
            )
            self.assertEqual(response.status_code, 200, response.text)

        deadline = time.monotonic() + 2
        with self.adapter._condition:
            while self.adapter._state_sequence <= before and time.monotonic() < deadline:
                self.adapter._condition.wait(deadline - time.monotonic())
            received = self.adapter._state
        self.assertIsNotNone(received)
        self.assertEqual(received.battery_percent, 10)
        domain_state = VehicleState.model_validate(received.model_dump())
        snapshot = AlertEngine().evaluate(domain_state)
        self.assertEqual([(item.code, item.source) for item in snapshot.active_alerts], [("LOW_BATTERY", "battery")])

    def test_mqtt_clients_reconnect_after_broker_restart(self) -> None:
        asyncio.run(self.adapter.get_state("before-restart"))
        self.broker.terminate()
        self.broker.communicate(timeout=3)
        deadline = time.monotonic() + 3
        while (self.service.connected.is_set() or self.adapter.is_connected()) and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertFalse(self.service.connected.is_set())

        self.broker = subprocess.Popen(
            [MOSQUITTO, "-c", str(self.directory / "mqtt/mosquitto.conf")],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        deadline = time.monotonic() + 10
        state = None
        while time.monotonic() < deadline:
            try:
                state = asyncio.run(self.adapter.get_state("after-restart"))
                break
            except VehicleUnavailableError:
                time.sleep(0.2)
        self.assertIsNotNone(state)
        self.assertTrue(self.service.connected.is_set())
        self.assertEqual(state.vehicle_id, "demo-car-1")
