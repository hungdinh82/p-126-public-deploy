"""One checkpoint-owned conversation workflow, independent of model provider."""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime
from typing import TypedDict
from uuid import uuid4

from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from src.vivi.agents.contracts import AssistantOutput
from src.vivi.capabilities.execution import CapabilityRuntime
from src.vivi.capabilities.registry import REGISTRY, catalog
from src.vivi.dialogue.contracts import SemanticTurn, SessionState, TurnEvent
from src.vivi.dialogue.planner import PROMPT_VERSION, SemanticResolver
from src.vivi.dialogue.reducer import (
    expire,
    prepare_step,
    record_navigation,
    record_step,
    record_trip_plan,
    reduce_turn,
    upgrade_contract,
)
from src.vivi.navigation.presentation import result_messages
from src.vivi.navigation.trips import TripWorkflow
from src.vivi.navigation.workflow import NavigationWorkflow
from src.vivi.persistence.runtime_store import fingerprint
from src.vivi.turn_control import current_control


class DialogueState(TypedDict, total=False):
    event: dict
    session: dict
    turn: dict
    output: dict


class AssistantGraph:
    def __init__(self, compiled, saver, profile):
        self.compiled, self.saver, self.profile = compiled, saver, profile
        self.default_provider = "rules"

    def event(self, state):
        if "text" in state:
            return TurnEvent.model_validate(state).model_dump(mode="json")
        return TurnEvent(
            session_id=state.get("session_id", "cli"),
            turn_id=state.get("turn_id", str(uuid4())),
            text=state.get("input_text", state.get("query", "")),
            provider=state.get("provider", self.default_provider),
            kind="navigation"
            if state.get("navigation_event")
            else "confirmation"
            if state.get("confirmation_id")
            else "action"
            if state.get("action")
            else "utterance",
            confirmation_id=state.get("confirmation_id"),
            confirmation_decision=state.get("confirmation_decision"),
            action=state.get("action"),
            navigation_event=state.get("navigation_event"),
            position=state.get("position"),
            vehicle_model=state.get("vehicle_model", "VF8"),
            model_year=state.get("model_year", 2026),
            locale=state.get("locale", "vi_vn"),
        ).model_dump(mode="json")

    def config(self, event):
        identifier = fingerprint({"profile": self.profile, "session": event["session_id"]})
        return {"configurable": {"thread_id": "conversation:3:" + identifier}, "recursion_limit": 60}

    async def input(self, event, config):
        snapshot = await self.compiled.aget_state(config)
        return Command(resume=event) if snapshot.next == ("await_input",) else {"event": event}

    def invoke(self, state):
        return asyncio.run(self.ainvoke(state))

    async def ainvoke(self, state):
        event = self.event(state)
        config = self.config(event)
        snapshot = await self.compiled.aget_state(config)
        old = snapshot.values.get("event", {})
        if (
            old.get("turn_id") == event["turn_id"]
            and snapshot.values.get("output", {}).get("turn_id") == event["turn_id"]
        ):
            if old != event:
                raise ValueError("turn_id đã được dùng cho một yêu cầu khác.")
            return self.project(snapshot.values)
        return self.project(await self.compiled.ainvoke(await self.input(event, config), config, durability="sync"))

    async def astream(self, state, stream_mode):
        event = self.event(state)
        config = self.config(event)
        async for item in self.compiled.astream(
            await self.input(event, config), config, stream_mode=stream_mode, durability="sync"
        ):
            yield item

    @staticmethod
    def project(state):
        output, turn = state.get("output", {}), state.get("turn", {})
        return {
            **state,
            **output,
            "output": output,
            "answer": output.get("response_text"),
            "response": output.get("response_text"),
            "query": state.get("event", {}).get("text"),
            "conversation_history": turn.get("history", []),
            "memory_context": turn.get("memory", []),
        }

    def close(self):
        self.saver.close()


def build_assistant_graph(services, saver, store=None, provider=None, *, resolver=None, generators=None, budgets=None):
    resolver = resolver or SemanticResolver()
    runtime = CapabilityRuntime(services, store, generators, budgets, resolver.breaker_seconds,
                                provider_chains=resolver.fallback_chains)
    navigation = NavigationWorkflow(services, store, resolver)
    trips = TripWorkflow(services, store, resolver)
    profile = services.memory_profile_id

    async def context(state):
        event = TurnEvent.model_validate(state["event"])
        previous = SessionState.model_validate(state.get("session") or {})
        session = expire(previous)
        session = upgrade_contract(session)
        expired_id = None
        if previous.active and session.active is None:
            index = previous.active.current_index()
            grant = previous.active.steps[index].confirmation if index is not None else None
            expired_id = grant["confirmation_id"] if grant else None
        if store and session.active:
            for i, step in enumerate(session.active.steps):
                if step.status not in {"running", "unknown"} or not step.command_id or step.resolved_arguments is None:
                    continue
                known = await asyncio.to_thread(store.command_result, event.session_id, step.command_id)
                args = dict(step.resolved_arguments)
                if step.draft.capability == "vehicle.get_status":
                    args.pop("query", None)
                if known and known["fingerprint"] == fingerprint({"intent": step.draft.capability, "arguments": args}):
                    result = known["result"]
                    if result.get("verified"):
                        projection = (
                            result
                            if result.get("status")
                            else {
                                "status": "completed",
                                "message": result["message"],
                                "vehicle_state": result["vehicle_state"],
                                "action": {"intent": step.draft.capability, "arguments": args},
                                "execution": {
                                    "allowed": True,
                                    **{k: v for k, v in result.items() if k != "vehicle_state"},
                                },
                            }
                        )
                        session = record_step(
                            session, i, status="completed", result=projection, command_id=step.command_id
                        )
                        if projection.get("navigation_update"):
                            session = record_navigation(session, projection["navigation_update"])
                        if projection.get("trip_plan_update"):
                            session = record_trip_plan(session, projection["trip_plan_update"])
        history_session = event.session_id if profile == "default" else f"profile:{profile}:{event.session_id}"
        history = await asyncio.to_thread(services.history.recent, history_session, services.history_turns)
        memory, errors = [], []
        if services.memory:
            try:
                memory = await asyncio.to_thread(
                    services.memory.context,
                    profile,
                    event.text,
                    limit=services.memory_context_limit,
                    max_characters=services.memory_context_max_characters,
                )
            except Exception as exc:
                errors.append({"stage": "load_memory", "message": str(exc)})
        return {
            "session": session.model_dump(mode="json"),
            "turn": {
                "history": history,
                "memory": memory,
                "history_session": history_session,
                "expired_confirmation": expired_id,
                "errors": errors,
                "timings": {},
                "spans": [],
                "results": [],
                "message": None,
                "effective_provider": "rules",
            },
        }

    async def interpret(state):
        event = TurnEvent.model_validate(state["event"])
        session = SessionState.model_validate(state["session"])
        turn = dict(state["turn"])
        if event.kind == "navigation":
            result = await navigation.event(event, session)
            turn["navigation_result"] = result
            semantic, served, attempts = SemanticTurn(act="reply", text=result.get("message", "")), "event", []
            if result.get("request_action"):
                semantic = SemanticTurn(act="request", actions=[result["request_action"]])
            task = session.active
            index = task.current_index() if task else None
            if (
                event.navigation_event.get("kind") == "position"
                and index is not None
                and task.steps[index].draft.capability.startswith(("navigation.", "trip."))
                and task.steps[index].status == "waiting_slot"
                and result.get("navigation_update")
            ):
                semantic = SemanticTurn(act="resume", task_ref="active")
        elif event.kind == "confirmation":
            task = session.active
            index = task.current_index() if task else None
            grant = task.steps[index].confirmation if index is not None else None
            if grant is None or event.confirmation_id != grant["confirmation_id"]:
                semantic = SemanticTurn(
                    act="clarify", text="Xác nhận không hợp lệ hoặc không còn thuộc yêu cầu hiện tại."
                )
            elif event.confirmation_decision == "approve" and datetime.fromisoformat(
                grant["expires_at"]
            ) <= datetime.now(UTC):
                semantic = SemanticTurn(
                    act="clarify",
                    text="Xác nhận đã hết hạn. Yêu cầu vẫn được giữ; bạn xác nhận lại để xem đề nghị mới nhé.",
                )
                turn["expired_confirmation"] = grant["confirmation_id"]
            elif event.confirmation_decision not in {"approve", "deny"}:
                semantic = SemanticTurn(act="clarify", text="Bạn chọn xác nhận hoặc huỷ thao tác nhé.")
            else:
                semantic = SemanticTurn(
                    act="confirm" if event.confirmation_decision == "approve" else "deny", task_ref="active"
                )
            served, attempts = "event", []
        elif event.kind == "action" and event.action:
            semantic, served, attempts = SemanticTurn(act="request", actions=[event.action]), "event", []
        else:
            semantic, served, attempts = await resolver.resolve(
                event.text,
                session,
                requested=event.provider,
                history=turn["history"],
                memory=turn["memory"],
                catalog=catalog(services.navigation is not None),
            )
        # Domain invalidity is a clarification, never a reason to ask a weaker model.
        if any(action.capability not in REGISTRY for action in semantic.drafts()):
            semantic = SemanticTurn(act="unsupported", text="Mình chưa hỗ trợ thao tác đó.")
        turn.update(semantic=semantic.model_dump(mode="json"), effective_provider=served, provider_attempts=attempts)
        return {"turn": turn}

    async def transition(state):
        previous = SessionState.model_validate(state["session"])
        semantic = SemanticTurn.model_validate(state["turn"]["semantic"])
        session, message = reduce_turn(
            previous, semantic, revoke_consent_on_clarify=state["event"]["kind"] == "utterance"
        )
        if state["turn"].get("navigation_result"):
            update = state["turn"]["navigation_result"]
            if update.get("navigation_update"):
                session = record_navigation(session, update["navigation_update"])
        if semantic.act == "clarify" and session.clarification:
            session.clarification.source_text = (
                previous.clarification.source_text if previous.clarification else state["event"]["text"]
            )
        if (
            session.active
            and semantic.act in {"request", "question", "offer"}
            and (not previous.active or session.active.id != previous.active.id)
        ):
            session.active.source_text = state["event"]["text"]
        old, new = previous.active, session.active
        if old and (
            new is None or old.id != new.id or old.revision != new.revision or semantic.act in {"cancel", "deny"}
        ):
            await asyncio.to_thread(services.action_gateway.confirmations.cancel, state["event"]["session_id"])
        turn = {
            **state["turn"],
            "message": message,
            "can_schedule": message is None
            and semantic.act in {"request", "question", "modify", "confirm", "deny", "resume"},
        }
        return {"session": session.model_dump(mode="json"), "turn": turn}

    async def schedule(state):
        session = SessionState.model_validate(state["session"])
        turn = dict(state["turn"])
        index = session.active.current_index() if session.active else None
        control = current_control.get()
        if control and control.cancelled.is_set() and session.active:
            session, message = reduce_turn(session, SemanticTurn(act="cancel", task_ref="active"))
            turn["can_schedule"], turn["message"] = False, message
        turn["index"] = index
        turn["dispatch"] = bool(
            turn["can_schedule"] and index is not None and session.active.steps[index].status in {"pending", "approved"}
        )
        return {"turn": turn, "session": session.model_dump(mode="json")}

    async def validate(state):
        event = TurnEvent.model_validate(state["event"])
        session = SessionState.model_validate(state["session"])
        turn = dict(state["turn"])
        index, task = turn["index"], session.active
        step = task.steps[index]
        spec = REGISTRY[step.draft.capability]
        command_id = step.command_id or f"task:{task.id}:r{task.revision}:step:{step.id}"
        try:
            observed = (
                await services.action_gateway.vehicle.get_state(event.session_id)
                if spec.executor == "vehicle"
                else None
            )
            arguments, question = spec.validate(step.draft, observed)
        except Exception as exc:
            result = {"status": "unknown", "message": "Mình chưa đọc được trạng thái cần thiết để thực hiện bước này."}
            session = record_step(session, index, status="unknown", result=result)
            turn["results"] = [
                *turn["results"],
                {"step_id": step.id, "index": index, "capability": step.draft.capability, **result},
            ]
            turn["errors"] = [*turn["errors"], {"stage": "validate", "message": str(exc)}]
            turn["can_schedule"], turn["dispatch"] = False, False
            return {"session": session.model_dump(mode="json"), "turn": turn}
        if step.resolved_arguments is not None:
            arguments = step.resolved_arguments
        if question:
            result = {"status": "waiting_slot", "message": question}
            if (
                spec.executor == "vehicle"
                and step.draft.capability in {"door.set_open", "hood.set_open", "trunk.set_open", "body.set_open"}
                and step.draft.arguments.open
                and observed
                and observed.driving
            ):
                result = {"status": "blocked", "message": "Không thể mở cửa khi xe đang ở chế độ lái."}
            session = record_step(session, index, status=result["status"], result=result)
            session.active.steps[index].missing_slots = [
                key
                for key, field in spec.spec.parameters.model_fields.items()
                if field.is_required() and key not in step.draft.arguments.model_dump(exclude_none=True)
            ]
            turn["results"] = [
                *turn["results"],
                {"step_id": step.id, "index": index, "capability": step.draft.capability, **result},
            ]
            turn["can_schedule"], turn["dispatch"] = False, False
        else:
            session = prepare_step(session, index, arguments, command_id)
            if store:
                await asyncio.to_thread(store.link_command, event.session_id, event.turn_id, command_id)
        return {"session": session.model_dump(mode="json"), "turn": turn}

    async def capability(state):
        event = TurnEvent.model_validate(state["event"])
        session = SessionState.model_validate(state["session"])
        turn = dict(state["turn"])
        index, task = turn["index"], session.active
        step = task.steps[index]
        spec = REGISTRY[step.draft.capability]
        command_id = step.command_id or f"task:{task.id}:r{task.revision}:step:{step.id}"
        try:
            arguments = dict(step.resolved_arguments)
            if spec.executor == "vehicle":
                result = await runtime.vehicle(event, step, arguments, command_id)
            elif spec.executor == "memory":
                result = await runtime.memory(event, step, arguments, command_id)
            elif spec.executor == "handbook":
                result = await runtime.handbook(event, arguments, turn["effective_provider"], turn["history"])
            else:
                workflow = trips if step.draft.capability.startswith("trip.") else navigation
                result = await workflow.execute(event, session, step, arguments, command_id, turn["effective_provider"])
        except Exception as exc:
            result = {
                "status": "unknown",
                "message": "Mình chưa xác minh được kết quả bước này. Bạn kiểm tra trước khi yêu cầu lại nhé.",
            }
            turn["errors"] = [*turn["errors"], {"stage": "capability", "message": str(exc)}]
        session = record_step(
            session,
            index,
            status=result["status"],
            result=result,
            confirmation=result.get("confirmation"),
            command_id=command_id if result["status"] != "waiting_slot" else None,
        )
        if result.get("navigation_update"):
            session = record_navigation(session, result["navigation_update"])
        if result.get("trip_plan_update"):
            session = record_trip_plan(session, result["trip_plan_update"])
        turn["results"] = [
            *turn["results"],
            {"step_id": step.id, "index": index, "capability": step.draft.capability, **result},
        ]
        turn["can_schedule"] = result["status"] == "completed"
        return {"session": session.model_dump(mode="json"), "turn": turn}

    async def response(state):
        event = TurnEvent.model_validate(state["event"])
        session = SessionState.model_validate(state["session"])
        turn = state["turn"]
        semantic = SemanticTurn.model_validate(turn["semantic"])
        results = turn["results"]
        if turn.get("navigation_result"):
            results = [turn["navigation_result"], *results]
        last = results[-1] if results else {}
        task = session.active
        current = task.current_index() if task else None
        steps = (
            [
                {
                    "id": s.id,
                    "capability": s.draft.capability,
                    "status": s.status,
                    "arguments": s.draft.arguments.model_dump(exclude_none=True),
                    "result": s.result,
                    "command_id": s.command_id,
                }
                for s in task.steps
            ]
            if task
            else []
        )
        route = (
            "handbook"
            if last.get("capability") == "manual.search"
            else "conversation"
            if last.get("capability", "").startswith(("memory.", "navigation.", "trip."))
            else "action"
            if last
            else "unsupported"
            if semantic.act == "unsupported"
            else "clarify"
            if semantic.act == "clarify"
            else "conversation"
        )
        status = {
            "completed": "action_verified" if route == "action" else "answered",
            "waiting_slot": "clarify",
            "waiting_confirmation": "confirmation_required",
            "blocked": "insufficient_evidence" if route == "handbook" else "blocked",
            "unknown": "action_unverified",
        }.get(
            last.get("status"),
            "unsupported" if semantic.act == "unsupported" else "clarify" if semantic.act == "clarify" else "answered",
        )
        if last.get("status") == "waiting_slot":
            route = "clarify"
        if turn["message"] and semantic.act in {"modify", "confirm", "resume", "request", "question"}:
            status, route = "clarify", "clarify"
        if event.kind == "confirmation" and semantic.act == "clarify":
            status = "denied"
        if event.kind == "confirmation" and semantic.act == "deny":
            status = "denied"
        if event.kind == "confirmation" and semantic.act == "confirm" and last.get("status") == "blocked":
            status = "denied"
        if event.kind == "confirmation" and event.confirmation_id == turn.get("expired_confirmation"):
            status = "expired"
        if (
            (semantic.negated and semantic.act != "reply")
            or semantic.conditional
            or semantic.scheduled
            or semantic.conflicting
            or (semantic.unresolved and semantic.act != "unsupported")
        ):
            status, route = "clarify", "clarify"
        if last.get("status") == "completed" and current is not None:
            status = "clarify"  # Never report whole-task completion for a partial chain.
        message = (
            turn["message"]
            or " ".join(result_messages(results))
            or ("" if event.kind == "navigation" else "Bạn nói rõ yêu cầu nhé.")
        )
        plan = session.trip_plan
        if (
            plan
            and plan.offer
            and plan.offer.get("status") == "pending"
            and current is None
            and any(
                r.get("trip_plan_update") and (r["trip_plan_update"].get("offer") or {}).get("status") == "pending"
                for r in results
            )
        ):
            message += " " + plan.offer["question"]
        confirmation = last.get("confirmation")
        action = last.get("action")
        if confirmation and last.get("capability") == "memory.reset":
            action = {"intent": "memory.reset", "arguments": {}}
        diagnostics = {
            "workflow_version": "unified-5",
            "prompt_version": PROMPT_VERSION,
            "requested_provider": event.provider,
            "effective_provider": turn["effective_provider"],
            "decision_source": turn["effective_provider"],
            "provider_attempts": turn.get("provider_attempts", []),
            "semantic": turn["semantic"],
            "node_spans": turn["spans"],
            "task_id": task.id if task else None,
            "revision": task.revision if task else None,
            "observation_available": any(item.get("vehicle_state") for item in results),
            "suspended_tasks": len(session.suspended),
            "generation_provider": last.get("generation_provider"),
            "generation_attempts": last.get("generation_attempts", []),
        }
        output = AssistantOutput(
            session_id=event.session_id,
            turn_id=event.turn_id,
            route=route,
            intent=(
                "conversation.clarify"
                if route == "clarify"
                else "unsupported.request"
                if route == "unsupported"
                else "conversation.respond"
                if semantic.act in {"reply", "offer"}
                else last.get("capability")
            ),
            status=status,
            response_text=message,
            tts_text=message,
            action_proposal=action,
            execution=last.get("execution"),
            confirmation=confirmation,
            vehicle_state=next(
                (item["vehicle_state"] for item in reversed(results) if item.get("vehicle_state")), None
            ),
            citations=last.get("citations", []),
            grounding_status=last.get("grounding_status", "not_applicable"),
            errors=turn["errors"],
            timings=turn["timings"],
            diagnostics=diagnostics,
            steps=steps,
            navigation=session.navigation.model_dump(mode="json"),
            trip_plan=session.trip_plan.model_dump(mode="json") if session.trip_plan else None,
            ui_events=[item for result in results for item in result.get("ui_events", [])],
        )
        return {"output": output.model_dump(mode="json")}

    async def persist(state):
        event, output = state["event"], state["output"]
        if event.get("navigation_event", {}) and event["navigation_event"].get("kind") in {"progress", "charge"}:
            return {}
        await asyncio.to_thread(
            services.history.save,
            {
                **event,
                **output,
                "session_id": state["turn"]["history_session"],
                "query": event["text"],
                "answer": output["response_text"],
            },
        )
        return {}

    def await_input(state):
        event = interrupt({"task": state["session"].get("active"), "output": state["output"]})
        # Resumption is input, not permission. It traverses the entire reducer path.
        return {"event": TurnEvent.model_validate(event).model_dump(mode="json")}

    def waiting(state):
        session = SessionState.model_validate(state["session"])
        return (
            "await_input" if session.active and session.active.current_index() is not None or session.suspended else END
        )

    def timed(name, function):
        async def node(state):
            started = time.perf_counter()
            result = await function(state)
            turn = dict(result.get("turn", state.get("turn", {})))
            elapsed = round((time.perf_counter() - started) * 1000, 2)
            turn["timings"] = {**turn.get("timings", {}), name: turn.get("timings", {}).get(name, 0) + elapsed}
            session = SessionState.model_validate(result.get("session", state.get("session", {})))
            index = turn.get("index")
            step = (
                session.active.steps[index]
                if session.active and index is not None and index < len(session.active.steps)
                else None
            )
            outcome = (
                turn.get("effective_provider", "rules")
                if name == "interpret"
                else step.status
                if step and name in {"validate", "capability"}
                else "clarify"
                if turn.get("message")
                else "ok"
            )
            turn["spans"] = [
                *turn.get("spans", []),
                {
                    "node": name,
                    "outcome": outcome,
                    "latency_ms": elapsed,
                    "step_id": step.id if step else None,
                    "provider": turn.get("effective_provider"),
                },
            ]
            return {**result, "turn": turn}

        return node

    graph = StateGraph(DialogueState)
    for name, function in {
        "context": context,
        "interpret": interpret,
        "reducer": transition,
        "scheduler": schedule,
        "validate": validate,
        "capability": capability,
    }.items():
        graph.add_node(name, timed(name, function))
    graph.add_node("response", response)
    graph.add_node("persist", persist)
    graph.add_node("await_input", await_input)
    graph.add_edge(START, "context")
    graph.add_edge("context", "interpret")
    graph.add_edge("interpret", "reducer")
    graph.add_edge("reducer", "scheduler")
    graph.add_conditional_edges(
        "scheduler",
        lambda s: "validate" if s["turn"]["dispatch"] else "response",
        {"validate": "validate", "response": "response"},
    )
    graph.add_conditional_edges(
        "validate",
        lambda s: "capability" if s["turn"]["dispatch"] else "scheduler",
        {"capability": "capability", "scheduler": "scheduler"},
    )
    graph.add_edge("capability", "scheduler")
    graph.add_edge("response", "persist")
    graph.add_conditional_edges("persist", waiting, {"await_input": "await_input", END: END})
    graph.add_edge("await_input", "context")
    return AssistantGraph(graph.compile(checkpointer=saver), saver, profile)
