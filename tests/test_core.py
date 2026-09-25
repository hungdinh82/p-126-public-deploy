from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from server.adapters.llm import RulesAdapter
from server.app import app
from server.config import Settings
from server.data_store import DataStore
from server.orchestrator import Orchestrator
from server.safety import validate
from server.schemas import ActionProposal, TurnRequest, VehicleState
from server.vehicle import VehicleSimulator


class SafetyTests(unittest.TestCase):
    def test_temperature_bounds(self):
        proposal = ActionProposal(intent="climate.set_temperature", arguments={"value_celsius": 35})
        result = validate(proposal, VehicleState())
        self.assertFalse(result.allowed)
        self.assertEqual(result.status, "blocked")

    def test_window_open_is_blocked_while_driving(self):
        proposal = ActionProposal(intent="window.set_position", arguments={"position_percent": 100})
        result = validate(proposal, VehicleState(driving=True))
        self.assertFalse(result.allowed)
        self.assertEqual(result.status, "blocked")

    def test_window_close_is_allowed_while_driving(self):
        proposal = ActionProposal(intent="window.set_position", arguments={"position_percent": 0})
        result = validate(proposal, VehicleState(driving=True, window_driver_percent=100))
        self.assertTrue(result.allowed)

    def test_door_and_seat_safety(self):
        opening = ActionProposal(intent="door.set_open", arguments={"open": True})
        self.assertEqual(validate(opening, VehicleState(driving=True)).status, "blocked")
        self.assertEqual(validate(opening, VehicleState(door_driver_locked=True)).status, "blocked")
        locking = ActionProposal(intent="door.set_lock", arguments={"locked": True})
        self.assertEqual(validate(locking, VehicleState(door_driver_open=True)).status, "blocked")
        too_hot = ActionProposal(intent="seat.set_heat_level", arguments={"level": 4})
        self.assertEqual(validate(too_hot, VehicleState()).status, "blocked")


class RulesTests(unittest.IsolatedAsyncioTestCase):
    async def test_cold_increases_temperature(self):
        result = await RulesAdapter().propose("Tôi hơi lạnh", VehicleState(temperature_celsius=23))
        self.assertEqual(result.intent, "climate.set_temperature")
        self.assertEqual(result.arguments["value_celsius"], 25)

    async def test_negation_clarifies(self):
        result = await RulesAdapter().propose("Đừng mở cửa sổ", VehicleState())
        self.assertEqual(result.intent, "conversation.clarify")

    async def test_door_and_seat_intents(self):
        rules = RulesAdapter()
        state = VehicleState()
        cases = (
            ("Mở cửa xe bên tài", "door.set_open", {"open": True}),
            ("Đóng cửa xe bên tài", "door.set_open", {"open": False}),
            ("Khóa cửa xe", "door.set_lock", {"locked": True}),
            ("Mở khóa cửa xe", "door.set_lock", {"locked": False}),
            ("Sưởi ghế mức 2", "seat.set_heat_level", {"level": 2}),
            ("Tắt sưởi ghế", "seat.set_heat_level", {"level": 0}),
        )
        for text, intent, arguments in cases:
            with self.subTest(text=text):
                proposal = await rules.propose(text, state)
                self.assertEqual((proposal.intent, proposal.arguments), (intent, arguments))
        unrelated = await rules.propose("Nhiệt độ của xe 25 độ", state)
        self.assertEqual(unrelated.intent, "climate.set_temperature")

    async def test_idempotent_turn(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Settings(data_dir=Path(directory))
            orchestrator = Orchestrator(RulesAdapter(), VehicleSimulator(), DataStore(config))
            request = TurnRequest(transcript="Tăng nhiệt độ 1 độ", session_id="s1", turn_id="t1")
            first = await orchestrator.run(request)
            second = await orchestrator.run(request)
            self.assertEqual(first.vehicle_state.temperature_celsius, 24)
            self.assertEqual(second.vehicle_state.temperature_celsius, 24)


class APITests(unittest.TestCase):
    def test_health(self):
        response = TestClient(app).get("/api/v1/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "ok")

    def test_rule_turn(self):
        local_orchestrator = Orchestrator(
            RulesAdapter(), VehicleSimulator(), DataStore(Settings(store_transcripts=False))
        )
        with patch("server.app.orchestrator", local_orchestrator):
            response = TestClient(app).post("/api/v1/turn", json={
                "transcript": "Đặt nhiệt độ 25 độ",
                "session_id": "api-session",
                "turn_id": "api-turn",
            })
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["status"], "verified")
        self.assertEqual(payload["vehicle_state"]["temperature_celsius"], 25)

    def test_confirmation_contract_over_http(self):
        local_orchestrator = Orchestrator(
            RulesAdapter(), VehicleSimulator(), DataStore(Settings(store_transcripts=False))
        )
        with patch("server.app.orchestrator", local_orchestrator):
            client = TestClient(app)
            quoted = client.post("/api/v1/turn", json={
                "transcript": "Mở cửa xe bên tài", "session_id": "confirm-session", "turn_id": "request-open",
            }).json()
            self.assertEqual(quoted["status"], "confirm")
            self.assertFalse(quoted["vehicle_state"]["door_driver_open"])
            self.assertTrue(quoted["confirmation_id"])
            approved = client.post("/api/v1/turn", json={
                "transcript": "Xác nhận", "session_id": "confirm-session", "turn_id": "approve-open",
                "confirmation_id": quoted["confirmation_id"],
            }).json()
            self.assertEqual(approved["status"], "verified")
            self.assertTrue(approved["vehicle_state"]["door_driver_open"])


if __name__ == "__main__":
    unittest.main()
