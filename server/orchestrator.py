from __future__ import annotations

from collections.abc import AsyncIterator
import json
import re
import time
from uuid import uuid4

from .adapters.llm import LLMAdapter
from .agent_state import AgentState, initial_state
from .confirmations import ConfirmationStore
from .data_store import DataStore
from .router import route_action
from .safety import SafetyResult, validate
from .schemas import ActionProposal, TraceSpan, TurnRequest, TurnResponse
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
    """Legacy provider orchestrator retained as a degraded/test fallback."""

    def __init__(
        self,
        llm: LLMAdapter,
        vehicle: VehicleSimulator,
        store: DataStore,
        confirmations: ConfirmationStore | None = None,
    ) -> None:
        self.llm = llm
        self.vehicle = vehicle
        self.store = store
        self.confirmations = confirmations or ConfirmationStore()

    @staticmethod
    def _trace(state: AgentState, stage: str, outcome: str, started: float, **detail: object) -> None:
        state["trace"].append(
            TraceSpan(
                stage=stage,
                outcome=outcome,
                latency_ms=round((time.perf_counter() - started) * 1000, 2),
                detail=detail,
            )
        )

    def _finish(
        self,
        state: AgentState,
        request: TurnRequest,
        started: float,
        status: str,
        message: str,
        *,
        provider: str,
        safety: SafetyResult | None = None,
        confirmation=None,
        error: str | None = None,
    ) -> TurnResponse:
        self._trace(state, "result", status, started)
        latency = {span.stage: span.latency_ms for span in state["trace"]}
        latency["total"] = round((time.perf_counter() - started) * 1000, 2)
        response = TurnResponse(
            session_id=request.session_id,
            turn_id=request.turn_id,
            trace_id=state["trace_id"],
            transcript=request.transcript,
            provider=provider,
            route=state["route"] or "unsupported",
            status=status,
            action=state["action"],
            risk_class=safety.risk_class if safety else state["risk_class"],
            confirmation=confirmation,
            evidence=state["evidence"],
            vehicle_state=state["vehicle_state"],
            message=message,
            error=error,
            latency_ms=latency,
            trace=state["trace"],
        )
        self.store.append_event(response.model_dump(mode="json"))
        return response

    async def _execute(
        self,
        state: AgentState,
        request: TurnRequest,
        started: float,
        action: ActionProposal,
        safety: SafetyResult,
        provider: str,
    ) -> TurnResponse:
        tool_started = time.perf_counter()
        try:
            vehicle, message = await self.vehicle.execute(request.session_id, request.turn_id, action)
        except Exception as exc:
            self._trace(state, "tool", "error", tool_started, error=type(exc).__name__)
            state["errors"].append(str(exc))
            return self._finish(
                state,
                request,
                started,
                "unverified",
                "Chưa thể xác minh thao tác trên xe mô phỏng.",
                provider=provider,
                safety=safety,
                error=str(exc),
            )
        state["vehicle_state"] = vehicle
        self._trace(state, "tool", "verified", tool_started, intent=action.intent)
        return self._finish(
            state, request, started, "verified", message, provider=provider, safety=safety
        )

    async def _process_action(
        self,
        state: AgentState,
        request: TurnRequest,
        started: float,
        action: ActionProposal,
        provider: str,
    ) -> TurnResponse:
        state["action"] = action
        state["intent"] = action.intent
        route_started = time.perf_counter()
        state["route"] = route_action(action)
        self._trace(state, "route", state["route"], route_started, intent=action.intent)
        policy_started = time.perf_counter()
        safety = validate(action, state["vehicle_state"])
        state["risk_class"] = safety.risk_class
        self._trace(state, "policy", safety.status, policy_started, risk_class=safety.risk_class)

        if state["route"] == "clarify":
            return self._finish(
                state, request, started, "clarify", safety.message, provider=provider, safety=safety
            )
        if state["route"] == "conversation":
            return self._finish(
                state,
                request,
                started,
                "verified",
                action.spoken_response,
                provider=provider,
                safety=safety,
            )
        if state["route"] == "handbook":
            return self._finish(
                state,
                request,
                started,
                "unsupported",
                "Chưa có bằng chứng cẩm nang phù hợp để trả lời câu hỏi này.",
                provider=provider,
                safety=safety,
            )
        if state["route"] == "unsupported":
            status = "blocked" if action.intent == "vehicle.prohibited" else "unsupported"
            return self._finish(
                state,
                request,
                started,
                status,
                safety.message or action.spoken_response,
                provider=provider,
                safety=safety,
            )
        if safety.requires_confirmation:
            confirmation = self.confirmations.create(
                request.session_id, action, safety.preview or safety.message
            )
            state["confirmation_id"] = confirmation.confirmation_id
            return self._finish(
                state,
                request,
                started,
                "confirmation_required",
                confirmation.preview,
                provider=provider,
                safety=safety,
                confirmation=confirmation,
            )
        if not safety.allowed:
            return self._finish(
                state,
                request,
                started,
                safety.status,
                safety.message,
                provider=provider,
                safety=safety,
            )
        return await self._execute(state, request, started, action, safety, provider)

    async def run(self, request: TurnRequest, llm: LLMAdapter | None = None) -> TurnResponse:
        selected = llm or self.llm
        started = time.perf_counter()
        vehicle = self.vehicle.state_for(request.session_id, request.vehicle_state)
        state = initial_state(
            request.session_id, request.turn_id, str(uuid4()), request.transcript, vehicle
        )

        if request.confirmation_id or request.confirmation_decision:
            policy_started = time.perf_counter()
            if not request.confirmation_id or not request.confirmation_decision:
                self._trace(state, "policy", "denied", policy_started, reason="incomplete_confirmation")
                return self._finish(
                    state,
                    request,
                    started,
                    "denied",
                    "Thiếu mã hoặc quyết định xác nhận.",
                    provider=selected.name,
                )
            resolution = self.confirmations.resolve(
                request.confirmation_id, request.session_id, request.confirmation_decision
            )
            if resolution.action is None:
                self._trace(state, "policy", resolution.status, policy_started)
                return self._finish(
                    state,
                    request,
                    started,
                    resolution.status,
                    resolution.message,
                    provider=selected.name,
                )
            action = resolution.action
            state["action"] = action
            state["intent"] = action.intent
            state["route"] = route_action(action)
            safety = validate(action, state["vehicle_state"], confirmed=True)
            state["risk_class"] = safety.risk_class
            self._trace(state, "policy", safety.status, policy_started, risk_class=safety.risk_class)
            if not safety.allowed:
                return self._finish(
                    state,
                    request,
                    started,
                    safety.status,
                    safety.message,
                    provider=selected.name,
                    safety=safety,
                )
            return await self._execute(state, request, started, action, safety, selected.name)

        model_started = time.perf_counter()
        action = await selected.propose(request.transcript, vehicle)
        self._trace(
            state, "model", "proposed", model_started, provider=selected.name, intent=action.intent
        )
        return await self._process_action(state, request, started, action, selected.name)

    async def run_stream(
        self, request: TurnRequest, llm: LLMAdapter | None = None
    ) -> AsyncIterator[dict]:
        selected = llm or self.llm
        if request.confirmation_id or request.confirmation_decision:
            response = await self.run(request, llm=selected)
            yield {
                "type": "final",
                "streamed_speech": False,
                "response": response.model_dump(mode="json"),
            }
            return

        started = time.perf_counter()
        vehicle = self.vehicle.state_for(request.session_id, request.vehicle_state)
        state = initial_state(
            request.session_id, request.turn_id, str(uuid4()), request.transcript, vehicle
        )
        document = ""
        spoken_seen = ""
        pending_speech = ""
        streamed_speech = False
        model_started = time.perf_counter()
        async for delta in selected.stream_json(request.transcript, vehicle):
            document += delta
            if self._partial_intent(document) != "conversation.respond":
                continue
            spoken = self._partial_json_string(document, "spoken_response")
            if len(spoken) <= len(spoken_seen):
                continue
            pending_speech += spoken[len(spoken_seen) :]
            spoken_seen = spoken
            segments, pending_speech = self._take_speech_segments(pending_speech)
            for segment in segments:
                streamed_speech = True
                yield {"type": "speech", "text": segment}

        action = ActionProposal.model_validate_json(document)
        self._trace(
            state, "model", "proposed", model_started, provider=selected.name, intent=action.intent
        )
        response = await self._process_action(state, request, started, action, selected.name)
        if action.intent == "conversation.respond" and pending_speech.strip():
            streamed_speech = True
            yield {"type": "speech", "text": pending_speech.strip()}
        yield {
            "type": "final",
            "streamed_speech": streamed_speech,
            "response": response.model_dump(mode="json"),
        }

    @staticmethod
    def _partial_intent(document: str) -> str | None:
        match = re.search(r'"intent"\s*:\s*"([^"\\]+)"', document)
        return match.group(1) if match else None

    @staticmethod
    def _partial_json_string(document: str, key: str) -> str:
        match = re.search(rf'"{re.escape(key)}"\s*:\s*"', document)
        if not match:
            return ""
        start = match.end()
        escaped = False
        end = len(document)
        for index in range(start, len(document)):
            char = document[index]
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                end = index
                break
        raw = document[start:end]
        while raw:
            try:
                return json.loads(f'"{raw}"')
            except json.JSONDecodeError:
                raw = raw[:-1]
        return ""

    @staticmethod
    def _take_speech_segments(text: str) -> tuple[list[str], str]:
        segments: list[str] = []
        while True:
            sentence = re.search(r"^(.+?[.!?…])(?:\s+|$)", text, re.S)
            clause = re.search(r"^(.{45,}?[,:;])(?:\s+|$)", text, re.S)
            match = sentence or clause
            if match:
                segments.append(match.group(1).strip())
                text = text[match.end() :]
                continue
            if len(text) >= 100:
                split = text.rfind(" ", 0, 90)
                if split > 40:
                    segments.append(text[:split].strip())
                    text = text[split + 1 :]
                    continue
            break
        return segments, text
