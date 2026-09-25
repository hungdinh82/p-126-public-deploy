from __future__ import annotations

import re
import threading
import time
import unicodedata
from dataclasses import dataclass
from uuid import uuid4

from .adapters.llm import LLMAdapter
from .data_store import DataStore
from .safety import validate
from .schemas import ActionProposal, TurnRequest, TurnResponse, VehicleState
from .vehicle import VehicleSimulator
from .vehicle_mqtt import MqttVehicleAdapter

CONFIRMATION_TTL_SECONDS = 30
SENSITIVE_INTENTS = {"window.set_position", "door.set_lock", "door.set_open"}


@dataclass(frozen=True)
class PendingConfirmation:
    confirmation_id: str
    action: ActionProposal
    source_turn_id: str
    vehicle_id: str
    state_version: int
    expires_at: float


def confirmation_decision(transcript: str) -> str | None:
    normalized = unicodedata.normalize("NFD", transcript.lower())
    normalized = "".join(char for char in normalized if unicodedata.category(char) != "Mn").replace("đ", "d")
    normalized = re.sub(r"[^a-z\s]", " ", normalized)
    normalized = " ".join(normalized.split())
    if normalized in {"huy", "khong", "khong dong y", "khong xac nhan", "no"}:
        return "deny"
    if normalized in {"xac nhan", "dong y", "co", "yes", "ok"}:
        return "approve"
    return None


def confirmation_summary(action: ActionProposal) -> str:
    if action.intent == "window.set_position":
        return f"đặt cửa kính bên tài ở mức {action.arguments['position_percent']}%"
    if action.intent == "door.set_lock":
        return "khóa cửa bên tài" if action.arguments["locked"] else "mở khóa cửa bên tài"
    return "mở cửa xe bên tài" if action.arguments["open"] else "đóng cửa xe bên tài"


class Orchestrator:
    def __init__(self, llm: LLMAdapter, vehicle: VehicleSimulator | MqttVehicleAdapter, store: DataStore):
        self.llm = llm
        self.vehicle = vehicle
        self.store = store
        self._pending: dict[str, PendingConfirmation] = {}
        self._pending_lock = threading.Lock()

    def _response(
        self, request: TurnRequest, started: float, action: ActionProposal, status: str, message: str,
        vehicle: VehicleState | None, *, confirmation_id: str | None = None, llm_ms: float | None = None,
    ) -> TurnResponse:
        latency = {"total": round((time.perf_counter() - started) * 1000, 2)}
        if llm_ms is not None:
            latency["llm"] = round(llm_ms, 2)
        response = TurnResponse(
            session_id=request.session_id,
            turn_id=request.turn_id,
            transcript=request.transcript,
            provider=self.llm.name,
            status=status,
            action=action,
            vehicle_state=vehicle,
            message=message,
            confirmation_id=confirmation_id,
            latency_ms=latency,
        )
        self.store.append_event(response.model_dump())
        return response

    async def run(self, request: TurnRequest) -> TurnResponse:
        started = time.perf_counter()
        try:
            supplied_state = None if request.confirmation_id else request.vehicle_state
            vehicle = await self.vehicle.get_state(request.session_id, supplied_state)
        except Exception:
            return self._response(
                request, started, ActionProposal(intent="conversation.clarify"), "unverified",
                "Chưa kết nối được xe mô phỏng. Mình chưa thực hiện thao tác nào.", None,
            )

        with self._pending_lock:
            pending = self._pending.get(request.session_id)
            expired = pending is not None and time.monotonic() >= pending.expires_at
            if expired:
                self._pending.pop(request.session_id, None)
            elif pending and request.confirmation_id == pending.confirmation_id:
                decision = confirmation_decision(request.transcript)
                if decision is not None:
                    self._pending.pop(request.session_id, None)
            elif pending and request.confirmation_id is None and confirmation_decision(request.transcript) is None:
                self._pending.pop(request.session_id, None)

        if expired:
            return self._response(request, started, pending.action, "blocked", "Xác nhận đã hết hạn. Hãy yêu cầu lại.", vehicle)
        if request.confirmation_id:
            if not pending or request.confirmation_id != pending.confirmation_id:
                return self._response(request, started, ActionProposal(intent="conversation.clarify"), "blocked", "Không có yêu cầu xác nhận phù hợp.", vehicle)
            if decision is None:
                return self._response(
                    request, started, pending.action, "confirm", "Hãy trả lời đúng 'Xác nhận' hoặc 'Hủy'.",
                    vehicle, confirmation_id=pending.confirmation_id,
                )
            if decision == "deny":
                return self._response(request, started, pending.action, "blocked", "Đã hủy yêu cầu; xe không thay đổi.", vehicle)
            if vehicle.vehicle_id != pending.vehicle_id or vehicle.state_version != pending.state_version:
                return self._response(request, started, pending.action, "blocked", "Trạng thái xe đã đổi. Hãy yêu cầu lại để xác nhận trạng thái mới.", vehicle)
            safety = validate(pending.action, vehicle)
            if not safety.allowed:
                return self._response(request, started, pending.action, safety.status, safety.message, vehicle)
            outcome = await self.vehicle.execute(request.session_id, pending.source_turn_id, pending.action, vehicle)
            return self._response(request, started, pending.action, outcome.status, outcome.message, outcome.state)
        if pending and confirmation_decision(request.transcript) is not None:
            return self._response(request, started, pending.action, "blocked", "Thiếu mã xác nhận của yêu cầu hiện tại.", vehicle)

        llm_started = time.perf_counter()
        proposal = await self.llm.propose(request.transcript, vehicle)
        llm_ms = (time.perf_counter() - llm_started) * 1000
        safety = validate(proposal, vehicle)
        confirmation_id = None
        if safety.allowed and proposal.intent in SENSITIVE_INTENTS:
            confirmation_id = str(uuid4())
            pending = PendingConfirmation(
                confirmation_id=confirmation_id,
                action=proposal,
                source_turn_id=request.turn_id,
                vehicle_id=vehicle.vehicle_id,
                state_version=vehicle.state_version,
                expires_at=time.monotonic() + CONFIRMATION_TTL_SECONDS,
            )
            with self._pending_lock:
                self._pending[request.session_id] = pending
            message = f"Bạn xác nhận {confirmation_summary(proposal)}? Hãy trả lời 'Xác nhận' hoặc 'Hủy' trong 30 giây."
            status = "confirm"
        elif safety.allowed and proposal.intent == "manual.search":
            message, status = "Mình đã mở cẩm nang minh họa.", "verified"
        elif safety.allowed and proposal.intent == "vehicle.get_status":
            message = f"Xe còn {vehicle.battery_percent} phần trăm pin, nhiệt độ {vehicle.temperature_celsius:g} độ."
            status = "verified"
        elif safety.allowed:
            outcome = await self.vehicle.execute(request.session_id, request.turn_id, proposal, vehicle)
            vehicle, message, status = outcome.state, outcome.message, outcome.status
        else:
            message, status = safety.message, safety.status
        return self._response(
            request, started, proposal, status, message, vehicle,
            confirmation_id=confirmation_id, llm_ms=llm_ms,
        )
