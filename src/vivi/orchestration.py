from __future__ import annotations

import random
import re
from collections.abc import AsyncIterator
from typing import Any
from uuid import uuid4

from src.vivi.agents.contracts import AssistantOutput
from src.vivi.agents.graph import build_graph
from src.vivi.config import get_settings
from src.vivi.persistence.event_store import EventStore
from src.vivi.providers import configured_llm_providers
from src.vivi.rag.runtime import create_services
from src.vivi.vehicle.gateway import VehicleActionGateway

from .domain.models import (
    ActionProposal,
    ConfirmationPreview,
    Evidence,
    TraceSpan,
    TurnRequest,
    TurnResponse,
    VehicleState,
)
from .vehicle.memory import VehicleSimulator

# Spoken while a handbook answer is retrieved and generated, which takes a few
# seconds on cloud models, so the cabin is not silent.
PROGRESS_MESSAGES = (
    "Mình đang tìm thông tin trong cẩm nang, bạn chờ chút nhé.",
    "Để mình tra cẩm nang VF8 một chút nhé.",
    "Mình đang tìm kiếm thông tin, bạn đợi mình một lát.",
)


class LangGraphOrchestrator:
    """Expose the LangGraph pipeline through ViVi's existing voice contract."""

    def __init__(
        self,
        graphs: dict[str, Any],
        gateway: VehicleActionGateway,
        store: EventStore,
        default_provider: str = "rules",
        provider_errors: dict[str, str] | None = None,
    ) -> None:
        self.graphs = graphs
        self.gateway = gateway
        self.store = store
        self.default_provider = default_provider
        self.provider_errors = provider_errors or {}

    @property
    def graph_providers(self) -> set[str]:
        return set(self.graphs)

    def _graph(self, provider: str | None) -> tuple[str, Any]:
        provider = provider or self.default_provider
        graph = self.graphs.get(provider)
        if graph is None:
            detail = self.provider_errors.get(provider, "LLM provider không khả dụng")
            raise ValueError(detail)
        return provider, graph

    @staticmethod
    def _graph_input(request: TurnRequest) -> dict[str, Any]:
        return {
            "input_text": request.transcript,
            "session_id": request.session_id,
            "turn_id": request.turn_id,
            "vehicle_model": "VF8",
            "model_year": 2026,
            "locale": "vi_vn",
            # Client state is never trusted at the policy boundary.
            "vehicle_state": None,
            "confirmation_id": request.confirmation_id,
            "confirmation_decision": request.confirmation_decision,
        }

    async def run(self, request: TurnRequest, provider: str | None = None) -> TurnResponse:
        provider, graph = self._graph(provider)
        result = await graph.ainvoke(self._graph_input(request))
        output = AssistantOutput.model_validate(result["output"])
        return self._to_turn_response(request, provider, output)

    async def run_stream(
        self, request: TurnRequest, provider: str | None = None
    ) -> AsyncIterator[dict]:
        provider, graph = self._graph(provider)
        result: dict[str, Any] = {}
        async for mode, chunk in graph.astream(
            self._graph_input(request), stream_mode=["updates", "values"]
        ):
            if mode == "values":
                result = chunk
            elif "scope_guard" in chunk and (chunk["scope_guard"] or {}).get(
                "status"
            ) != "out_of_scope":
                # The question passed the scope check, so retrieval and answer
                # generation follow; announce the wait before the slow part.
                yield {"type": "progress", "text": random.choice(PROGRESS_MESSAGES)}
        output = AssistantOutput.model_validate(result["output"])
        response = self._to_turn_response(request, provider, output)
        speech_segments, remainder = self._take_speech_segments(response.message)
        if remainder.strip():
            speech_segments.append(remainder.strip())
        for segment in speech_segments:
            yield {"type": "speech", "text": segment}
        yield {
            "type": "final",
            "streamed_speech": bool(speech_segments),
            "response": response.model_dump(mode="json"),
        }

    def _to_turn_response(
        self,
        request: TurnRequest,
        provider: str,
        output: AssistantOutput,
    ) -> TurnResponse:
        action = None
        if output.action_proposal is not None:
            action = ActionProposal(
                intent=output.action_proposal.intent,
                arguments=output.action_proposal.arguments,
                confidence=output.action_proposal.confidence,
            )
        vehicle_state = self.gateway.vehicle.state_for(request.session_id)
        if output.vehicle_state:
            vehicle_state = VehicleState.model_validate(output.vehicle_state)
        route_map = {
            "action": "vehicle",
            "handbook": "handbook",
            "conversation": "conversation",
            "clarify": "clarify",
            "unsupported": "unsupported",
            "invalid": "unsupported",
        }
        status_map = {
            "action_verified": "verified",
            "answered": "verified",
            "action_unverified": "unverified",
            "classification_error": "error",
            "generation_error": "error",
            "retrieval_error": "error",
            "invalid_input": "error",
            "out_of_scope": "unsupported",
            "insufficient_evidence": "unsupported",
        }
        status = status_map.get(output.status, output.status)
        if status not in {
            "verified",
            "clarify",
            "blocked",
            "confirmation_required",
            "denied",
            "expired",
            "unsupported",
            "unverified",
            "error",
        }:
            status = "error"
        evidence = [
            Evidence(
                source_id=item.get("source_id", ""),
                section=" > ".join(item.get("section_path") or []),
                source_url=item.get("source_url"),
            )
            for item in output.citations
        ]
        confirmation = None
        if output.confirmation is not None and action is not None:
            confirmation = ConfirmationPreview(
                confirmation_id=output.confirmation.confirmation_id,
                preview=output.confirmation.preview,
                action=action,
                risk_class=(output.execution.risk_class if output.execution else "R2") or "R2",
                expires_at=output.confirmation.expires_at,
            )
        trace = [
            TraceSpan(
                stage=self._trace_stage(stage),
                outcome=output.status,
                latency_ms=latency,
                detail={"node": stage},
            )
            for stage, latency in output.timings.items()
        ]
        response = TurnResponse(
            session_id=output.session_id,
            turn_id=output.turn_id,
            trace_id=str(uuid4()),
            transcript=request.transcript,
            provider=f"langgraph/{provider}",
            route=route_map[output.route],
            status=status,
            action=action,
            risk_class=output.execution.risk_class if output.execution else None,
            confirmation=confirmation,
            evidence=evidence,
            grounding_status=output.grounding_status,
            vehicle_state=vehicle_state,
            message=output.response_text,
            error="; ".join(item["message"] for item in output.errors) or None,
            latency_ms={**output.timings, "total": round(sum(output.timings.values()), 2)},
            trace=trace,
        )
        self.store.append_event({"event_type": "turn", **response.model_dump(mode="json")})
        return response

    @staticmethod
    def _trace_stage(stage: str) -> str:
        if stage in {"classify_intent", "retrieve", "generate", "validate_citations"}:
            return "model"
        if stage in {"scope_guard", "validate_action", "safety_check", "resolve_confirmation"}:
            return "policy"
        if stage in {"execute_action", "verify_action"}:
            return "tool"
        if stage == "compose_output":
            return "result"
        return "route"

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


def create_langgraph_orchestrator(
    vehicle: VehicleSimulator,
    store: EventStore,
    config=None,
) -> LangGraphOrchestrator:
    config = config or get_settings()
    gateway = VehicleActionGateway(vehicle=vehicle)
    graphs = {}
    provider_errors = {}
    for provider in configured_llm_providers(config):
        try:
            services = create_services(
                config,
                retrieval_mode=config.rag_retrieval_mode,
                provider=provider,
            )
            services.action_gateway = gateway
            graphs[provider] = build_graph(services)
        except Exception as exc:
            provider_errors[provider] = str(exc)
    if "rules" not in graphs:
        raise RuntimeError(f"Không khởi tạo được rules graph: {provider_errors['rules']}")
    default_provider = config.llm_provider if config.llm_provider in graphs else "rules"
    return LangGraphOrchestrator(
        graphs,
        gateway,
        store,
        default_provider=default_provider,
        provider_errors=provider_errors,
    )
