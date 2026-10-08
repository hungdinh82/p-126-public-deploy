from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from src.vivi.config import Settings
from src.vivi.domain.confirmations import ConfirmationStore
from src.vivi.domain.models import TurnRequest, VehicleState
from src.vivi.orchestration import create_langgraph_orchestrator
from src.vivi.persistence.event_store import EventStore
from src.vivi.vehicle.memory import VehicleSimulator


class ConfirmationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.vehicle = VehicleSimulator()
        config = Settings(
            llm_provider="rules",
            data_dir=root / "data",
            rag_handbook_db=root / "handbook.sqlite3",
            rag_history_db=root / "history.sqlite3",
            store_transcripts=False,
        )
        self.orchestrator = create_langgraph_orchestrator(
            self.vehicle, EventStore(config), config
        )
        self.turn_number = 0

    async def turn(
        self,
        transcript: str,
        confirmation_id: str | None = None,
        decision: str | None = None,
    ):
        self.turn_number += 1
        return await self.orchestrator.run(
            TurnRequest(
                transcript=transcript,
                session_id="s1",
                turn_id=f"t{self.turn_number}",
                confirmation_id=confirmation_id,
                confirmation_decision=decision,
            )
        )

    async def test_approval_is_bound_to_pending_action_and_one_use(self):
        quote = await self.turn("Mở cửa sổ bên tài")
        self.assertEqual(quote.status, "confirmation_required")
        confirmation_id = quote.confirmation.confirmation_id
        self.assertEqual(self.vehicle.state_for("s1").window_driver_percent, 0)
        spoken = await self.turn("Xác nhận")
        self.assertEqual(spoken.status, "verified")
        self.assertEqual(self.vehicle.state_for("s1").window_driver_percent, 100)

        quote = await self.turn("Mở cửa sổ bên phụ")
        confirmation_id = quote.confirmation.confirmation_id
        wrong = await self.turn("Xác nhận", "wrong-id", "approve")
        self.assertEqual(wrong.status, "denied")
        approved = await self.turn("Xác nhận", confirmation_id, "approve")
        self.assertEqual(approved.status, "verified")
        self.assertEqual(approved.vehicle_state.window_positions.front_passenger, 100)
        replay = await self.turn("Xác nhận", confirmation_id, "approve")
        self.assertEqual(replay.status, "denied")

    async def test_denial_and_expiry_never_apply(self):
        quote = await self.turn("Khóa tất cả cửa")
        denied = await self.turn("Hủy", quote.confirmation.confirmation_id, "deny")
        self.assertEqual(denied.status, "denied")
        self.assertFalse(self.vehicle.state_for("s1").door_driver_locked)

        self.orchestrator.gateway.confirmations = ConfirmationStore(ttl_seconds=0)
        expired_quote = await self.turn("Khóa tất cả cửa")
        expired = await self.turn(
            "Xác nhận", expired_quote.confirmation.confirmation_id, "approve"
        )
        self.assertEqual(expired.status, "expired")
        self.assertFalse(self.vehicle.state_for("s1").door_driver_locked)

    async def test_state_change_requires_new_confirmation(self):
        quote = await self.turn("Mở cửa xe bên tài")
        self.vehicle.state_for("s1", VehicleState(state_version=1, driving=True))
        result = await self.turn(
            "Xác nhận", quote.confirmation.confirmation_id, "approve"
        )
        self.assertEqual(result.status, "denied")
        self.assertTrue(self.vehicle.state_for("s1").driving)
        self.assertFalse(self.vehicle.state_for("s1").door_driver_open)

    async def test_vague_door_request_clarifies_location_before_confirmation(self):
        vague = await self.turn("Mở cửa")
        self.assertEqual(vague.status, "clarify")
        self.assertEqual(vague.message, "Bạn muốn mở cửa bên nào?")
        self.assertIsNone(vague.confirmation)

        specified = await self.turn("Bên phụ")
        self.assertEqual(specified.status, "confirmation_required")
        self.assertEqual(specified.confirmation.preview, "Xác nhận mở cửa bên phụ?")
        approved = await self.turn("Xác nhận", specified.confirmation.confirmation_id, "approve")
        self.assertEqual(approved.status, "verified")
        self.assertTrue(approved.vehicle_state.door_states.front_passenger.open)

    async def test_vague_rear_door_request_is_blocked_immediately_while_driving(self):
        self.vehicle.set_driving("s1", True)

        for transcript in ("Mở cửa phía sau", "Mở cửa đằng sau"):
            with self.subTest(transcript=transcript):
                response = await self.turn(transcript)
                self.assertEqual(response.status, "blocked")
                self.assertEqual(response.message, "Không thể mở cửa khi xe đang ở chế độ lái.")
                self.assertIsNone(response.confirmation)

        followup = await self.turn("trái")
        self.assertEqual(followup.status, "clarify")
        self.assertIsNone(followup.confirmation)

    async def test_new_request_cancels_old_confirmation(self):
        quote = await self.turn("Mở cửa xe bên tài")
        replacement = await self.turn("Khóa tất cả cửa")
        self.assertEqual(replacement.status, "confirmation_required")
        stale = await self.turn("Xác nhận", quote.confirmation.confirmation_id, "approve")
        self.assertEqual(stale.status, "denied")
        approved = await self.turn(
            "Đồng ý", replacement.confirmation.confirmation_id, "approve"
        )
        self.assertEqual(approved.status, "verified")
        self.assertTrue(approved.vehicle_state.door_driver_locked)

    async def test_seat_does_not_require_confirmation(self):
        response = await self.turn("Sưởi ghế bên phụ mức 3")
        self.assertEqual(response.status, "verified")
        self.assertEqual(response.vehicle_state.seat_heat_levels.front_passenger, 3)

    async def test_hood_and_trunk_open_after_confirmation_and_block_driving(self):
        quote = await self.turn("Mở cốp sau")
        self.assertEqual(quote.status, "confirmation_required")
        self.assertEqual(quote.confirmation.preview, "Xác nhận mở cốp sau?")
        approved = await self.turn("Xác nhận", quote.confirmation.confirmation_id, "approve")
        self.assertEqual(approved.status, "verified")
        self.assertTrue(approved.vehicle_state.trunk_open)
        with self.assertRaises(ValueError):
            self.vehicle.set_driving("s1", True)

        hood = await self.turn("Mở nắp capo")
        approved = await self.turn("Xác nhận", hood.confirmation.confirmation_id, "approve")
        self.assertTrue(approved.vehicle_state.hood_open)
        status = await self.turn("Capo đang mở không?")
        self.assertIn("Nắp capo đang mở", status.message)

    async def test_hood_cannot_open_while_driving(self):
        self.vehicle.set_driving("s1", True)
        response = await self.turn("Mở capo")
        self.assertEqual(response.status, "blocked")
        self.assertFalse(self.vehicle.state_for("s1").hood_open)

    async def test_open_all_needs_one_confirmation_for_every_panel(self):
        quote = await self.turn("Mở tất cả cửa, capo và cốp")
        self.assertEqual(quote.status, "confirmation_required")
        self.assertEqual(quote.confirmation.preview, "Xác nhận mở tất cả cửa, nắp capo và cốp sau?")
        opened = await self.turn("Xác nhận", quote.confirmation.confirmation_id, "approve")
        self.assertEqual(opened.status, "verified")
        state = opened.vehicle_state
        self.assertTrue(state.hood_open and state.trunk_open)
        self.assertTrue(all(door.open for _, door in state.door_states))

        quote = await self.turn("Đóng tất cả cửa, capo và cốp")
        closed = await self.turn("Xác nhận", quote.confirmation.confirmation_id, "approve")
        self.assertEqual(closed.status, "verified")
        self.assertFalse(closed.vehicle_state.hood_open or closed.vehicle_state.trunk_open)
        self.assertFalse(any(door.open for _, door in closed.vehicle_state.door_states))

    async def test_open_all_is_blocked_by_a_locked_door(self):
        quote = await self.turn("Khóa cửa bên phụ")
        await self.turn("Xác nhận", quote.confirmation.confirmation_id, "approve")
        response = await self.turn("Mở tất cả cửa, capo và cốp")
        self.assertEqual(response.status, "blocked")
        self.assertFalse(self.vehicle.state_for("s1").hood_open)
