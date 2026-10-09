"""Confirmation grants remain bound and single use across backend restarts."""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from src.vivi.domain.confirmations import ConfirmationResolution, action_fingerprint
from src.vivi.domain.models import ActionProposal, ConfirmationPreview


class DurableConfirmations:
    def __init__(self, store, ttl_seconds=30):
        self.store = store
        self.ttl_seconds = ttl_seconds
        with store.connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS confirmation_grants (
                id TEXT PRIMARY KEY, session TEXT, payload TEXT, used INTEGER DEFAULT 0)""")

    def create(self, session_id, action, preview, vehicle_state):
        identifier = str(uuid4())
        expires = datetime.now(UTC) + timedelta(seconds=self.ttl_seconds)
        payload = {"action": action.model_dump(mode="json"), "fingerprint": action_fingerprint(action),
                   "vehicle_id": vehicle_state.vehicle_id, "state_version": vehicle_state.state_version,
                   "expires_at": expires.isoformat()}
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("UPDATE confirmation_grants SET used=1 WHERE session=?", (session_id,))
            db.execute("INSERT INTO confirmation_grants VALUES (?,?,?,0)", (identifier, session_id, json.dumps(payload)))
        return ConfirmationPreview(confirmation_id=identifier, preview=preview, action=action, expires_at=expires.isoformat())

    def cancel(self, session_id):
        with self.store.connect() as db:
            db.execute("UPDATE confirmation_grants SET used=1 WHERE session=?", (session_id,))

    def resolve(self, confirmation_id, session_id, decision, vehicle_state):
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM confirmation_grants WHERE id=? AND session=?", (confirmation_id, session_id)).fetchone()
            if row is None or row["used"]:
                return ConfirmationResolution("denied", "Xác nhận không hợp lệ hoặc đã được sử dụng.")
            db.execute("UPDATE confirmation_grants SET used=1 WHERE id=?", (confirmation_id,))
            payload = json.loads(row["payload"])
        if datetime.now(UTC) >= datetime.fromisoformat(payload["expires_at"]):
            return ConfirmationResolution("expired", "Xác nhận đã hết hạn. Bạn gửi lại yêu cầu nhé.")
        if decision != "approve":
            return ConfirmationResolution("denied", "Đã hủy thao tác.")
        if vehicle_state.vehicle_id != payload["vehicle_id"] or vehicle_state.state_version != payload["state_version"]:
            return ConfirmationResolution("denied", "Trạng thái xe đã thay đổi. Bạn gửi lại yêu cầu nhé.")
        action = ActionProposal.model_validate(payload["action"])
        if action_fingerprint(action) != payload["fingerprint"]:
            return ConfirmationResolution("denied", "Nội dung thao tác đã thay đổi.")
        return ConfirmationResolution("approved", "", action)

    def create_generic(self, session_id, binding, preview):
        identifier = str(uuid4())
        expires = datetime.now(UTC) + timedelta(seconds=self.ttl_seconds)
        payload = {"binding": binding, "expires_at": expires.isoformat()}
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("UPDATE confirmation_grants SET used=1 WHERE session=?", (session_id,))
            db.execute("INSERT INTO confirmation_grants VALUES (?,?,?,0)", (identifier, session_id, json.dumps(payload)))
        return {"confirmation_id": identifier, "preview": preview, "expires_at": expires.isoformat()}

    def consume_generic(self, session_id, identifier, binding):
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM confirmation_grants WHERE id=? AND session=?", (identifier, session_id)).fetchone()
            if row is None or row["used"]:
                return False
            db.execute("UPDATE confirmation_grants SET used=1 WHERE id=?", (identifier,))
            payload = json.loads(row["payload"])
        return payload.get("binding") == binding and datetime.now(UTC) < datetime.fromisoformat(payload["expires_at"])
