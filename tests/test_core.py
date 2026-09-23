from __future__ import annotations

import tempfile
import unittest
import json
import zipfile
from pathlib import Path

from fastapi.testclient import TestClient

from server.adapters.llm import RulesAdapter
from server.adapters.tts import ZeroTTSAdapter
from server.app import app
from server.config import Settings
from server.data_store import DataStore
from server.orchestrator import Orchestrator
from server.schemas import ActionProposal, TurnRequest, VehicleState
from server.safety import validate
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


class RulesTests(unittest.IsolatedAsyncioTestCase):
    async def test_cold_increases_temperature(self):
        result = await RulesAdapter().propose("Tôi hơi lạnh", VehicleState(temperature_celsius=23))
        self.assertEqual(result.intent, "climate.set_temperature")
        self.assertEqual(result.arguments["value_celsius"], 25)

    async def test_word_hai_is_not_mistaken_for_lower_command(self):
        result = await RulesAdapter().propose(
            "Tôi hơi lạnh, tăng nhiệt độ thêm hai độ",
            VehicleState(temperature_celsius=23),
        )
        self.assertEqual(result.intent, "climate.set_temperature")
        self.assertEqual(result.arguments["value_celsius"], 25)

    async def test_negation_clarifies(self):
        result = await RulesAdapter().propose("Đừng mở cửa sổ", VehicleState())
        self.assertEqual(result.intent, "conversation.clarify")

    async def test_idempotent_turn(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Settings(data_dir=Path(directory))
            orchestrator = Orchestrator(RulesAdapter(), VehicleSimulator(), DataStore(config))
            request = TurnRequest(transcript="Tăng nhiệt độ 1 độ", session_id="s1", turn_id="t1")
            first = await orchestrator.run(request)
            second = await orchestrator.run(request)
            self.assertEqual(first.vehicle_state.temperature_celsius, 24)
            self.assertEqual(second.vehicle_state.temperature_celsius, 24)


class TTSTests(unittest.TestCase):
    def test_vivi_pack_uses_vivi_id(self):
        pack = Path(__file__).resolve().parent.parent / "voices" / "VIVI.zip"
        with zipfile.ZipFile(pack) as archive:
            meta = json.loads(archive.read("VIVI/meta.json"))
            self.assertIn("VIVI/voice.npz", archive.namelist())
        self.assertEqual(meta["name"], "VIVI")
        self.assertEqual(meta["display_name"], "Mai Chi")

    def test_mai_chi_display_name_resolves_to_voice_key(self):
        self.assertEqual(ZeroTTSAdapter._normalize_voice_name("Mai Chi"), "maichi")

    def test_voice_key_normalization_ignores_common_separators(self):
        self.assertEqual(ZeroTTSAdapter._normalize_voice_name("MAI-CHI"), "maichi")


class APITests(unittest.TestCase):
    def test_health(self):
        response = TestClient(app).get("/api/v1/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "ok")

    def test_rule_turn(self):
        response = TestClient(app).post("/api/v1/turn", json={
            "transcript": "Đặt nhiệt độ 25 độ",
            "session_id": "api-session",
            "turn_id": "api-turn",
        })
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["status"], "verified")
        self.assertEqual(payload["vehicle_state"]["temperature_celsius"], 25)


if __name__ == "__main__":
    unittest.main()
