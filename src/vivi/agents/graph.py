from __future__ import annotations

import re
import time
from typing import Any, Literal
from uuid import uuid4

from langchain_core.runnables import RunnableLambda
from langgraph.graph import END, StateGraph
from pydantic import ValidationError

from src.vivi.agents.classifier import RulesIntentClassifier
from src.vivi.agents.contracts import ActionProposal, AssistantOutput, IntentDecision
from src.vivi.agents.state import AgentState
from src.vivi.rag.generator import (
    ExtractiveHandbookGenerator,
    normalize_answer,
    validate_grounding,
)
from src.vivi.rag.runtime import HandbookServices, create_services
from src.vivi.rag.schemas import ModelDecision
from src.vivi.rag.scope import scope_rejection_reason
from src.vivi.vehicle.zones import selected_zones, zoned_noun

ABSTAIN_MESSAGE = "Mình chưa tìm thấy đủ bằng chứng trong cẩm nang VF8 2026 để trả lời câu hỏi này."
_FOLLOW_UP_RE = re.compile(r"\b(vậy|thế|nó|cái đó|việc đó|còn|như vậy)\b", re.IGNORECASE)


class CompiledAssistantGraph:
    """Expose a stable sync/async facade around the compiled LangGraph."""

    def __init__(self, compiled) -> None:
        self.compiled = compiled

    def invoke(self, state: AgentState) -> dict[str, Any]:
        return self.compiled.invoke(state)

    async def ainvoke(self, state: AgentState) -> dict[str, Any]:
        return await self.compiled.ainvoke(state)


def _elapsed(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 2)


def _merge_timing(state: AgentState, stage: str, started: float) -> dict[str, float]:
    return {**state.get("timings", {}), stage: _elapsed(started)}


def _legacy_manual_decision(raw: dict[str, Any]) -> tuple[str, IntentDecision]:
    legacy = ModelDecision.model_validate(raw)
    query = legacy.arguments.query
    return query, IntentDecision(
        route="handbook",
        intent="manual.search",
        arguments={"query": query},
        confidence=legacy.confidence if legacy.confidence is not None else 1.0,
    )


def _validate_action(decision: IntentDecision) -> tuple[ActionProposal | None, str | None]:
    arguments = decision.arguments.model_dump(exclude_none=True)
    if decision.intent in {"window.set_position", "door.set_open", "door.set_lock", "seat.set_heat_level"}:
        try:
            selected_zones(arguments)
        except ValueError as exc:
            return None, str(exc)
    if decision.intent == "climate.set_temperature":
        value = arguments.get("value_celsius")
        if not isinstance(value, (int, float)):
            return None, "Bạn muốn đặt nhiệt độ bao nhiêu?"
        if not 16 <= float(value) <= 30:
            return None, "Nhiệt độ hỗ trợ nằm trong khoảng 16 đến 30 độ C."
    elif decision.intent == "window.set_position":
        position = arguments.get("position_percent")
        if not isinstance(position, (int, float)):
            return None, "Bạn muốn mở hoặc đóng cửa sổ đến mức nào?"
        if not 0 <= float(position) <= 100:
            return None, "Vị trí cửa sổ phải nằm trong khoảng 0 đến 100 phần trăm."
    elif decision.intent == "door.set_open":
        if not isinstance(arguments.get("open"), bool):
            return None, "Bạn muốn mở hay đóng cửa?"
    elif decision.intent == "door.set_lock":
        if not isinstance(arguments.get("locked"), bool):
            return None, "Bạn muốn khóa hay mở khóa cửa?"
    elif decision.intent == "seat.set_heat_level":
        level = arguments.get("level")
        if not isinstance(level, int) or isinstance(level, bool) or not 0 <= level <= 3:
            return None, "Mức sưởi ghế hợp lệ nằm trong khoảng 0 đến 3."
    return ActionProposal(
        intent=decision.intent,
        arguments=arguments,
        confidence=decision.confidence,
    ), None


def _action_preview_text(proposal: ActionProposal) -> str:
    arguments = proposal.arguments
    if proposal.intent == "climate.set_temperature":
        target = float(arguments["value_celsius"])
        request = f"đặt nhiệt độ ở {target:g} độ C"
    elif proposal.intent == "window.set_position":
        position = float(arguments["position_percent"])
        request = f"điều chỉnh {zoned_noun('cửa sổ', arguments)} đến {position:g}%"
    elif proposal.intent == "door.set_open":
        request = f"{'mở' if arguments['open'] else 'đóng'} {zoned_noun('cửa', arguments)}"
    elif proposal.intent == "door.set_lock":
        request = f"{'khóa' if arguments['locked'] else 'mở khóa'} {zoned_noun('cửa', arguments)}"
    elif proposal.intent == "seat.set_heat_level":
        request = f"đặt sưởi {zoned_noun('ghế', arguments)} mức {arguments['level']}"
    elif proposal.intent == "media.play":
        request = "phát nội dung âm thanh"
    elif proposal.intent == "media.pause":
        request = "dừng nội dung âm thanh"
    else:
        request = "kiểm tra trạng thái xe"
    return f"Mình đã hiểu yêu cầu {request}. Thao tác đang chờ kiểm tra an toàn."


def build_graph(services: HandbookServices | None = None):
    runtime = services or create_services()
    classifier = runtime.classifier or RulesIntentClassifier()
    action_gateway = runtime.action_gateway

    def validate_input(state: AgentState) -> dict[str, Any]:
        started = time.perf_counter()
        input_text = str(state.get("input_text") or state.get("query") or "").strip()
        preclassified: IntentDecision | None = None
        raw = state.get("model_input")
        try:
            confirmation_id = state.get("confirmation_id")
            confirmation_decision = state.get("confirmation_decision")
            if bool(confirmation_id) != bool(confirmation_decision):
                raise ValueError("confirmation_id and confirmation_decision must be provided together")
            if raw:
                if raw.get("intent") == "manual.search" and "route" not in raw:
                    input_text, preclassified = _legacy_manual_decision(raw)
                else:
                    preclassified = IntentDecision.model_validate(raw)
                    input_text = input_text or str(preclassified.arguments.query or "").strip()
            if not input_text:
                raise ValueError("input_text is required")
            if len(input_text) > 1000:
                raise ValueError("input_text exceeds 1000 characters")
        except (ValidationError, ValueError, TypeError) as exc:
            message = "Transcript đầu vào không hợp lệ."
            return {
                "session_id": state.get("session_id") or "invalid",
                "turn_id": state.get("turn_id") or str(uuid4()),
                "input_text": input_text,
                "query": input_text,
                "route": "invalid",
                "answer": message,
                "response": message,
                "response_text": message,
                "tts_text": message,
                "status": "invalid_input",
                "grounding_status": "not_applicable",
                "errors": [{"stage": "validate_input", "message": str(exc)}],
                "timings": _merge_timing(state, "validate_input", started),
            }
        metadata = dict(state.get("metadata", {}))
        if preclassified is not None:
            metadata["preclassified_decision"] = preclassified.model_dump(mode="json")
        return {
            "session_id": state.get("session_id") or "default",
            "turn_id": state.get("turn_id") or str(uuid4()),
            "input_text": input_text,
            "query": input_text,
            "vehicle_model": state.get("vehicle_model") or "VF8",
            "model_year": int(state.get("model_year") or 2026),
            "locale": state.get("locale") or "vi_vn",
            "vehicle_state": state.get("vehicle_state"),
            "confirmation_id": state.get("confirmation_id"),
            "confirmation_decision": state.get("confirmation_decision"),
            "status": "validated",
            "grounding_status": "not_applicable",
            "errors": [],
            "metadata": metadata,
            "timings": _merge_timing(state, "validate_input", started),
        }

    def load_history(state: AgentState) -> dict[str, Any]:
        started = time.perf_counter()
        try:
            history = runtime.history.recent(state["session_id"], runtime.history_turns)
        except Exception as exc:
            return {
                "conversation_history": [],
                "errors": [*state.get("errors", []), {"stage": "load_history", "message": str(exc)}],
                "timings": _merge_timing(state, "load_history", started),
            }
        return {
            "conversation_history": history,
            "timings": _merge_timing(state, "load_history", started),
        }

    def observe_vehicle_state(state: AgentState) -> dict[str, Any]:
        started = time.perf_counter()
        if action_gateway is None:
            return {"timings": _merge_timing(state, "observe_vehicle_state", started)}
        try:
            vehicle_state = action_gateway.vehicle.state_for(state["session_id"])
            return {
                "vehicle_state": vehicle_state.model_dump(mode="json"),
                "timings": _merge_timing(state, "observe_vehicle_state", started),
            }
        except Exception as exc:
            return {
                "errors": [
                    *state.get("errors", []),
                    {"stage": "observe_vehicle_state", "message": str(exc)},
                ],
                "timings": _merge_timing(state, "observe_vehicle_state", started),
            }

    async def observe_vehicle_state_async(state: AgentState) -> dict[str, Any]:
        started = time.perf_counter()
        if action_gateway is None:
            return {"timings": _merge_timing(state, "observe_vehicle_state", started)}
        try:
            vehicle_state = await action_gateway.vehicle.get_state(state["session_id"])
            return {
                "vehicle_state": vehicle_state.model_dump(mode="json"),
                "timings": _merge_timing(state, "observe_vehicle_state", started),
            }
        except Exception as exc:
            return {
                "errors": [
                    *state.get("errors", []),
                    {"stage": "observe_vehicle_state", "message": str(exc)},
                ],
                "timings": _merge_timing(state, "observe_vehicle_state", started),
            }

    def classify_intent(state: AgentState) -> dict[str, Any]:
        started = time.perf_counter()
        try:
            preclassified = state.get("metadata", {}).get("preclassified_decision")
            decision = (
                IntentDecision.model_validate(preclassified)
                if preclassified is not None
                else classifier.classify_with_context(
                    state["input_text"],
                    state.get("conversation_history", []),
                    state.get("vehicle_state"),
                )
            )
        except Exception as exc:
            if isinstance(classifier, RulesIntentClassifier):
                message = "Mình chưa thể hiểu yêu cầu lúc này. Bạn thử nói lại ngắn gọn nhé."
                return {
                    "route": "clarify",
                    "intent": "conversation.clarify",
                    "answer": message,
                    "response": message,
                    "response_text": message,
                    "tts_text": message,
                    "status": "classification_error",
                    "errors": [
                        *state.get("errors", []),
                        {"stage": "classify_intent", "message": str(exc)},
                    ],
                    "timings": _merge_timing(state, "classify_intent", started),
                }
            decision = RulesIntentClassifier().classify_with_context(
                state["input_text"],
                state.get("conversation_history", []),
                state.get("vehicle_state"),
            )
            errors = [
                *state.get("errors", []),
                {"stage": "classify_intent_fallback", "message": str(exc)},
            ]
        else:
            errors = state.get("errors", [])
        return {
            "decision": decision.model_dump(mode="json"),
            "route": decision.route,
            "intent": decision.intent,
            "status": "classified",
            "errors": errors,
            "timings": _merge_timing(state, "classify_intent", started),
        }

    async def classify_intent_async(state: AgentState) -> dict[str, Any]:
        started = time.perf_counter()
        try:
            preclassified = state.get("metadata", {}).get("preclassified_decision")
            decision = (
                IntentDecision.model_validate(preclassified)
                if preclassified is not None
                else await classifier.aclassify_with_context(
                    state["input_text"],
                    state.get("conversation_history", []),
                    state.get("vehicle_state"),
                )
            )
        except Exception as exc:
            if isinstance(classifier, RulesIntentClassifier):
                message = "Mình chưa thể hiểu yêu cầu lúc này. Bạn thử nói lại ngắn gọn nhé."
                return {
                    "route": "clarify",
                    "intent": "conversation.clarify",
                    "answer": message,
                    "response": message,
                    "response_text": message,
                    "tts_text": message,
                    "status": "classification_error",
                    "errors": [
                        *state.get("errors", []),
                        {"stage": "classify_intent", "message": str(exc)},
                    ],
                    "timings": _merge_timing(state, "classify_intent", started),
                }
            decision = RulesIntentClassifier().classify_with_context(
                state["input_text"],
                state.get("conversation_history", []),
                state.get("vehicle_state"),
            )
            errors = [
                *state.get("errors", []),
                {"stage": "classify_intent_fallback", "message": str(exc)},
            ]
        else:
            errors = state.get("errors", [])
        return {
            "decision": decision.model_dump(mode="json"),
            "route": decision.route,
            "intent": decision.intent,
            "status": "classified",
            "errors": errors,
            "timings": _merge_timing(state, "classify_intent", started),
        }

    def resolve_confirmation(state: AgentState) -> dict[str, Any]:
        started = time.perf_counter()
        if action_gateway is None:
            message = "Runtime hiện tại chưa cấu hình safety gateway."
            return {
                "route": "unsupported",
                "status": "execution_unavailable",
                "answer": message,
                "response": message,
                "response_text": message,
                "tts_text": message,
                "requires_execution": False,
                "timings": _merge_timing(state, "resolve_confirmation", started),
            }
        proposal, decision = action_gateway.resolve_confirmation(
            state["session_id"],
            state["turn_id"],
            state["confirmation_id"],
            state["confirmation_decision"],
        )
        execution = {
            "allowed": decision.allowed,
            "executed": False,
            "verified": False,
            "risk_class": decision.risk_class,
            "message": decision.message,
        }
        if proposal is None or not decision.allowed:
            message = decision.message or "Xác nhận không hợp lệ."
            return {
                "route": "action",
                "status": decision.status,
                "answer": message,
                "response": message,
                "response_text": message,
                "tts_text": message,
                "action_proposal": None,
                "requires_execution": False,
                "execution": execution,
                "risk_class": decision.risk_class,
                "timings": _merge_timing(state, "resolve_confirmation", started),
            }
        return {
            "route": "action",
            "intent": proposal.intent,
            "status": "action_allowed",
            "action_proposal": proposal.model_dump(mode="json"),
            "requires_execution": True,
            "execution": execution,
            "risk_class": decision.risk_class,
            "timings": _merge_timing(state, "resolve_confirmation", started),
        }

    def scope_guard(state: AgentState) -> dict[str, Any]:
        started = time.perf_counter()
        reason = scope_rejection_reason(state["query"])
        if reason and state.get("conversation_history") and _FOLLOW_UP_RE.search(state["query"]):
            reason = None
        if reason:
            return {
                "answer": reason,
                "response": reason,
                "response_text": reason,
                "tts_text": reason,
                "status": "out_of_scope",
                "grounding_status": "unsupported",
                "abstain_reason": reason,
                "timings": _merge_timing(state, "scope_guard", started),
            }
        return {
            "grounding_status": "pending",
            "timings": _merge_timing(state, "scope_guard", started),
        }

    def retrieve(state: AgentState) -> dict[str, Any]:
        started = time.perf_counter()
        retrieval_query = state["query"]
        history = state.get("conversation_history", [])
        if history and _FOLLOW_UP_RE.search(state["query"]):
            retrieval_query = f"{history[-1]['query']}\n{state['query']}"
        try:
            chunks = runtime.retriever.retrieve(
                retrieval_query, state["vehicle_model"], state["model_year"], state["locale"]
            )
            values = [chunk.model_dump(mode="json") for chunk in chunks]
            return {
                "retrieval_query": retrieval_query,
                "retrieved_chunks": values,
                "accepted_chunks": values,
                "timings": _merge_timing(state, "retrieve", started),
            }
        except Exception as exc:
            message = "Không thể truy xuất chỉ mục cẩm nang lúc này."
            return {
                "answer": message,
                "response": message,
                "response_text": message,
                "tts_text": message,
                "status": "retrieval_error",
                "grounding_status": "unsupported",
                "abstain_reason": message,
                "errors": [*state.get("errors", []), {"stage": "retrieve", "message": str(exc)}],
                "timings": _merge_timing(state, "retrieve", started),
            }

    def evidence_gate(state: AgentState) -> dict[str, Any]:
        started = time.perf_counter()
        if state.get("accepted_chunks"):
            return {"timings": _merge_timing(state, "evidence_gate", started)}
        reason = state.get("abstain_reason") or ABSTAIN_MESSAGE
        return {
            "answer": reason,
            "response": reason,
            "response_text": reason,
            "tts_text": reason,
            "status": state.get("status") if state.get("status") == "retrieval_error" else "insufficient_evidence",
            "grounding_status": "unsupported",
            "abstain_reason": reason,
            "timings": _merge_timing(state, "evidence_gate", started),
        }

    def generate_handbook_answer(state: AgentState) -> dict[str, Any]:
        from src.vivi.rag.schemas import RetrievedChunk

        started = time.perf_counter()
        chunks = [RetrievedChunk.model_validate(value) for value in state["accepted_chunks"]]
        try:
            result = runtime.generator.generate(
                state["query"], chunks, state.get("conversation_history", [])
            )
        except Exception as exc:
            if isinstance(runtime.generator, ExtractiveHandbookGenerator):
                message = "Không thể tạo câu trả lời từ bằng chứng lúc này."
                return {
                    "answer": message,
                    "response": message,
                    "response_text": message,
                    "tts_text": message,
                    "status": "generation_error",
                    "grounding_status": "unsupported",
                    "errors": [
                        *state.get("errors", []),
                        {"stage": "generate", "message": str(exc)},
                    ],
                    "timings": _merge_timing(state, "generate", started),
                }
            result = ExtractiveHandbookGenerator().generate(
                state["query"], chunks, state.get("conversation_history", [])
            )
            errors = [
                *state.get("errors", []),
                {"stage": "generate_fallback", "message": str(exc)},
            ]
        else:
            errors = state.get("errors", [])
        return {
            "metadata": {**state.get("metadata", {}), "generated_answer": result.model_dump(mode="json")},
            "errors": errors,
            "timings": _merge_timing(state, "generate", started),
        }

    async def generate_handbook_answer_async(state: AgentState) -> dict[str, Any]:
        from src.vivi.rag.schemas import RetrievedChunk

        started = time.perf_counter()
        chunks = [RetrievedChunk.model_validate(value) for value in state["accepted_chunks"]]
        try:
            async_generate = getattr(runtime.generator, "agenerate", None)
            result = (
                await async_generate(
                    state["query"], chunks, state.get("conversation_history", [])
                )
                if async_generate is not None
                else runtime.generator.generate(
                    state["query"], chunks, state.get("conversation_history", [])
                )
            )
        except Exception as exc:
            if isinstance(runtime.generator, ExtractiveHandbookGenerator):
                message = "Không thể tạo câu trả lời từ bằng chứng lúc này."
                return {
                    "answer": message,
                    "response": message,
                    "response_text": message,
                    "tts_text": message,
                    "status": "generation_error",
                    "grounding_status": "unsupported",
                    "errors": [
                        *state.get("errors", []),
                        {"stage": "generate", "message": str(exc)},
                    ],
                    "timings": _merge_timing(state, "generate", started),
                }
            result = ExtractiveHandbookGenerator().generate(
                state["query"], chunks, state.get("conversation_history", [])
            )
            errors = [
                *state.get("errors", []),
                {"stage": "generate_fallback", "message": str(exc)},
            ]
        else:
            errors = state.get("errors", [])
        return {
            "metadata": {
                **state.get("metadata", {}),
                "generated_answer": result.model_dump(mode="json"),
            },
            "errors": errors,
            "timings": _merge_timing(state, "generate", started),
        }

    def validate_citations(state: AgentState) -> dict[str, Any]:
        from src.vivi.rag.schemas import GroundedAnswer, RetrievedChunk

        started = time.perf_counter()
        payload = state.get("metadata", {}).get("generated_answer")
        if payload is None:
            return {"timings": _merge_timing(state, "validate_citations", started)}
        chunks = [RetrievedChunk.model_validate(value) for value in state["accepted_chunks"]]
        answer = normalize_answer(GroundedAnswer.model_validate(payload), chunks)
        valid, reason = validate_grounding(answer, chunks)
        if not valid:
            message = reason or ABSTAIN_MESSAGE
            return {
                "answer": message,
                "response": message,
                "response_text": message,
                "tts_text": message,
                "citations": [],
                "status": "insufficient_evidence",
                "grounding_status": "unsupported",
                "abstain_reason": message,
                "timings": _merge_timing(state, "validate_citations", started),
            }
        by_id = {chunk.source_id: chunk for chunk in chunks}
        cited_ids: list[str] = []
        for claim in answer.claims:
            for source_id in claim.source_ids:
                if source_id not in cited_ids:
                    cited_ids.append(source_id)
        citations = [
            {
                "source_id": source_id,
                "title": by_id[source_id].chapter,
                "section_path": by_id[source_id].section_path,
                "source_url": by_id[source_id].source_url,
            }
            for source_id in cited_ids
        ]
        return {
            "answer": answer.answer,
            "response": answer.answer,
            "response_text": answer.answer,
            "tts_text": answer.answer,
            "citations": citations,
            "status": "answered",
            "grounding_status": "supported",
            "abstain_reason": None,
            "timings": _merge_timing(state, "validate_citations", started),
        }

    def validate_action(state: AgentState) -> dict[str, Any]:
        started = time.perf_counter()
        decision = IntentDecision.model_validate(state["decision"])
        if decision.confidence < 0.7:
            clarification = decision.clarification_question or (
                "Mình chưa chắc về lệnh điều khiển này. Bạn có thể nói rõ hơn không?"
            )
            return {
                "route": "clarify",
                "intent": "conversation.clarify",
                "answer": clarification,
                "response": clarification,
                "response_text": clarification,
                "tts_text": clarification,
                "status": "clarify",
                "action_proposal": None,
                "requires_execution": False,
                "timings": _merge_timing(state, "validate_action", started),
            }
        proposal, clarification = _validate_action(decision)
        if clarification:
            return {
                "route": "clarify",
                "intent": "conversation.clarify",
                "answer": clarification,
                "response": clarification,
                "response_text": clarification,
                "tts_text": clarification,
                "status": "clarify",
                "action_proposal": None,
                "requires_execution": False,
                "timings": _merge_timing(state, "validate_action", started),
            }
        if proposal is None:
            raise RuntimeError("validated action did not produce a proposal")
        response_text = _action_preview_text(proposal)
        return {
            "answer": response_text,
            "response": response_text,
            "response_text": response_text,
            "tts_text": response_text,
            "status": "action_proposed",
            "action_proposal": proposal.model_dump(mode="json") if proposal else None,
            "requires_execution": proposal is not None,
            "timings": _merge_timing(state, "validate_action", started),
        }

    def safety_check(state: AgentState) -> dict[str, Any]:
        started = time.perf_counter()
        proposal = ActionProposal.model_validate(state["action_proposal"])
        if action_gateway is None:
            return {"timings": _merge_timing(state, "safety_check", started)}
        decision = action_gateway.check(
            state["session_id"],
            proposal,
            state.get("vehicle_state"),
            turn_id=state["turn_id"],
        )
        execution = {
            "allowed": decision.allowed,
            "executed": False,
            "verified": False,
            "risk_class": decision.risk_class,
            "message": decision.message,
        }
        if decision.allowed:
            return {
                "status": "action_allowed",
                "execution": execution,
                "risk_class": decision.risk_class,
                "timings": _merge_timing(state, "safety_check", started),
            }
        message = decision.message or "Safety gateway đã từ chối thao tác."
        return {
            "answer": message,
            "response": message,
            "response_text": message,
            "tts_text": message,
            "status": decision.status,
            "requires_execution": False,
            "execution": execution,
            "confirmation": decision.confirmation,
            "risk_class": decision.risk_class,
            "timings": _merge_timing(state, "safety_check", started),
        }

    def execute_action(state: AgentState) -> dict[str, Any]:
        started = time.perf_counter()
        if action_gateway is None:
            return {"timings": _merge_timing(state, "execute_action", started)}
        proposal = ActionProposal.model_validate(state["action_proposal"])
        result = action_gateway.execute(state["session_id"], state["turn_id"], proposal)
        return {
            "metadata": {
                **state.get("metadata", {}),
                "gateway_execution": {
                    "executed": result.executed,
                    "verified": result.verified,
                    "message": result.message,
                    "vehicle_state": result.vehicle_state,
                    "error": result.error,
                },
            },
            "timings": _merge_timing(state, "execute_action", started),
        }

    def verify_action(state: AgentState) -> dict[str, Any]:
        started = time.perf_counter()
        result = state.get("metadata", {}).get("gateway_execution") or {}
        verified = bool(result.get("executed") and result.get("verified"))
        message = result.get("message") or "Chưa thể xác minh thao tác."
        execution = {
            "allowed": True,
            "executed": bool(result.get("executed")),
            "verified": bool(result.get("verified")),
            "risk_class": state.get("risk_class"),
            "message": message,
            "error": result.get("error"),
        }
        updates: dict[str, Any] = {
            "answer": message,
            "response": message,
            "response_text": message,
            "tts_text": message,
            "status": "action_verified" if verified else "action_unverified",
            "requires_execution": False,
            "execution": execution,
            "vehicle_state": result.get("vehicle_state"),
            "timings": _merge_timing(state, "verify_action", started),
        }
        if result.get("error"):
            updates["errors"] = [
                *state.get("errors", []),
                {"stage": "execute_action", "message": str(result["error"])},
            ]
        return updates

    def compose_decision_response(state: AgentState) -> dict[str, Any]:
        started = time.perf_counter()
        decision = IntentDecision.model_validate(state["decision"])
        text = decision.clarification_question or decision.response_text
        if not text:
            text = "Mình chưa hỗ trợ yêu cầu này."
        statuses = {
            "conversation": "answered",
            "clarify": "clarify",
            "unsupported": "unsupported",
        }
        return {
            "answer": text,
            "response": text,
            "response_text": text,
            "tts_text": text,
            "status": statuses[decision.route],
            "action_proposal": None,
            "requires_execution": False,
            "timings": _merge_timing(state, "compose_decision_response", started),
        }

    def compose_output(state: AgentState) -> dict[str, Any]:
        started = time.perf_counter()
        text = state.get("response_text") or state.get("response") or "Mình chưa thể xử lý yêu cầu này."
        payload = AssistantOutput(
            session_id=state.get("session_id", "invalid"),
            turn_id=state.get("turn_id", "invalid"),
            route=state.get("route", "invalid"),
            intent=state.get("intent"),
            status=state.get("status", "error"),
            response_text=text,
            tts_text=state.get("tts_text") or text,
            action_proposal=state.get("action_proposal"),
            requires_execution=state.get("requires_execution", False),
            execution=state.get("execution"),
            confirmation=state.get("confirmation"),
            vehicle_state=state.get("vehicle_state"),
            citations=state.get("citations", []),
            grounding_status=state.get("grounding_status", "not_applicable"),
            errors=state.get("errors", []),
            timings=_merge_timing(state, "compose_output", started),
        )
        return {
            "answer": payload.response_text,
            "response": payload.response_text,
            "response_text": payload.response_text,
            "tts_text": payload.tts_text,
            "output": payload.model_dump(mode="json"),
            "timings": payload.timings,
        }

    def persist(state: AgentState) -> dict[str, Any]:
        started = time.perf_counter()
        try:
            runtime.history.save(state)
        except Exception as exc:
            return {
                "errors": [*state.get("errors", []), {"stage": "persist", "message": str(exc)}],
                "timings": _merge_timing(state, "persist", started),
            }
        return {"timings": _merge_timing(state, "persist", started)}

    def after_validation(state: AgentState) -> Literal["load_history", "persist"]:
        return "persist" if state.get("status") == "invalid_input" else "load_history"

    def after_classification(
        state: AgentState,
    ) -> Literal["scope_guard", "validate_action", "compose_decision_response", "persist"]:
        if state.get("status") == "classification_error":
            return "persist"
        route = state.get("route")
        if route == "handbook":
            return "scope_guard"
        if route == "action":
            return "validate_action"
        return "compose_decision_response"

    def after_action_validation(state: AgentState) -> Literal["safety_check", "persist"]:
        if state.get("status") != "action_proposed" or action_gateway is None:
            return "persist"
        return "safety_check"

    def after_safety(state: AgentState) -> Literal["execute_action", "persist"]:
        return "execute_action" if state.get("status") == "action_allowed" else "persist"

    def after_confirmation(state: AgentState) -> Literal["execute_action", "persist"]:
        return "execute_action" if state.get("status") == "action_allowed" else "persist"

    def after_scope(state: AgentState) -> Literal["retrieve", "persist"]:
        return "persist" if state.get("status") == "out_of_scope" else "retrieve"

    def after_evidence(state: AgentState) -> Literal["generate_handbook_answer", "persist"]:
        return "generate_handbook_answer" if state.get("accepted_chunks") else "persist"

    def runnable(func, afunc=None):
        async def default_async_func(state: AgentState) -> Any:
            # SQLite connections and the in-memory vehicle gateway are short,
            # synchronous critical sections. Running them through the default
            # executor can strand LangGraph's async runner during loop wake-up;
            # providing an explicit coroutine keeps the async graph deterministic.
            return func(state)

        return RunnableLambda(func, afunc=afunc or default_async_func, name=func.__name__)

    graph = StateGraph(AgentState)
    graph.add_node("validate_input", runnable(validate_input))
    graph.add_node("load_history", runnable(load_history))
    graph.add_node(
        "observe_vehicle_state",
        runnable(observe_vehicle_state, observe_vehicle_state_async),
    )
    graph.add_node("classify_intent", runnable(classify_intent, classify_intent_async))
    graph.add_node("resolve_confirmation", runnable(resolve_confirmation))
    graph.add_node("scope_guard", runnable(scope_guard))
    graph.add_node("retrieve", runnable(retrieve))
    graph.add_node("evidence_gate", runnable(evidence_gate))
    graph.add_node(
        "generate_handbook_answer",
        runnable(generate_handbook_answer, generate_handbook_answer_async),
    )
    graph.add_node("validate_citations", runnable(validate_citations))
    graph.add_node("validate_action", runnable(validate_action))
    graph.add_node("safety_check", runnable(safety_check))
    graph.add_node("execute_action", runnable(execute_action))
    graph.add_node("verify_action", runnable(verify_action))
    graph.add_node("compose_decision_response", runnable(compose_decision_response))
    graph.add_node("compose_output", runnable(compose_output))
    graph.add_node("persist", runnable(persist))
    graph.set_entry_point("validate_input")
    graph.add_conditional_edges("validate_input", runnable(after_validation))
    graph.add_conditional_edges(
        "load_history",
        runnable(
            lambda state: "resolve_confirmation"
            if state.get("confirmation_id")
            else "observe_vehicle_state"
        ),
    )
    graph.add_edge("observe_vehicle_state", "classify_intent")
    graph.add_conditional_edges("resolve_confirmation", runnable(after_confirmation))
    graph.add_conditional_edges("classify_intent", runnable(after_classification))
    graph.add_conditional_edges("scope_guard", runnable(after_scope))
    graph.add_edge("retrieve", "evidence_gate")
    graph.add_conditional_edges("evidence_gate", runnable(after_evidence))
    graph.add_edge("generate_handbook_answer", "validate_citations")
    graph.add_edge("validate_citations", "persist")
    graph.add_conditional_edges("validate_action", runnable(after_action_validation))
    graph.add_conditional_edges("safety_check", runnable(after_safety))
    graph.add_edge("execute_action", "verify_action")
    graph.add_edge("verify_action", "persist")
    graph.add_edge("compose_decision_response", "persist")
    graph.add_edge("persist", "compose_output")
    graph.add_edge("compose_output", END)
    return CompiledAssistantGraph(graph.compile())


class _CompatibilityRetriever:
    def retrieve(self, *args, **kwargs):
        return []


class _CompatibilityGenerator:
    def generate(self, *args, **kwargs):
        raise RuntimeError("handbook runtime is available through src.vivi.cli.ask")


class _CompatibilityHistory:
    def recent(self, session_id: str, limit: int = 6):
        return []

    def save(self, state: dict[str, Any]) -> None:
        return None


agent = build_graph(
    HandbookServices(
        retriever=_CompatibilityRetriever(),  # type: ignore[arg-type]
        generator=_CompatibilityGenerator(),  # type: ignore[arg-type]
        history=_CompatibilityHistory(),  # type: ignore[arg-type]
        classifier=RulesIntentClassifier(),
    )
)
