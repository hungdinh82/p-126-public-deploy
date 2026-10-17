from __future__ import annotations

import struct
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from src.vivi.config import Settings
from src.vivi.domain.models import ActionProposal, TurnRequest, VehicleState
from src.vivi.domain.safety import validate
from src.vivi.orchestration import (
    PROGRESS_MESSAGES,
    LangGraphOrchestrator,
)
from src.vivi.persistence.event_store import EventStore
from src.vivi.speech.stt import DisabledSTTAdapter, PhoWhisperAdapter, create_stt
from src.vivi.speech.tts import DisabledTTSAdapter, ZeroTTSAdapter, create_tts
from src.vivi.vehicle.gateway import VehicleActionGateway
from src.vivi.vehicle.memory import VehicleSimulator


class SafetyTests(unittest.TestCase):
    def test_relative_climate_alias_is_normalized_before_validation(self):
        proposal = ActionProposal.model_validate(
            {
                "intent": "climate.increase_temperature",
                "arguments": {"value_celsius": 25},
            }
        )
        self.assertEqual(proposal.intent, "climate.set_temperature")
        self.assertTrue(validate(proposal, VehicleState()).allowed)

    def test_relative_climate_alias_without_target_clarifies(self):
        proposal = ActionProposal.model_validate(
            {
                "intent": "climate.decrease_temperature",
                "arguments": {},
            }
        )
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


class STTTests(unittest.TestCase):
    def test_stt_can_be_explicitly_disabled_without_affecting_text_flow(self):
        adapter = create_stt(Settings(stt_provider="off"))
        self.assertIsInstance(adapter, DisabledSTTAdapter)
        self.assertEqual(adapter.name, "off")
        self.assertEqual(adapter.availability()[0], False)

    def test_phowhisper_reports_missing_ffmpeg_before_first_request(self):
        adapter = PhoWhisperAdapter(Settings(ffmpeg_binary="missing-test-ffmpeg"))
        with (
            patch("src.vivi.speech.stt.shutil.which", return_value=None),
            patch.dict("sys.modules", {"torch": SimpleNamespace(), "transformers": SimpleNamespace()}),
        ):
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
        model = SimpleNamespace(
            synthesize_stream=lambda text, voice: iter(
                [
                    np.array([[0.0, 0.5]], dtype=np.float32),
                    np.array([[-0.5]], dtype=np.float32),
                ]
            )
        )
        adapter = ZeroTTSAdapter(Settings())
        adapter._model = model
        adapter._voice = voice
        chunks = [chunk async for chunk in adapter.stream("Xin chào")]
        self.assertEqual(len(chunks), 2)
        self.assertEqual(struct.unpack("<3h", b"".join(chunks)), (0, 16383, -16383))


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
                yield (
                    "updates",
                    {
                        "interpret": {
                            "turn": {
                                "effective_provider": "google",
                                "semantic": {"actions": [{"capability": "manual.search"}]},
                            }
                        }
                    },
                )
                yield "values", await self.ainvoke(state)

        with tempfile.TemporaryDirectory() as directory:
            config = Settings(data_dir=Path(directory))
            vehicle = VehicleSimulator()
            store = EventStore(config)
            pipeline = LangGraphOrchestrator(FakeGraph(), VehicleActionGateway(vehicle=vehicle), store)
            request = TurnRequest(transcript="Sạc AC thế nào?", session_id="integrated", turn_id="t1")
            events = [event async for event in pipeline.run_stream(request, provider="google")]

        self.assertEqual([event["type"] for event in events], ["provider", "progress", "speech", "final"])
        self.assertIn(next(event["text"] for event in events if event["type"] == "progress"), PROGRESS_MESSAGES)
        response = events[-1]["response"]
        self.assertEqual(response["route"], "handbook")
        self.assertEqual(response["status"], "verified")
        self.assertEqual(response["grounding_status"], "supported")
        self.assertEqual(response["evidence"][0]["source_id"], "manual-1")
        self.assertEqual(response["provider"], "typed_event")


if __name__ == "__main__":
    unittest.main()
