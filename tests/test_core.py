from __future__ import annotations

import tempfile
import unittest
import json
import struct
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from server.adapters.llm import RulesAdapter
from server.adapters.tts import ZeroTTSAdapter
from server.app import app
from server.config import Settings
from server.data_store import DataStore
from server.langgraph_orchestrator import LangGraphOrchestrator
from server.orchestrator import Orchestrator
from server.safety import validate
from server.schemas import ActionProposal, TurnRequest, VehicleState
from server.vehicle import VehicleSimulator
from src.actions.gateway import VehicleActionGateway


class SafetyTests(unittest.TestCase):
    def test_relative_climate_alias_is_normalized_before_validation(self):
        proposal = ActionProposal.model_validate({
            "intent": "climate.increase_temperature",
            "arguments": {"value_celsius": 25},
        })
        self.assertEqual(proposal.intent, "climate.set_temperature")
        self.assertTrue(validate(proposal, VehicleState()).allowed)

    def test_relative_climate_alias_without_target_clarifies(self):
        proposal = ActionProposal.model_validate({
            "intent": "climate.decrease_temperature",
            "arguments": {},
        })
        result = validate(proposal, VehicleState())
        self.assertEqual(proposal.intent, "climate.set_temperature")
        self.assertFalse(result.allowed)
        self.assertEqual(result.status, "clarify")

    def test_conversation_does_not_change_vehicle(self):
        proposal = ActionProposal(intent="conversation.respond", spoken_response="Mình là ViVi.")
        self.assertTrue(validate(proposal, VehicleState(driving=True)).allowed)

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
    async def test_open_ended_reply_bypasses_vehicle_simulator(self):
        class ChatAdapter(RulesAdapter):
            async def propose(self, transcript, vehicle):
                return ActionProposal(intent="conversation.respond", spoken_response="Mình là ViVi, trợ lý AI trên ô tô.")

        with tempfile.TemporaryDirectory() as directory:
            config = Settings(data_dir=Path(directory))
            vehicle = VehicleSimulator()
            orchestrator = Orchestrator(ChatAdapter(), vehicle, DataStore(config))
            response = await orchestrator.run(TurnRequest(transcript="Bạn là ai", session_id="chat", turn_id="chat-1"))
            self.assertEqual(response.message, "Mình là ViVi, trợ lý AI trên ô tô.")
            self.assertEqual(response.action.intent, "conversation.respond")
            self.assertEqual(response.status, "verified")
            self.assertEqual(vehicle._executed, {})

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


class TTSTests(unittest.TestCase):
    def test_tts_preload_failure_stops_app_startup(self):
        with patch("server.app.tts.preload", new_callable=AsyncMock, side_effect=RuntimeError("voice pack invalid")), \
             patch("server.app.stt.preload", new_callable=AsyncMock):
            with self.assertRaisesRegex(RuntimeError, "voice pack invalid"):
                with TestClient(app):
                    pass

    def test_preload_runs_at_app_startup(self):
        with patch("server.app.tts.preload", new_callable=AsyncMock) as preload, \
             patch("server.app.stt.preload", new_callable=AsyncMock) as stt_preload:
            with TestClient(app) as client:
                response = client.get("/api/v1/health")
        preload.assert_awaited_once()
        stt_preload.assert_awaited_once()
        self.assertEqual(response.status_code, 200)

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

    def test_cuda_provider_is_preferred_with_cpu_fallback(self):
        providers = ZeroTTSAdapter._select_execution_providers(
            "cuda", ["CPUExecutionProvider", "CUDAExecutionProvider"]
        )
        self.assertEqual(providers, ["CUDAExecutionProvider", "CPUExecutionProvider"])

    def test_explicit_cuda_fails_when_provider_is_unavailable(self):
        with self.assertRaisesRegex(RuntimeError, "CUDAExecutionProvider"):
            ZeroTTSAdapter._select_execution_providers("cuda", ["CPUExecutionProvider"])

    def test_auto_falls_back_to_cpu(self):
        providers = ZeroTTSAdapter._select_execution_providers(
            "auto", ["CPUExecutionProvider"]
        )
        self.assertEqual(providers, ["CPUExecutionProvider"])

    def test_stream_route_returns_pcm_chunks_and_format(self):
        class FakeTTS:
            _model = SimpleNamespace(sample_rate=48000)

            def availability(self):
                return True, "loaded"

            async def stream(self, text):
                yield b"\x00\x00"
                yield b"\xff\x7f"

        with patch("server.app.tts", FakeTTS()):
            response = TestClient(app).post("/api/v1/tts/stream", json={
                "text": "Xin chào", "session_id": "stream-test", "turn_id": "stream-1",
            })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["X-ViVi-Audio-Format"], "pcm_s16le")
        self.assertEqual(response.headers["X-ViVi-Sample-Rate"], "48000")
        self.assertEqual(response.content, b"\x00\x00\xff\x7f")


class TTSStreamTests(unittest.IsolatedAsyncioTestCase):
    async def test_adapter_streams_model_chunks_as_pcm(self):
        import numpy as np

        voice = object()
        model = SimpleNamespace(synthesize_stream=lambda text, voice: iter([
            np.array([[0.0, 0.5]], dtype=np.float32),
            np.array([[-0.5]], dtype=np.float32),
        ]))
        adapter = ZeroTTSAdapter(Settings())
        adapter._model = model
        adapter._voice = voice
        chunks = [chunk async for chunk in adapter.stream("Xin chào")]
        self.assertEqual(len(chunks), 2)
        self.assertEqual(struct.unpack("<3h", b"".join(chunks)), (0, 16383, -16383))


class APITests(unittest.TestCase):
    def test_health(self):
        response = TestClient(app).get("/api/v1/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "ok")
        options = response.json()["llm"]["options"]
        self.assertEqual({item["provider"] for item in options}, {"rules", "openai", "google", "local"})
        self.assertTrue(next(item for item in options if item["provider"] == "rules")["available"])

    def test_turn_uses_selected_provider_without_changing_default(self):
        class SelectedAdapter(RulesAdapter):
            name = "local"

            async def propose(self, transcript, vehicle):
                return ActionProposal(intent="conversation.respond", spoken_response="Mình là ViVi local.")

        with tempfile.TemporaryDirectory() as directory:
            config = Settings(data_dir=Path(directory))
            test_orchestrator = Orchestrator(RulesAdapter(), VehicleSimulator(), DataStore(config))
            with patch("server.app.orchestrator", test_orchestrator), patch.dict("server.app.llm_adapters", {"local": SelectedAdapter()}):
                response = TestClient(app).post("/api/v1/turn", json={
                    "transcript": "Bạn là ai", "session_id": "model-session", "turn_id": "model-1", "llm_provider": "local",
                })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["provider"], "local")
        self.assertEqual(response.json()["message"], "Mình là ViVi local.")
        self.assertEqual(test_orchestrator.llm.name, "rules")

    def test_unavailable_provider_is_rejected(self):
        with patch.dict("server.app.llm_adapters", clear=True):
            response = TestClient(app).post("/api/v1/turn", json={
                "transcript": "Bạn là ai", "session_id": "model-session", "turn_id": "model-2", "llm_provider": "local",
            })
        self.assertEqual(response.status_code, 400)

    def test_rule_turn(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Settings(data_dir=Path(directory))
            test_orchestrator = Orchestrator(RulesAdapter(), VehicleSimulator(), DataStore(config))
            with patch("server.app.orchestrator", test_orchestrator):
                response = TestClient(app).post("/api/v1/turn", json={
                    "transcript": "Đặt nhiệt độ 25 độ",
                    "session_id": "api-session",
                    "turn_id": "api-turn",
                    "llm_provider": "rules",
                })
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["status"], "verified")
        self.assertEqual(payload["vehicle_state"]["temperature_celsius"], 25)

    def test_stream_route_returns_speech_then_final_ndjson(self):
        class StreamingAdapter(RulesAdapter):
            name = "local"

            async def stream_json(self, transcript, vehicle):
                yield '{"intent":"conversation.respond","spoken_response":"Xin chào. Mình là ViVi.",'
                yield '"arguments":{},"needs_clarification":false,"clarification_question":null}'

        with tempfile.TemporaryDirectory() as directory:
            config = Settings(data_dir=Path(directory))
            test_orchestrator = Orchestrator(RulesAdapter(), VehicleSimulator(), DataStore(config))
            with patch("server.app.orchestrator", test_orchestrator), patch.dict("server.app.llm_adapters", {"local": StreamingAdapter()}):
                response = TestClient(app).post("/api/v1/turn/stream", json={
                    "transcript": "Bạn là ai", "session_id": "stream-api", "turn_id": "stream-api-1", "llm_provider": "local",
                })

        events = [json.loads(line) for line in response.text.splitlines()]
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["content-type"], "application/x-ndjson")
        self.assertEqual([event["type"] for event in events], ["speech", "speech", "final"])
        self.assertEqual(events[-1]["response"]["status"], "verified")


class TurnStreamingTests(unittest.IsolatedAsyncioTestCase):
    async def test_conversation_speech_arrives_before_final_response(self):
        class StreamingChat(RulesAdapter):
            name = "stream-test"

            async def stream_json(self, transcript, vehicle):
                chunks = [
                    '{"intent":"conversation.respond","spoken_response":"Xin chào bạn. ',
                    'Mình là ViVi.","arguments":{},"needs_clarification":false,',
                    '"clarification_question":null}',
                ]
                for chunk in chunks:
                    yield chunk

        with tempfile.TemporaryDirectory() as directory:
            config = Settings(data_dir=Path(directory))
            orchestrator = Orchestrator(StreamingChat(), VehicleSimulator(), DataStore(config))
            request = TurnRequest(transcript="Bạn là ai", session_id="stream", turn_id="stream-chat")
            events = [event async for event in orchestrator.run_stream(request)]

        self.assertEqual(events[0], {"type": "speech", "text": "Xin chào bạn."})
        self.assertEqual(events[1], {"type": "speech", "text": "Mình là ViVi."})
        self.assertEqual(events[-1]["type"], "final")
        self.assertTrue(events[-1]["streamed_speech"])

    async def test_vehicle_action_is_not_spoken_before_verification(self):
        class StreamingAction(RulesAdapter):
            async def stream_json(self, transcript, vehicle):
                yield '{"intent":"window.set_position","spoken_response":"Đang mở cửa sổ.",'
                yield '"arguments":{"position_percent":100},"needs_clarification":false,"clarification_question":null}'

        with tempfile.TemporaryDirectory() as directory:
            config = Settings(data_dir=Path(directory))
            orchestrator = Orchestrator(StreamingAction(), VehicleSimulator(), DataStore(config))
            request = TurnRequest(transcript="Mở cửa sổ", session_id="stream", turn_id="stream-action")
            events = [event async for event in orchestrator.run_stream(request)]

        self.assertEqual([event["type"] for event in events], ["final"])
        self.assertFalse(events[0]["streamed_speech"])
        self.assertEqual(events[0]["response"]["status"], "confirmation_required")
        self.assertEqual(events[0]["response"]["vehicle_state"]["window_driver_percent"], 0)


class IntegratedLangGraphVoiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_grounded_graph_response_maps_to_voice_contract(self):
        class FakeGraph:
            async def ainvoke(self, state):
                return {
                    "output": {
                        "session_id": state["session_id"],
                        "turn_id": state["turn_id"],
                        "route": "handbook",
                        "intent": "manual.search",
                        "status": "answered",
                        "response_text": "VF8 hỗ trợ sạc AC theo hướng dẫn.",
                        "tts_text": "VF8 hỗ trợ sạc AC theo hướng dẫn.",
                        "citations": [
                            {
                                "source_id": "manual-1",
                                "section_path": ["Pin và Sạc", "Sạc AC"],
                                "source_url": "https://example.test/manual",
                            }
                        ],
                        "grounding_status": "supported",
                    }
                }

        with tempfile.TemporaryDirectory() as directory:
            config = Settings(data_dir=Path(directory))
            vehicle = VehicleSimulator()
            store = DataStore(config)
            fallback = Orchestrator(RulesAdapter(), vehicle, store)
            pipeline = LangGraphOrchestrator(
                {"rules": FakeGraph()}, VehicleActionGateway(vehicle=vehicle), store, fallback
            )
            request = TurnRequest(
                transcript="Sạc AC thế nào?", session_id="integrated", turn_id="t1"
            )
            events = [event async for event in pipeline.run_stream(request, llm=RulesAdapter())]

        self.assertEqual([event["type"] for event in events], ["speech", "final"])
        response = events[-1]["response"]
        self.assertEqual(response["route"], "handbook")
        self.assertEqual(response["status"], "verified")
        self.assertEqual(response["grounding_status"], "supported")
        self.assertEqual(response["evidence"][0]["source_id"], "manual-1")
        self.assertEqual(response["provider"], "langgraph/rules")


if __name__ == "__main__":
    unittest.main()
