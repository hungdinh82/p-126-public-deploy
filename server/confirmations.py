from __future__ import annotations

import hashlib
import json
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from .schemas import ActionProposal, ConfirmationPreview


def action_fingerprint(action: ActionProposal) -> str:
    payload = action.model_dump(mode="json", exclude={"spoken_response", "confidence"})
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


@dataclass
class PendingConfirmation:
    confirmation_id: str
    session_id: str
    action: ActionProposal
    fingerprint: str
    preview: str
    expires_at: datetime
    used: bool = False


@dataclass
class ConfirmationResolution:
    status: str
    message: str
    action: ActionProposal | None = None


class ConfirmationStore:
    def __init__(self, ttl_seconds: int = 30):
        self.ttl_seconds = ttl_seconds
        self._pending: dict[str, PendingConfirmation] = {}
        self._lock = threading.Lock()

    def create(self, session_id: str, action: ActionProposal, preview: str) -> ConfirmationPreview:
        now = datetime.now(timezone.utc)
        item = PendingConfirmation(
            confirmation_id=str(uuid4()),
            session_id=session_id,
            action=action.model_copy(deep=True),
            fingerprint=action_fingerprint(action),
            preview=preview,
            expires_at=now + timedelta(seconds=self.ttl_seconds),
        )
        with self._lock:
            self._pending[item.confirmation_id] = item
        return ConfirmationPreview(
            confirmation_id=item.confirmation_id,
            preview=preview,
            action=item.action,
            expires_at=item.expires_at.isoformat(),
        )

    def resolve(self, confirmation_id: str, session_id: str, decision: str) -> ConfirmationResolution:
        with self._lock:
            item = self._pending.get(confirmation_id)
            if item is None or item.session_id != session_id:
                return ConfirmationResolution("denied", "Xác nhận không hợp lệ hoặc không thuộc phiên này.")
            if item.used:
                return ConfirmationResolution("denied", "Xác nhận này đã được sử dụng và không thể phát lại.")
            item.used = True
            if datetime.now(timezone.utc) >= item.expires_at:
                return ConfirmationResolution("expired", "Xác nhận đã hết hạn. Vui lòng gửi lại yêu cầu.")
            if decision != "approve":
                return ConfirmationResolution("denied", "Đã hủy thao tác theo lựa chọn của bạn.")
            if action_fingerprint(item.action) != item.fingerprint:
                return ConfirmationResolution("denied", "Nội dung thao tác đã thay đổi nên xác nhận bị hủy.")
            return ConfirmationResolution("approved", "", item.action.model_copy(deep=True))
