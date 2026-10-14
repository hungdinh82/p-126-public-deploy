from __future__ import annotations

import asyncio
import random
import re
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, nullcontext
from typing import Any
from uuid import uuid4

from src.vivi.agents.contracts import AssistantOutput
from src.vivi.config import get_settings
from src.vivi.graphs.assistant import CONTROL_EVENTS
from src.vivi.history.erasure import clear_backups
from src.vivi.persistence.event_store import EventStore
from src.vivi.persistence.runtime_store import RuntimeStore
from src.vivi.providers import configured_llm_providers
from src.vivi.rag.runtime import create_services
from src.vivi.text import strip_address
from src.vivi.turn_control import TurnControl, current_control
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
        ledger: RuntimeStore | None = None,
        timeout_seconds: float = 45,
        profile_id: str = "default",
    ) -> None:
        self.graphs = graphs
        self.gateway = gateway
        self.store = store
        self.default_provider = default_provider
        self.provider_errors = provider_errors or {}
        self.ledger, self.timeout_seconds = ledger, timeout_seconds
        self.profile_id = profile_id
        self._sessions: dict[str, asyncio.Lock] = {}
        self._active: dict[tuple[str, str], TurnControl] = {}
        self._runners: dict[tuple[str, str], asyncio.Task] = {}
        self._closing = False
        self._resetting = False
        self._in_scope = 0
        self._reset_task = None

    def session_key(self, request):
        return f"{self.profile_id}:{request.session_id}"

    @asynccontextmanager
    async def turn_scope(self, request, provider):
        if self._closing:
            raise RuntimeError("Assistant runtime is closing")
        if self._resetting:
            raise ValueError("Đang xoá lịch sử. Hãy thử lại khi hoàn tất.")
        self._in_scope += 1
        session = self.session_key(request)
        lock = self._sessions.setdefault(session, asyncio.Lock())
        control = TurnControl(time.monotonic() + self.timeout_seconds)
        started = time.perf_counter()
        token = current_control.set(control)
        key = (request.session_id, request.turn_id)
        try:
            async with asyncio.timeout(self.timeout_seconds):
                async with nullcontext() if (request.navigation_event or {}).get("kind") in CONTROL_EVENTS else lock:
                    cached = (
                        await asyncio.to_thread(
                            self.ledger.reserve_turn,
                            session,
                            request.turn_id,
                            {**request.model_dump(mode="json", exclude={"vehicle_state"}), "provider": provider},
                        )
                        if self.ledger
                        else None
                    )
                    self._active[key] = control
                    self._runners[key] = asyncio.current_task()
                    try:
                        yield cached
                    except BaseException as exc:
                        control.cancel()
                        timed_out = isinstance(exc, TimeoutError) or time.monotonic() >= control.deadline
                        self.store.append_event({
                            "event_type": "turn_error", "session_id": request.session_id,
                            "turn_id": request.turn_id, "transcript": request.transcript,
                            "provider": provider,
                            "error_code": "turn_timeout" if timed_out else "turn_cancelled"
                            if isinstance(exc, asyncio.CancelledError) else "turn_failed",
                            "stage": control.stage, "dispatched": control.dispatched,
                            "latency_ms": {**control.timings, "total": round((time.perf_counter() - started) * 1000, 2)},
                        })
                        if self.ledger and not cached:
                            await asyncio.to_thread(self.ledger.fail_turn, session, request.turn_id)
                        raise
                    finally:
                        self._active.pop(key, None)
                        self._runners.pop(key, None)
        finally:
            current_control.reset(token)
            self._in_scope -= 1

    def cancel(self, session_id, turn_id):
        control = self._active.get((session_id, turn_id))
        if control is None:
            return {"cancelled": False, "dispatched": False}
        dispatched = control.cancel()
        runner = self._runners.get((session_id, turn_id))
        if not dispatched and runner:
            runner.cancel()
        return {"cancelled": True, "dispatched": dispatched}

    async def complete(self, request, response):
        if self.ledger:
            await asyncio.to_thread(
                self.ledger.finish_turn, self.session_key(request), request.turn_id, response.model_dump(mode="json")
            )

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
            "input_text": strip_address(request.transcript),
            "session_id": request.session_id,
            "turn_id": request.turn_id,
            "vehicle_model": "VF8",
            "model_year": 2026,
            "locale": "vi_vn",
            # Client state is never trusted at the policy boundary.
            "vehicle_state": None,
            "confirmation_id": request.confirmation_id,
            "confirmation_decision": request.confirmation_decision,
            "provider": request.llm_provider or "rules",
            "action": request.action.model_dump(mode="json") if request.action else None,
            "question_reply": request.question_reply.model_dump(mode="json") if request.question_reply else None,
            "navigation_event": request.navigation_event,
            "position": request.position,
        }

    async def run(self, request: TurnRequest, provider: str | None = None) -> TurnResponse:
        provider, graph = self._graph(provider)
        started = time.perf_counter()
        async with self.turn_scope(request, provider) as cached:
            if cached:
                return TurnResponse.model_validate(cached)
            result = await graph.ainvoke({**self._graph_input(request), "provider": provider})
            output = AssistantOutput.model_validate(result["output"])
            response = self._to_turn_response(
                request, provider, output, total_ms=round((time.perf_counter() - started) * 1000, 2)
            )
            await self.complete(request, response)
            return response

    async def run_stream(self, request: TurnRequest, provider: str | None = None) -> AsyncIterator[dict]:
        if (request.navigation_event or {}).get("kind") in CONTROL_EVENTS:
            response = await self.run(request, provider)
            yield {
                "type": "final",
                "streamed_speech": False,
                "response": response.model_dump(mode="json"),
                "runtime_epoch": response.runtime_epoch,
                "snapshot_sequence": response.snapshot_sequence,
                "event_id": response.event_id,
            }
            return
        provider, _ = self._graph(provider)
        sequence = 0
        async with self.turn_scope(request, provider) as cached:
            if cached:
                yield {
                    "type": "final",
                    "runtime_epoch": cached.get("runtime_epoch"),
                    "snapshot_sequence": cached.get("snapshot_sequence", 0),
                    "event_id": cached.get("event_id"),
                    "streamed_speech": False,
                    "response": cached,
                    "session_id": request.session_id,
                    "turn_id": request.turn_id,
                    "sequence": 1,
                    "version": 2,
                    "timestamp": time.time(),
                }
                return
            async for event in self._stream(request, provider):
                metadata = event.get("response") or {}
                epoch = metadata.get("runtime_epoch") or getattr(self.graphs[provider], "runtime_epoch", None)
                if epoch:
                    event = {
                        **event,
                        "runtime_epoch": epoch,
                        "snapshot_sequence": metadata.get("snapshot_sequence"),
                        "event_id": metadata.get(
                            "event_id", (request.navigation_event or {}).get("id", request.turn_id)
                        ),
                    }
                if self.ledger:
                    sequence += 1
                    event = {
                        **event,
                        "session_id": request.session_id,
                        "turn_id": request.turn_id,
                        "sequence": sequence,
                        "version": 2,
                        "timestamp": time.time(),
                        **({"speech_id": f"{request.session_id}:{request.turn_id}:{sequence}"} if event.get("type") == "speech" else {}),
                    }
                yield event

    async def _stream(self, request, provider):
        provider, graph = self._graph(provider)
        started = time.perf_counter()
        result: dict[str, Any] = {}
        spoken = False
        reported_results = 0
        graph_input = {**self._graph_input(request), "provider": provider}
        stream = (
            graph.astream_dialogue(graph_input) if hasattr(graph, "astream_dialogue")
            else graph.astream(graph_input, stream_mode=["updates", "values"])
        )
        async for mode, chunk in stream:
            if mode == "values":
                result = chunk
            elif "interpret" in chunk:
                turn = (chunk["interpret"] or {}).get("turn", {})
                yield {
                    "type": "provider",
                    "provider": turn.get("effective_provider"),
                    "attempts": turn.get("provider_attempts", []),
                }
                if any(a.get("capability") == "manual.search" for a in turn.get("semantic", {}).get("actions", [])):
                    yield {"type": "progress", "text": random.choice(PROGRESS_MESSAGES)}
                elif any(a.get("capability") in {"web.search", "knowledge.lookup", "weather.get_forecast"}
                         for a in turn.get("semantic", {}).get("actions", [])):
                    yield {"type": "progress", "text": "Mình đang tra thông tin, bạn chờ một chút nhé."}
            elif "capability" in chunk or "validate" in chunk:
                update = chunk.get("capability", chunk.get("validate")) or {}
                turn = update.get("turn", {})
                results = turn.get("results", [])
                for result in results[reported_results:]:
                    yield {"type": "step", "step": result, "provider": turn.get("effective_provider")}
                    if result.get("trip_plan_update"):
                        yield {
                            "type": "trip_plan",
                            "committed": False,
                            "trip_plan": (update.get("session") or {}).get("trip_plan", result["trip_plan_update"]),
                            "ui_events": result.get("ui_events", []),
                            "provider": turn.get("effective_provider"),
                            "step_id": result["step_id"],
                        }
                    if result.get("navigation_update"):
                        from src.vivi.dialogue.contracts import SessionState

                        projected = SessionState.model_validate(update["session"]) if update.get("session") else None
                        navigation = (
                            projected.navigation.model_dump(mode="json") if projected else result["navigation_update"]
                        )
                        yield {
                            "type": "navigation",
                            "committed": False,
                            "navigation": navigation,
                            "navigation_workspace": projected.navigation_workspace.model_dump(mode="json")
                            if projected
                            else None,
                            "ui_events": result.get("ui_events", []),
                            "provider": turn.get("effective_provider"),
                            "step_id": result["step_id"],
                        }
                reported_results = len(results)
        output = AssistantOutput.model_validate(result["output"])
        response = self._to_turn_response(
            request, provider, output, total_ms=round((time.perf_counter() - started) * 1000, 2)
        )
        await self.complete(request, response)
        speech_segments, remainder = self._take_speech_segments(response.message)
        if remainder.strip():
            speech_segments.append(remainder.strip())
        if not spoken:
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
        *,
        total_ms: float,
    ) -> TurnResponse:
        action = None
        if output.action_proposal is not None:
            action = ActionProposal(
                intent=output.action_proposal.intent,
                arguments=output.action_proposal.arguments,
                confidence=output.action_proposal.confidence,
            )
        vehicle_state = VehicleState.model_validate(output.vehicle_state) if output.vehicle_state else VehicleState()
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
                **{key: item.get(key) for key in ("title", "kind", "published_at", "retrieved_at", "valid_date", "timezone")},
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
                outcome=next(
                    (span["outcome"] for span in output.diagnostics.get("node_spans", []) if span["node"] == stage),
                    output.status,
                ),
                latency_ms=latency,
                detail={"node": stage},
            )
            for stage, latency in output.timings.items()
        ]
        response = TurnResponse(
            control_revision=output.control_revision,
            receipt=output.receipt,
            control_receipts=output.control_receipts,
            runtime_epoch=output.runtime_epoch,
            snapshot_sequence=output.snapshot_sequence,
            event_id=output.event_id,
            session_id=output.session_id,
            turn_id=output.turn_id,
            trace_id=str(uuid4()),
            transcript=request.transcript,
            provider=f"langgraph/{output.diagnostics.get('effective_provider', provider)}",
            steps=output.steps,
            navigation_workspace=output.navigation_workspace,
            navigation=output.navigation,
            trip_plan=output.trip_plan,
            pending_question=output.pending_question,
            ui_events=output.ui_events,
            route=route_map[output.route],
            status=status,
            action=action,
            risk_class=output.execution.risk_class if output.execution else None,
            execution=output.execution.model_dump(mode="json") if output.execution else None,
            confirmation=confirmation,
            evidence=evidence,
            grounding_status=output.grounding_status,
            vehicle_state=vehicle_state,
            message=output.response_text,
            answer_kind=output.answer_kind,
            answer_basis=output.answer_basis,
            error="; ".join(item["message"] for item in output.errors) or None,
            # Dependency spans overlap their containing capability span. Log
            # the same elapsed wall time returned to the client, not their sum.
            latency_ms={**output.timings, "total": total_ms},
            trace=trace,
            diagnostics={
                **output.diagnostics,
                **({"io_timings": current_control.get().io_timings}
                   if current_control.get() and current_control.get().io_timings else {}),
            },
        )
        if (request.navigation_event or {}).get("kind") not in {"progress", "charge"}:
            self.store.append_event({"event_type": "turn", **response.model_dump(mode="json")})
        return response

    async def reset_history(self, session_id=None):
        if self._closing or self._resetting or self._in_scope:
            raise ValueError("Đợi lượt đang xử lý hoàn tất rồi xoá lịch sử.")
        self._resetting = True
        self._reset_task = asyncio.create_task(self._reset_history(session_id))
        return await asyncio.shield(self._reset_task)

    async def _reset_history(self, session_id):
        try:
            graphs = set(self.graphs.values())
            history = next(iter(graphs)).navigation.workflow.services.history
            sessions = set(await asyncio.to_thread(history.sessions, self.profile_id))
            if self.ledger:
                sessions.update(await asyncio.to_thread(self.ledger.sessions, self.profile_id))
            sessions.update(key.split(":", 1)[1] for key in self._sessions if key.startswith(self.profile_id + ":"))
            if session_id:
                sessions.add(session_id)
            for graph in graphs:
                for sid in sessions:
                    from src.vivi.dialogue.contracts import SessionState

                    state = await graph.navigation.hydrate(SessionState(), sid)
                    if state.navigation_workspace.draft:
                        state.navigation_workspace.draft.offer = None
                        state.navigation_workspace.draft.clarification = None
                    if state.trip_plan:
                        state.trip_plan.offer = None
                    state.snapshot_sequence += 1
                    await graph.navigation.save(state, sid, invalidate=True)
                    await asyncio.to_thread(self.gateway.confirmations.cancel, sid)
                    thread_id = graph.config({"session_id": sid})["configurable"]["thread_id"]
                    await graph.saver.adelete_thread(thread_id)
                    graph._dialogue_sessions.pop(thread_id, None)
            deleted = await asyncio.to_thread(history.clear, sessions, self.profile_id)
            if self.ledger:
                await asyncio.to_thread(self.ledger.clear_history, self.profile_id, sessions)
            await asyncio.to_thread(clear_backups, history, self.ledger, graphs, sessions, self.profile_id)
            records = await asyncio.to_thread(self.store.clear_sessions, sessions)
            return {"profile_id": self.profile_id, "sessions": len(sessions), "deleted_turns": deleted,
                    "deleted_log_records": records}
        finally:
            self._resetting = False

    async def navigation_snapshot(self, session_id, event_id=None):
        _, graph = self._graph(None)
        return await graph.snapshot(session_id, event_id)

    async def aclose(self):
        self._closing = True
        if self._reset_task:
            await asyncio.gather(self._reset_task, return_exceptions=True)
        tasks = [task for task in self._runners.values() if task is not asyncio.current_task()]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for graph in set(self.graphs.values()):
            if hasattr(graph, "aclose"):
                await graph.aclose()

    @staticmethod
    def _trace_stage(stage: str) -> str:
        if stage == "interpret":
            return "model"
        if stage == "validate":
            return "policy"
        if stage == "capability":
            return "tool"
        if stage == "response":
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
    from src.vivi.dialogue.planner import ModelProvider, SemanticResolver
    from src.vivi.graphs.assistant import build_assistant_graph
    from src.vivi.persistence.checkpoints import ThreadedSqliteSaver
    from src.vivi.persistence.confirmations import DurableConfirmations
    from src.vivi.persistence.migration import migrate_runtime, repair_logged_name

    ledger = RuntimeStore(config.data_dir / "runtime" / "state.sqlite3")
    gateway = VehicleActionGateway(vehicle=vehicle, confirmations=DurableConfirmations(ledger), ledger=ledger)
    migrate_runtime(
        ledger,
        databases=[config.rag_history_db, config.memory_path, config.data_dir / "runtime" / "conversation-v3.sqlite3"],
    )
    repair_logged_name(config.memory_path)
    services = create_services(config, provider="rules")
    services.action_gateway = gateway
    import json

    from src.vivi.navigation.energy import EnergyPlanner, EnergyProfile, load_assumptions, load_stations

    energy_profile = (
        EnergyProfile.model_validate(json.loads(config.energy_profile_path.read_text()))
        if config.energy_profile_path
        else None
    )
    services.energy_planner = EnergyPlanner(energy_profile, load_stations(config.charging_stations_path))
    services.navigation_categories = {
        "pharmacy": config.navigation_pharmacy_category,
        "charging": config.navigation_charging_category,
    }
    if services.navigation is not None:
        from src.vivi.navigation.charging import VietMapChargingDiscovery

        services.charging_discovery = VietMapChargingDiscovery(
            services.navigation,
            services.energy_planner.stations,
            category=config.navigation_charging_category,
            radius_m=config.navigation_charging_radius_m,
            budget_seconds=config.navigation_charging_search_seconds,
            max_detour_m=config.navigation_charging_max_detour_m,
            assumptions=load_assumptions(config.charging_assumptions_path, config.charging_assumptions_enabled),
        )
    providers, generators, errors = {}, {"rules": services.generator}, {}
    for name in set(configured_llm_providers(config)) - {"rules"}:
        try:
            candidate = create_services(config, provider=name)
            client = candidate.semantic_client
            client.max_tokens, client.max_attempts = config.planner_max_tokens, 1
            budget = config.local_budget_seconds if name == "local" else config.cloud_budget_seconds
            providers[name] = ModelProvider(name, client, budget)
            generators[name] = candidate.generator
        except Exception as exc:
            errors[name] = str(exc)
    fallback_chains = {}
    if config.jetson_ai_policy != "disabled":
        cloud = (
            []
            if config.jetson_ai_policy == "offline_only" or config.rag_local_only
            else [name for name in ("google", "openai", "openrouter") if name in providers]
        )
        fallback_chains["local"] = ["local", *cloud]
    resolver = SemanticResolver(
        providers, breaker_seconds=config.provider_breaker_seconds,
        budget_seconds=config.semantic_budget_seconds, fallback_chains=fallback_chains
    )
    from src.vivi.conversation.service import ConversationService
    from src.vivi.information.service import InformationService

    services.conversation = ConversationService(providers, fallback_chains=fallback_chains, enabled=config.conversation_enabled)
    services.information = InformationService(config, services.conversation)
    saver = ThreadedSqliteSaver(config.data_dir / "runtime" / "conversation-v3.sqlite3")
    graph = build_assistant_graph(
        services,
        saver,
        ledger,
        resolver=resolver,
        generators=generators,
        budgets={name: value.budget_seconds for name, value in providers.items()},
    )
    graph.default_provider = config.llm_provider
    # Provider selection changes the resolver's first tier, never the workflow or
    # session namespace. Missing configurations remain available through fallback.
    graphs = {name: graph for name in ("rules", "google", "openai", "openrouter", "local")}
    result = LangGraphOrchestrator(
        graphs,
        gateway,
        store,
        default_provider=config.llm_provider,
        provider_errors=errors,
        ledger=ledger,
        timeout_seconds=config.turn_timeout_seconds,
        profile_id=config.memory_profile_id,
    )
    result.local_semantic_client = providers["local"].client if "local" in providers else None
    result.semantic_providers = {"rules", *providers}
    result.provider_fallbacks = fallback_chains
    return result
