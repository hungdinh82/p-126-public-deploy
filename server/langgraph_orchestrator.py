from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import replace
from typing import Any
from uuid import uuid4

from src.actions.gateway import VehicleActionGateway
from src.agents.classifier import RulesIntentClassifier
from src.agents.contracts import AssistantOutput
from src.agents.graph import build_graph
from src.config import get_settings
from src.history.sqlite import SQLiteConversationHistory
from src.rag.retrieval_lexical import LexicalHandbookRetriever
from src.rag.runtime import HandbookServices, create_services

from .adapters.llm import LLMAdapter
from .data_store import DataStore
from .orchestrator import Orchestrator
from .schemas import (
    ActionProposal,
    ConfirmationPreview,
    Evidence,
    TraceSpan,
    TurnRequest,
    TurnResponse,
    VehicleState,
)
from .vehicle import VehicleSimulator


class _UnavailableHandbookGenerator:
    def generate(self, *args, **kwargs):
        raise RuntimeError("GOOGLE_API_KEY chưa được cấu hình cho handbook RAG")


class LangGraphOrchestrator:
    """Expose the LangGraph pipeline through ViVi's existing voice contract."""

    def __init__(
        self,
        graphs: dict[str, Any],
        gateway: VehicleActionGateway,
        store: DataStore,
        fallback: Orchestrator,
    ) -> None:
        self.graphs = graphs
        self.gateway = gateway
        self.store = store
        self.fallback = fallback
        self.llm = fallback.llm

    @property
    def graph_providers(self) -> set[str]:
        return set(self.graphs)

    async def run(self, request: TurnRequest, llm: LLMAdapter | None = None) -> TurnResponse:
        provider = (llm or self.llm).name
        graph = self.graphs.get(provider)
        if graph is None:
            return await self.fallback.run(request, llm=llm)
        result = await graph.ainvoke(
            {
                "input_text": request.transcript,
                "session_id": request.session_id,
                "turn_id": request.turn_id,
                "vehicle_model": "VF8",
                "model_year": 2026,
                "locale": "vi_vn",
                "vehicle_state": request.vehicle_state.model_dump(mode="json")
                if request.vehicle_state
                else None,
                "confirmation_id": request.confirmation_id,
                "confirmation_decision": request.confirmation_decision,
            }
        )
        output = AssistantOutput.model_validate(result["output"])
        return self._to_turn_response(request, provider, output)

    async def run_stream(
        self, request: TurnRequest, llm: LLMAdapter | None = None
    ) -> AsyncIterator[dict]:
        provider = (llm or self.llm).name
        if provider not in self.graphs:
            async for event in self.fallback.run_stream(request, llm=llm):
                yield event
            return
        response = await self.run(request, llm=llm)
        speech_segments, remainder = Orchestrator._take_speech_segments(response.message)
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
        self.store.append_event(response.model_dump(mode="json"))
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


def create_langgraph_orchestrator(
    fallback: Orchestrator,
    vehicle: VehicleSimulator,
    store: DataStore,
) -> LangGraphOrchestrator:
    config = get_settings()
    gateway = VehicleActionGateway(vehicle=vehicle)
    try:
        google_services = create_services(config, retrieval_mode="lexical")
        google_services.action_gateway = gateway
        rule_services = replace(google_services, classifier=RulesIntentClassifier())
        graphs = {
            "google": build_graph(google_services),
            "rules": build_graph(rule_services),
        }
    except RuntimeError:
        degraded = HandbookServices(
            retriever=LexicalHandbookRetriever(config.rag_data_dir, final_k=config.rag_final_k),
            generator=_UnavailableHandbookGenerator(),  # type: ignore[arg-type]
            history=SQLiteConversationHistory(config.rag_history_db),
            history_turns=config.rag_history_turns,
            classifier=RulesIntentClassifier(),
            action_gateway=gateway,
        )
        graphs = {"rules": build_graph(degraded)}
    return LangGraphOrchestrator(graphs, gateway, store, fallback)
