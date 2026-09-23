from __future__ import annotations

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


class Orchestrator:
    def __init__(
        self,
        llm: LLMAdapter,
        vehicle: VehicleSimulator,
        store: DataStore,
        confirmations: ConfirmationStore | None = None,
    ):
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
            provider=self.llm.name,
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
    ) -> TurnResponse:
        tool_started = time.perf_counter()
        try:
            vehicle, message = await self.vehicle.execute(request.session_id, request.turn_id, action)
        except Exception as exc:
            self._trace(state, "tool", "error", tool_started, error=type(exc).__name__)
            state["errors"].append(str(exc))
            return self._finish(state, request, started, "unverified", "Chưa thể xác minh thao tác trên xe mô phỏng.", safety=safety, error=str(exc))
        state["vehicle_state"] = vehicle
        self._trace(state, "tool", "verified", tool_started, intent=action.intent)
        return self._finish(state, request, started, "verified", message, safety=safety)

    async def run(self, request: TurnRequest) -> TurnResponse:
        started = time.perf_counter()
        vehicle = self.vehicle.state_for(request.session_id, request.vehicle_state)
        state = initial_state(request.session_id, request.turn_id, str(uuid4()), request.transcript, vehicle)

        if request.confirmation_id or request.confirmation_decision:
            policy_started = time.perf_counter()
            if not request.confirmation_id or not request.confirmation_decision:
                self._trace(state, "policy", "denied", policy_started, reason="incomplete_confirmation")
                return self._finish(state, request, started, "denied", "Thiếu mã hoặc quyết định xác nhận.")
            resolution = self.confirmations.resolve(
                request.confirmation_id,
                request.session_id,
                request.confirmation_decision,
            )
            if resolution.action is None:
                self._trace(state, "policy", resolution.status, policy_started)
                return self._finish(state, request, started, resolution.status, resolution.message)
            action = resolution.action
            state["action"] = action
            state["intent"] = action.intent
            state["route"] = route_action(action)
            safety = validate(action, state["vehicle_state"], confirmed=True)
            state["risk_class"] = safety.risk_class
            self._trace(state, "policy", safety.status, policy_started, risk_class=safety.risk_class)
            if not safety.allowed:
                return self._finish(state, request, started, safety.status, safety.message, safety=safety)
            return await self._execute(state, request, started, action, safety)

        model_started = time.perf_counter()
        action = await self.llm.propose(request.transcript, vehicle)
        state["action"] = action
        state["intent"] = action.intent
        self._trace(state, "model", "proposed", model_started, provider=self.llm.name, intent=action.intent)

        route_started = time.perf_counter()
        state["route"] = route_action(action)
        self._trace(state, "route", state["route"], route_started, intent=action.intent)

        policy_started = time.perf_counter()
        safety = validate(action, vehicle)
        state["risk_class"] = safety.risk_class
        self._trace(state, "policy", safety.status, policy_started, risk_class=safety.risk_class)

        if state["route"] == "clarify":
            return self._finish(state, request, started, "clarify", safety.message, safety=safety)
        if state["route"] == "conversation":
            return self._finish(state, request, started, "verified", action.spoken_response, safety=safety)
        if state["route"] == "handbook":
            message = "Chưa có bằng chứng cẩm nang phù hợp để trả lời câu hỏi này."
            return self._finish(state, request, started, "unsupported", message, safety=safety)
        if state["route"] == "unsupported":
            status = "blocked" if action.intent == "vehicle.prohibited" else "unsupported"
            return self._finish(state, request, started, status, safety.message or action.spoken_response, safety=safety)
        if safety.requires_confirmation:
            confirmation = self.confirmations.create(request.session_id, action, safety.preview or safety.message)
            state["confirmation_id"] = confirmation.confirmation_id
            return self._finish(
                state,
                request,
                started,
                "confirmation_required",
                confirmation.preview,
                safety=safety,
                confirmation=confirmation,
            )
        if not safety.allowed:
            return self._finish(state, request, started, safety.status, safety.message, safety=safety)
        return await self._execute(state, request, started, action, safety)

