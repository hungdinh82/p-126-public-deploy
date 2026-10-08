from __future__ import annotations

import json
import struct
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from src.vivi.agents.classifier import RulesIntentClassifier
from src.vivi.api.app import app
from src.vivi.api.runtime import runtime
from src.vivi.config import Settings, settings
from src.vivi.domain.models import ActionProposal, TurnRequest, VehicleState
from src.vivi.domain.safety import validate
from src.vivi.orchestration import (
    PROGRESS_MESSAGES,
    LangGraphOrchestrator,
    create_langgraph_orchestrator,
)
from src.vivi.persistence.event_store import EventStore
from src.vivi.speech.stt import DisabledSTTAdapter, PhoWhisperAdapter, create_stt
from src.vivi.speech.tts import DisabledTTSAdapter, ZeroTTSAdapter, create_tts
from src.vivi.vehicle.gateway import VehicleActionGateway
from src.vivi.vehicle.memory import VehicleSimulator


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
    def setUp(self):
        self.rules = RulesIntentClassifier()

    def classify(self, text: str, state: VehicleState | None = None):
        return self.rules.classify_with_context(
            text,
            [],
            (state or VehicleState()).model_dump(mode="json"),
        )

    async def test_open_ended_reply_is_conversation(self):
        result = self.classify("Bạn là ai")
        self.assertEqual(result.intent, "conversation.respond")
        self.assertIn("ViVi", result.response_text)

    async def test_cold_increases_temperature(self):
        result = self.classify("Tôi hơi lạnh", VehicleState(temperature_celsius=23))
        self.assertEqual(result.intent, "climate.set_temperature")
        self.assertEqual(result.arguments.value_celsius, 25)

    async def test_word_hai_is_not_mistaken_for_lower_command(self):
        result = self.classify(
            "Tôi hơi lạnh, tăng nhiệt độ thêm hai độ",
            VehicleState(temperature_celsius=23),
        )
        self.assertEqual(result.intent, "climate.set_temperature")
        self.assertEqual(result.arguments.value_celsius, 25)

    async def test_negation_acknowledges_without_execution(self):
        result = self.classify("Đừng mở cửa sổ", VehicleState())
        self.assertEqual(result.intent, "conversation.respond")

    async def test_door_and_seat_intents(self):
        state = VehicleState()
        cases = (
            ("Mở cửa xe bên tài", "door.set_open", {"open": True, "zone": "driver"}),
            ("Đóng cửa xe bên tài", "door.set_open", {"open": False, "zone": "driver"}),
            ("Khóa tất cả cửa", "door.set_lock", {"locked": True, "zone": "all"}),
            ("Mở khóa cửa bên phụ", "door.set_lock", {"locked": False, "zone": "front_passenger"}),
            ("Sưởi ghế sau trái mức 2", "seat.set_heat_level", {"level": 2, "zone": "rear_left"}),
            ("Tắt sưởi ghế bên tài", "seat.set_heat_level", {"level": 0, "zone": "driver"}),
        )
        for text, intent, arguments in cases:
            with self.subTest(text=text):
                proposal = self.classify(text, state)
                self.assertEqual(
                    (proposal.intent, proposal.arguments.model_dump(exclude_none=True)),
                    (intent, arguments),
                )
        unrelated = self.classify("Nhiệt độ của xe 25 độ", state)
        self.assertEqual(unrelated.intent, "climate.set_temperature")

    async def test_door_action_requires_location_and_accepts_driver_side_followup(self):
        vague = self.classify("Mở cửa", VehicleState())
        self.assertEqual(vague.intent, "conversation.clarify")
        self.assertEqual(vague.clarification_question, "Bạn muốn mở cửa bên nào?")

        followup = self.rules.classify_with_context(
            "Bên tài",
            [
                {
                    "query": "Mở cửa",
                    "answer": "Bạn muốn mở cửa bên nào?",
                    "route": "clarify",
                    "intent": "conversation.clarify",
                }
            ],
            VehicleState().model_dump(mode="json"),
        )
        self.assertEqual(followup.intent, "door.set_open")
        self.assertEqual(followup.arguments.model_dump(exclude_none=True), {"open": True, "zone": "driver"})

        changed_topic = self.rules.classify_with_context(
            "Bên tài đang bị bẩn",
            [
                {
                    "query": "Mở cửa",
                    "answer": "Bạn muốn mở cửa bên nào?",
                    "route": "clarify",
                    "intent": "conversation.clarify",
                }
            ],
            VehicleState().model_dump(mode="json"),
        )
        self.assertEqual(changed_topic.intent, "conversation.clarify")

        passenger = self.classify("Mở cửa bên phụ", VehicleState())
        self.assertEqual(passenger.intent, "door.set_open")
        self.assertEqual(passenger.arguments.zone, "front_passenger")

        all_windows = self.classify("Mở tất cả cửa sổ", VehicleState())
        self.assertEqual(all_windows.intent, "window.set_position")
        self.assertEqual(all_windows.arguments.zone, "all")

    async def test_idempotent_turn(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Settings(data_dir=Path(directory))
            orchestrator = create_langgraph_orchestrator(
                VehicleSimulator(), EventStore(config), config
            )
            request = TurnRequest(transcript="Tăng nhiệt độ 1 độ", session_id="s1", turn_id="t1")
            first = await orchestrator.run(request)
            second = await orchestrator.run(request)
            self.assertEqual(first.vehicle_state.temperature_celsius, 24)
            self.assertEqual(second.vehicle_state.temperature_celsius, 24)


class TTSTests(unittest.TestCase):
    def test_tts_preload_failure_stops_app_startup(self):
        with patch.object(settings, "tts_provider", "zerotts"), \
             patch.object(settings, "zerotts_preload", True), \
             patch.object(runtime.tts, "preload", new_callable=AsyncMock, side_effect=RuntimeError("voice pack invalid")), \
             patch.object(runtime.stt, "preload", new_callable=AsyncMock):
            with self.assertRaisesRegex(RuntimeError, "voice pack invalid"):
                with TestClient(app):
                    pass

    def test_preload_runs_at_app_startup(self):
        with patch.object(settings, "tts_provider", "zerotts"), \
             patch.object(settings, "stt_provider", "phowhisper"), \
             patch.object(settings, "zerotts_preload", True), \
             patch.object(settings, "phowhisper_preload", True), \
             patch.object(runtime.tts, "preload", new_callable=AsyncMock) as preload, \
             patch.object(runtime.stt, "preload", new_callable=AsyncMock) as stt_preload:
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

        with patch.object(runtime, "tts", FakeTTS()):
            response = TestClient(app).post("/api/v1/tts/stream", json={
                "text": "Xin chào", "session_id": "stream-test", "turn_id": "stream-1",
            })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["X-ViVi-Audio-Format"], "pcm_s16le")
        self.assertEqual(response.headers["X-ViVi-Sample-Rate"], "48000")
        self.assertEqual(response.content, b"\x00\x00\xff\x7f")


class STTTests(unittest.TestCase):
    def test_stt_can_be_explicitly_disabled_without_affecting_text_flow(self):
        adapter = create_stt(Settings(stt_provider="off"))
        self.assertIsInstance(adapter, DisabledSTTAdapter)
        self.assertEqual(adapter.name, "off")
        self.assertEqual(adapter.availability()[0], False)

    def test_phowhisper_reports_missing_ffmpeg_before_first_request(self):
        adapter = PhoWhisperAdapter(Settings(ffmpeg_binary="missing-test-ffmpeg"))
        with patch("src.vivi.speech.stt.shutil.which", return_value=None), \
             patch.dict("sys.modules", {"torch": SimpleNamespace(), "transformers": SimpleNamespace()}):
            available, detail = adapter.availability()
        self.assertFalse(available)
        self.assertIn("sudo apt install ffmpeg", detail)


class TTSStreamTests(unittest.IsolatedAsyncioTestCase):
    async def test_tts_can_be_explicitly_disabled(self):
        adapter = create_tts(Settings(tts_provider="off"))
        self.assertIsInstance(adapter, DisabledTTSAdapter)
        self.assertEqual(adapter.name, "off")
        self.assertEqual(adapter.availability()[0], False)
        with self.assertRaisesRegex(RuntimeError, "TTS đang tắt"):
            await adapter.synthesize("Xin chào")

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
        # The catalog follows config (cloud entries appear when rag_local_only is off);
        # without credentials only the scripted provider can run.
        self.assertEqual({item["provider"] for item in options}, set(runtime.llm_models))
        self.assertEqual({item["provider"] for item in options if item["available"]}, {"rules"})

    def test_turn_uses_selected_provider_without_changing_default(self):
        class LocalGraph:
            async def ainvoke(self, state):
                return {"output": {
                    "session_id": state["session_id"],
                    "turn_id": state["turn_id"],
                    "route": "conversation",
                    "intent": "conversation.respond",
                    "status": "answered",
                    "response_text": "Mình là ViVi local.",
                    "tts_text": "Mình là ViVi local.",
                }}

        with tempfile.TemporaryDirectory() as directory:
            config = Settings(data_dir=Path(directory))
            vehicle = VehicleSimulator()
            test_orchestrator = LangGraphOrchestrator(
                {"local": LocalGraph()},
                VehicleActionGateway(vehicle=vehicle),
                EventStore(config),
                default_provider="rules",
            )
            with patch.object(runtime, "orchestrator", test_orchestrator):
                response = TestClient(app).post("/api/v1/turn", json={
                    "transcript": "Bạn là ai", "session_id": "model-session", "turn_id": "model-1", "llm_provider": "local",
                })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["provider"], "langgraph/local")
        self.assertEqual(response.json()["message"], "Mình là ViVi local.")
        self.assertEqual(test_orchestrator.default_provider, "rules")

    def test_unavailable_provider_is_rejected(self):
        with patch.dict(runtime.orchestrator.graphs, {}, clear=True):
            response = TestClient(app).post("/api/v1/turn", json={
                "transcript": "Bạn là ai", "session_id": "model-session", "turn_id": "model-2", "llm_provider": "local",
            })
        self.assertEqual(response.status_code, 400)

    def test_rule_turn(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Settings(data_dir=Path(directory))
            test_orchestrator = create_langgraph_orchestrator(
                VehicleSimulator(), EventStore(config), config
            )
            with patch.object(runtime, "orchestrator", test_orchestrator):
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
        class LocalGraph:
            async def ainvoke(self, state):
                return {"output": {
                    "session_id": state["session_id"],
                    "turn_id": state["turn_id"],
                    "route": "conversation",
                    "intent": "conversation.respond",
                    "status": "answered",
                    "response_text": "Xin chào. Mình là ViVi.",
                    "tts_text": "Xin chào. Mình là ViVi.",
                }}

            async def astream(self, state, stream_mode):
                yield "values", await self.ainvoke(state)

        with tempfile.TemporaryDirectory() as directory:
            config = Settings(data_dir=Path(directory))
            vehicle = VehicleSimulator()
            test_orchestrator = LangGraphOrchestrator(
                {"local": LocalGraph()},
                VehicleActionGateway(vehicle=vehicle),
                EventStore(config),
            )
            with patch.object(runtime, "orchestrator", test_orchestrator):
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
        class ChatGraph:
            async def ainvoke(self, state):
                return {"output": {
                    "session_id": state["session_id"],
                    "turn_id": state["turn_id"],
                    "route": "conversation",
                    "intent": "conversation.respond",
                    "status": "answered",
                    "response_text": "Xin chào bạn. Mình là ViVi.",
                    "tts_text": "Xin chào bạn. Mình là ViVi.",
                }}

            async def astream(self, state, stream_mode):
                yield "updates", {"classify_intent": {"route": "conversation"}}
                yield "values", await self.ainvoke(state)

        with tempfile.TemporaryDirectory() as directory:
            config = Settings(data_dir=Path(directory))
            vehicle = VehicleSimulator()
            orchestrator = LangGraphOrchestrator(
                {"rules": ChatGraph()},
                VehicleActionGateway(vehicle=vehicle),
                EventStore(config),
            )
            request = TurnRequest(transcript="Bạn là ai", session_id="stream", turn_id="stream-chat")
            events = [event async for event in orchestrator.run_stream(request)]

        self.assertEqual(events[0], {"type": "speech", "text": "Xin chào bạn."})
        self.assertEqual(events[1], {"type": "speech", "text": "Mình là ViVi."})
        self.assertEqual(events[-1]["type"], "final")
        self.assertTrue(events[-1]["streamed_speech"])

    async def test_vehicle_action_stream_uses_safety_result(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Settings(data_dir=Path(directory))
            orchestrator = create_langgraph_orchestrator(
                VehicleSimulator(), EventStore(config), config
            )
            request = TurnRequest(transcript="Mở cửa sổ bên tài", session_id="stream", turn_id="stream-action")
            events = [event async for event in orchestrator.run_stream(request)]

        self.assertEqual(events[-1]["type"], "final")
        self.assertTrue(events[-1]["streamed_speech"])
        self.assertEqual(events[-1]["response"]["status"], "confirmation_required")
        self.assertEqual(events[-1]["response"]["vehicle_state"]["window_driver_percent"], 0)
        self.assertNotIn("progress", [event["type"] for event in events])

    async def test_handbook_stream_announces_wait_first(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Settings(data_dir=Path(directory))
            orchestrator = create_langgraph_orchestrator(
                VehicleSimulator(), EventStore(config), config
            )
            request = TurnRequest(transcript="Cách sạc AC cho VF8 thế nào?", session_id="stream", turn_id="stream-manual")
            events = [event async for event in orchestrator.run_stream(request, provider="rules")]

        self.assertEqual(events[0]["type"], "progress")
        self.assertIn(events[0]["text"], PROGRESS_MESSAGES)
        self.assertEqual(events[-1]["response"]["route"], "handbook")


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

            async def astream(self, state, stream_mode):
                yield "updates", {"scope_guard": {"grounding_status": "pending"}}
                yield "values", await self.ainvoke(state)

        with tempfile.TemporaryDirectory() as directory:
            config = Settings(data_dir=Path(directory))
            vehicle = VehicleSimulator()
            store = EventStore(config)
            pipeline = LangGraphOrchestrator(
                {"rules": FakeGraph()}, VehicleActionGateway(vehicle=vehicle), store
            )
            request = TurnRequest(
                transcript="Sạc AC thế nào?", session_id="integrated", turn_id="t1"
            )
            events = [event async for event in pipeline.run_stream(request, provider="rules")]

        self.assertEqual([event["type"] for event in events], ["progress", "speech", "final"])
        self.assertIn(events[0]["text"], PROGRESS_MESSAGES)
        response = events[-1]["response"]
        self.assertEqual(response["route"], "handbook")
        self.assertEqual(response["status"], "verified")
        self.assertEqual(response["grounding_status"], "supported")
        self.assertEqual(response["evidence"][0]["source_id"], "manual-1")
        self.assertEqual(response["provider"], "langgraph/rules")


if __name__ == "__main__":
    unittest.main()
