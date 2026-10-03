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
        missing = await self.turn("Xác nhận")
        self.assertEqual(missing.status, "clarify")
        wrong = await self.turn("Xác nhận", "wrong-id", "approve")
        self.assertEqual(wrong.status, "denied")
        approved = await self.turn("Xác nhận", confirmation_id, "approve")
        self.assertEqual(approved.status, "verified")
        self.assertEqual(approved.vehicle_state.window_driver_percent, 100)
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
