from __future__ import annotations

import unittest
from unittest.mock import patch

from server.adapters.llm import RulesAdapter
from server.config import Settings
from server.data_store import DataStore
from server.orchestrator import Orchestrator
from server.schemas import TurnRequest, VehicleState
from server.vehicle import VehicleSimulator


class ConfirmationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.vehicle = VehicleSimulator()
        self.orchestrator = Orchestrator(
            RulesAdapter(), self.vehicle, DataStore(Settings(store_transcripts=False))
        )
        self.turn_number = 0

    async def turn(self, transcript: str, confirmation_id: str | None = None):
        self.turn_number += 1
        return await self.orchestrator.run(TurnRequest(
            transcript=transcript, session_id="s1", turn_id=f"t{self.turn_number}",
            confirmation_id=confirmation_id,
        ))

    async def test_approval_is_bound_to_pending_action_and_one_use(self):
        quote = await self.turn("Mở cửa sổ bên tài")
        self.assertEqual(quote.status, "confirm")
        self.assertEqual(self.vehicle.state_for("s1").window_driver_percent, 0)
        missing = await self.turn("Xác nhận")
        self.assertEqual(missing.status, "blocked")
        wrong = await self.turn("Xác nhận", "wrong-id")
        self.assertEqual(wrong.status, "blocked")
        self.assertEqual(self.vehicle.state_for("s1").window_driver_percent, 0)
        approved = await self.turn("Xác nhận", quote.confirmation_id)
        self.assertEqual(approved.status, "verified")
        self.assertEqual(approved.vehicle_state.window_driver_percent, 100)
        replay = await self.turn("Xác nhận", quote.confirmation_id)
        self.assertEqual(replay.status, "blocked")

    async def test_denial_and_expiry_never_apply(self):
        quote = await self.turn("Khóa cửa xe")
        denied = await self.turn("Hủy", quote.confirmation_id)
        self.assertEqual(denied.status, "blocked")
        self.assertFalse(self.vehicle.state_for("s1").door_driver_locked)
        with patch("server.orchestrator.CONFIRMATION_TTL_SECONDS", 0):
            expired_quote = await self.turn("Khóa cửa xe")
        expired = await self.turn("Xác nhận", expired_quote.confirmation_id)
        self.assertEqual(expired.status, "blocked")
        self.assertFalse(self.vehicle.state_for("s1").door_driver_locked)

    async def test_state_change_requires_new_confirmation(self):
        quote = await self.turn("Mở cửa xe bên tài")
        self.vehicle.state_for("s1", VehicleState(state_version=1, driving=True))
        result = await self.orchestrator.run(TurnRequest(
            transcript="Xác nhận", session_id="s1", turn_id="approve-stale",
            confirmation_id=quote.confirmation_id,
            vehicle_state=VehicleState(state_version=0, driving=False),
        ))
        self.assertEqual(result.status, "blocked")
        self.assertTrue(self.vehicle.state_for("s1").driving)
        self.assertFalse(self.vehicle.state_for("s1").door_driver_open)

    async def test_new_request_cancels_old_confirmation(self):
        quote = await self.turn("Mở cửa xe bên tài")
        replacement = await self.turn("Khóa cửa xe")
        self.assertEqual(replacement.status, "confirm")
        stale = await self.turn("Xác nhận", quote.confirmation_id)
        self.assertEqual(stale.status, "blocked")
        self.assertFalse(self.vehicle.state_for("s1").door_driver_open)
        approved = await self.turn("Đồng ý", replacement.confirmation_id)
        self.assertEqual(approved.status, "verified")
        self.assertTrue(approved.vehicle_state.door_driver_locked)

    async def test_seat_does_not_require_confirmation(self):
        response = await self.turn("Sưởi ghế mức 3")
        self.assertEqual(response.status, "verified")
        self.assertEqual(response.vehicle_state.seat_driver_heat_level, 3)
