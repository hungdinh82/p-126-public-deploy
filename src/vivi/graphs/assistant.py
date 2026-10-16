"""One checkpoint-owned conversation workflow, independent of model provider."""

from __future__ import annotations

import asyncio
import logging
import time
from contextlib import aclosing, nullcontext
from typing import TypedDict
from uuid import uuid4
from weakref import WeakValueDictionary

from langgraph.graph import END, START, StateGraph

from src.vivi.agents.contracts import AssistantOutput
from src.vivi.capabilities.execution import CapabilityRuntime
from src.vivi.capabilities.registry import REGISTRY, catalog
from src.vivi.capabilities.result import CapabilityResult
from src.vivi.conversation.guardrails import TopicDecision, assess_topic, output_violation, terminal_decision
from src.vivi.dialogue.contracts import SemanticTurn, SessionState, TurnEvent
from src.vivi.dialogue.planner import PROMPT_VERSION, SemanticResolver
from src.vivi.dialogue.reducer import (
    clear_journey,
    expire,
    prepare_step,
    record_step,
    reduce_turn,
    synchronize_navigation_question,
)
from src.vivi.graphs.concurrency import PriorityLock, SupersededTurnError, event_priority, external_read, writer_context
from src.vivi.navigation.presentation import result_messages
from src.vivi.navigation.projections import dialogue_result, dialogue_turn
from src.vivi.navigation.service import NavigationService
from src.vivi.navigation.store import checkpoint_session
from src.vivi.persistence.runtime_store import fingerprint
from src.vivi.turn_control import current_control

CONTROL_EVENTS = {"gear", "pause", "resume", "end", "clear", "history_clear", "progress", "charge"}


class DialogueState(TypedDict, total=False):
    event: dict
    session: dict
    turn: dict
    output: dict


logger = logging.getLogger(__name__)


class AssistantGraph:
    def __init__(self, compiled, saver, profile, control, vehicle, runtime_epoch, energy, navigation):
        self.compiled, self.saver, self.profile = compiled, saver, profile
        self.control, self.vehicle, self.runtime_epoch = control, vehicle, runtime_epoch
        self.energy = energy
        self.navigation = navigation
        self._dialogue_sessions = {}
        self._tasks = set()
        self._closing = False
        self._pending_telemetry = {}
        self._gear_requests = {}
        self.default_provider = "google"
        # Hold the lock from checkpoint read through the final checkpoint write.
        # Weak values discard idle session locks; provider aliases share this graph.
        self._thread_locks = WeakValueDictionary()
        self._dialogue_locks = WeakValueDictionary()

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
            else "action"
            if state.get("action") or state.get("question_reply")
            else "utterance",
            action=state.get("action"),
            question_reply=state.get("question_reply"),
            navigation_event=state.get("navigation_event"),
            position=state.get("position"),
            vehicle_model=state.get("vehicle_model", "VF8"),
            model_year=state.get("model_year", 2026),
            locale=state.get("locale", "vi_vn"),
        ).model_dump(mode="json")

    def config(self, event):
        identifier = fingerprint({"profile": self.profile, "session": event["session_id"]})
        return {"configurable": {"thread_id": "conversation:4:" + identifier}, "recursion_limit": 60}

    async def input(self, event, config):
        return {"event": event}

    def invoke(self, state):
        return asyncio.run(self.ainvoke(state))

    def thread_lock(self, config):
        return self._thread_locks.setdefault(config["configurable"]["thread_id"], PriorityLock())

    def generation(self, config):
        return self.navigation.generation(config["configurable"]["thread_id"])

    def dialogue_lock(self, config):
        return self._dialogue_locks.setdefault(config["configurable"]["thread_id"], asyncio.Lock())

    async def snapshot(self, session_id, event_id=None):
        key = self.navigation.key(session_id)
        async with self.navigation.lock(key):
            session = await self.navigation.synchronize(SessionState(), session_id)
            result = {
                "runtime_epoch": self.runtime_epoch,
                "snapshot_sequence": session.snapshot_sequence,
                "control_revision": session.control_revision,
                "control_uncertain": await self.navigation.repository.run(
                    self.navigation.repository.uncertain_control, key, session.control_revision
                ),
                "event_id": event_id,
                "vehicle_state": (await self.vehicle.get_state(session_id)).model_dump(mode="json"),
                "navigation": session.navigation.model_dump(mode="json"),
                "navigation_workspace": session.navigation_workspace.model_dump(mode="json"),
                "trip_plan": session.trip_plan.model_dump(mode="json") if session.trip_plan else None,
                "ui_events": [],
                "receipt": await self.navigation.repository.run(self.navigation.repository.receipt, key, event_id)
                if event_id
                else None,
                "control_receipts": session.control_receipts,
            }

        # Dialogue questions are read separately, without holding the domain
        # writer while SQLite loads a checkpoint. Reading never creates a question.
        checkpoint = await self.compiled.aget_state(self.config({"session_id": session_id}))
        dialogue = SessionState.model_validate(checkpoint.values.get("session") or {})
        result["pending_question"] = dialogue.pending_question()
        return result

    async def ainvoke(self, state):
        if self._closing:
            raise RuntimeError("Assistant runtime is closing")
        task = asyncio.current_task()
        self._tasks.add(task)
        try:
            return await self._ainvoke(state)
        finally:
            self._tasks.discard(task)

    async def _ainvoke(self, state):
        event = self.event(state)
        config = self.config(event)
        queued = time.perf_counter()
        value = event.get("navigation_event") or {}
        gear_order = None
        if value.get("kind") == "gear":
            key = config["configurable"]["thread_id"]
            prior = self._gear_requests.get(key)
            gear_order = prior[0] if prior and prior[2] == value["id"] else prior[0] + 1 if prior else 1
            self._gear_requests[key] = (gear_order, value.get("gear"), value["id"])
        telemetry_key = (config["configurable"]["thread_id"], value.get("kind"))
        batch = None
        if value.get("kind") in {"progress", "charge"}:
            batch = self._pending_telemetry.get(telemetry_key)
            if batch is None:
                batch = {
                    "event": event,
                    "count": 1,
                    "result": None,
                    "ids": {value["id"]},
                    "elapsed": {value["id"]: value.get("elapsed_s") or 0},
                    "members": {value["id"]: value},
                }
                self._pending_telemetry[telemetry_key] = batch
            else:
                if value["id"] not in batch["ids"]:
                    batch["event"] = event
                    batch["elapsed"][value["id"]] = value.get("elapsed_s") or 0
                batch["ids"].add(value["id"])
                batch["members"].setdefault(value["id"], value)
                batch["count"] += 1
        priority_token = event_priority.set(
            0
            if value.get("gear") == "P"
            else 3
            if value.get("kind") in {"progress", "charge"}
            else 1
            if value.get("kind") in CONTROL_EVENTS
            else 2
        )
        lock = (
            self.navigation.lock(config["configurable"]["thread_id"])
            if value.get("kind") in CONTROL_EVENTS
            else self.thread_lock(config)
        )
        try:
            async with nullcontext() if value.get("kind") in CONTROL_EVENTS else self.dialogue_lock(config), lock:
                token = writer_context.set((self, config, lock))
                try:
                    if batch:
                        if batch["result"] is None:
                            if self._pending_telemetry.get(telemetry_key) is batch:
                                self._pending_telemetry.pop(telemetry_key)
                            batch["result"] = await self._invoke_locked(
                                batch["event"],
                                config,
                                queued,
                                coalesced_ids=batch["ids"],
                                coalesced_elapsed=batch["elapsed"],
                                coalesced_members=batch["members"],
                            )
                        result = dict(batch["result"])
                        result["output"] = {**result["output"], "turn_id": event["turn_id"]}
                        result["output"]["diagnostics"] = {
                            **result["output"]["diagnostics"],
                            "coalesced_events": batch["count"],
                        }
                        return await self.aproject(result)
                    return await self._invoke_locked(event, config, queued, gear_order)
                finally:
                    writer_context.reset(token)
        finally:
            event_priority.reset(priority_token)

    async def _invoke_locked(
        self, event, config, queued, gear_order=None, coalesced_ids=None, coalesced_elapsed=None, coalesced_members=None
    ):
        key = config["configurable"]["thread_id"]
        if (event.get("navigation_event") or {}).get("kind") in CONTROL_EVENTS:
            previous = {"session": self._dialogue_sessions.get(key, {})}
            value = event["navigation_event"]
            latest_gear = self._gear_requests.get(key)
            superseded = bool(
                gear_order
                and value.get("gear") == "D"
                and latest_gear
                and latest_gear[1] == "P"
                and latest_gear[0] > gear_order
            )
            members = coalesced_members
            result = await self.control(event, previous, queued, superseded=superseded, coalesced=members)
            return await self.aproject(result)
        snapshot = await self.compiled.aget_state(config)
        old = snapshot.values.get("event", {})
        if (
            old.get("turn_id") == event["turn_id"]
            and snapshot.values.get("output", {}).get("turn_id") == event["turn_id"]
        ):
            if old != event:
                raise ValueError("turn_id đã được dùng cho một yêu cầu khác.")
            return await self.aproject(snapshot.values)
        try:
            result = await self.compiled.ainvoke(await self.input(event, config), config, durability="sync")
        except SupersededTurnError:
            latest = await self.compiled.aget_state(config)
            result = await self.control(event, latest.values, queued, superseded=True)
            await self.compiled.aupdate_state(config, result, as_node="persist")
        return await self.aproject(result)

    async def astream_dialogue(self, state):
        """Project domain state only for updates consumed by the chat stream."""
        async with aclosing(
            self.astream(state, stream_mode=["updates", "values"], navigation_updates_only=True)
        ) as stream:
            async for item in stream:
                yield item

    async def astream(self, state, stream_mode, *, navigation_updates_only=False):
        if self._closing:
            raise RuntimeError("Assistant runtime is closing")
        task = asyncio.current_task()
        self._tasks.add(task)
        try:
            async with aclosing(
                self._astream(state, stream_mode, navigation_updates_only=navigation_updates_only)
            ) as stream:
                async for item in stream:
                    yield item
        finally:
            self._tasks.discard(task)

    async def _astream(self, state, stream_mode, *, navigation_updates_only=False):
        event = self.event(state)
        config = self.config(event)
        lock = self.thread_lock(config)
        async with self.dialogue_lock(config), lock:
            token = writer_context.set((self, config, lock))
            try:
                async with aclosing(
                    self.compiled.astream(
                        await self.input(event, config), config, stream_mode=stream_mode, durability="sync"
                    )
                ) as stream:
                    async for item in stream:
                        multiple = isinstance(stream_mode, (list, tuple))
                        mode, chunk = item if multiple else (stream_mode, item)
                        if mode == "values" and (chunk.get("output") or {}).get("turn_id") == event["turn_id"]:
                            chunk = await self.aproject(chunk)
                        elif mode == "updates":
                            for name, update in chunk.items():
                                if navigation_updates_only and (
                                    name not in {"capability", "validate"}
                                    or not any(
                                        r.get("draft_update") or r.get("journey_update")
                                        for r in ((update or {}).get("turn") or {}).get("results", [])
                                    )
                                ):
                                    continue
                                if isinstance(update, dict) and update.get("session"):
                                    hydrated = await self.navigation.hydrate(
                                        SessionState.model_validate(update["session"]), event["session_id"]
                                    )
                                    chunk = {**chunk, name: {**update, "session": hydrated.model_dump(mode="json")}}
                        yield (mode, chunk) if multiple else chunk
            except SupersededTurnError:
                latest = await self.compiled.aget_state(config)
                result = await self.control(event, latest.values, time.perf_counter(), superseded=True)
                await self.compiled.aupdate_state(config, result, as_node="persist")
                projected = await self.aproject(result)
                if isinstance(stream_mode, (list, tuple)):
                    yield ("values", projected)
                else:
                    yield projected
            finally:
                writer_context.reset(token)

    async def aproject(self, state):
        event = state.get("event", {})
        session = await self.navigation.hydrate(
            SessionState.model_validate(state.get("session") or {}), event.get("session_id", "cli")
        )
        return self.project({**state, "session": session})

    @staticmethod
    def project(state):
        event = state.get("event", {})
        session = SessionState.model_validate(state.get("session") or {})
        payload = session.model_dump(mode="json")
        output, turn = dict(state.get("output", {})), state.get("turn", {})
        output.update(
            pending_question=session.pending_question(),
            navigation=session.navigation.model_dump(mode="json"),
            control_revision=session.control_revision,
            navigation_workspace=payload["navigation_workspace"],
            control_receipts=session.control_receipts,
            snapshot_sequence=session.snapshot_sequence,
        )
        return {
            **state,
            "session": payload,
            **output,
            "output": output,
            "answer": output.get("response_text"),
            "response": output.get("response_text"),
            "query": event.get("text"),
            "conversation_history": turn.get("history", []),
            "memory_context": turn.get("memory", []),
        }

    def close(self):
        self._closing = True
        self.saver.close()
        self.navigation.repository.close()

    async def aclose(self):
        self._closing = True
        tasks = [task for task in self._tasks if task is not asyncio.current_task()]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await self.saver.aclose()
        await self.navigation.repository.aclose()
        client = self.navigation.workflow.services.navigation
        if client and hasattr(client, "aclose"):
            await client.aclose()
        if hasattr(self.saver, "_navigation_directory"):
            self.saver._navigation_directory.cleanup()


def build_assistant_graph(services, saver, store=None, provider=None, *, resolver=None, generators=None, budgets=None):
    runtime_epoch = str(uuid4())
    resolver = resolver or SemanticResolver()
    if services.conversation is None:
        from src.vivi.conversation.service import ConversationService

        services.conversation = ConversationService(gateway=resolver.gateway)
    runtime = CapabilityRuntime(
        services,
        store,
        generators,
        budgets,
        resolver.breaker_seconds,
        provider_chains=resolver.fallback_chains,
        gateway=resolver.gateway,
    )
    if store is None:
        from pathlib import Path

        from src.vivi.persistence.runtime_store import RuntimeStore

        database = saver.connection.execute("PRAGMA database_list").fetchone()[2]
        if not database:
            from tempfile import TemporaryDirectory

            saver._navigation_directory = TemporaryDirectory(prefix="vivi-navigation-")
            database = str(Path(saver._navigation_directory.name) / "state.sqlite3")
        store = RuntimeStore(Path(database).with_suffix(".runtime.sqlite3"))
    domain = NavigationService(services, store, saver, resolver, runtime_epoch)
    navigation = domain.workflow
    profile = services.memory_profile_id

    async def context(state):
        event = TurnEvent.model_validate(state["event"])
        previous = SessionState.model_validate(state.get("session") or {})
        session = expire(previous)
        session = await domain.synchronize(session, event.session_id)
        session = synchronize_navigation_question(session)
        reference = ((state.get("session") or {}).get("navigation_reference") or {}).get("revision", -1)
        if (
            session.navigation_workspace.cleared_at_revision > 0
            and reference < session.navigation_workspace.cleared_at_revision
        ):
            session = clear_journey(session)
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
                        if step.draft.capability == "navigation.start":
                            await domain.reconcile(event.session_id, projection)
                            session = await domain.hydrate(session, event.session_id)
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
                "effective_provider": None,
            },
        }

    async def interpret(state):
        event = TurnEvent.model_validate(state["event"])
        session = await domain.hydrate(SessionState.model_validate(state["session"]), state["event"]["session_id"])
        turn = dict(state["turn"])
        guardrail = terminal_decision(event.text, event.action, event.question_reply)
        if guardrail:
            semantic = SemanticTurn(act="reply", text=guardrail.message)
            served, attempts = None, []
        elif event.question_reply:
            from src.vivi.dialogue.answers import AnswerResolver

            semantic = AnswerResolver().resolve(event.text, session, reply=event.question_reply)
            served, attempts = None, []
        elif event.kind == "navigation":
            result, session = await domain.event(event, session)
            turn["navigation_result"] = result
            turn["timings"].update(result.get("timings", {}))
            semantic, served, attempts = SemanticTurn(act="reply", text=result.get("message", "")), None, []
            if result.get("request_action"):
                semantic = SemanticTurn(act="request", actions=[result["request_action"]])
            task = session.active
            index = task.current_index() if task else None
            if (
                event.navigation_event.get("kind") == "position"
                and index is not None
                and task.steps[index].draft.capability.startswith("navigation.")
                and task.steps[index].status == "waiting_slot"
                and (result.get("navigation_update") or result.get("draft_update"))
            ):
                semantic = SemanticTurn(act="resume", task_ref="active")
        elif event.kind == "action" and event.action:
            semantic, served, attempts = SemanticTurn(act="request", actions=[event.action]), None, []
        else:
            semantic, served, attempts = await external_read(
                resolver.resolve(
                    event.text,
                    session,
                    requested=event.provider,
                    history=turn["history"],
                    memory=turn["memory"],
                    catalog=catalog(
                        services.navigation is not None,
                        conversation=services.conversation.enabled,
                        information=services.information is not None and services.information.online,
                    ),
                ),
                fence=lambda resolved: (
                    resolved[0].act in {"confirm", "deny", "modify", "resume", "cancel"}
                    or any(
                        REGISTRY.get(d.capability) and not REGISTRY[d.capability].spec.read_only
                        for d in resolved[0].drafts()
                    )
                ),
            )
        if semantic.question_answer:
            from src.vivi.dialogue.answers import AnswerResolver

            semantic = AnswerResolver().resolve("", session, reply=semantic.question_answer)
            turn["rejected_question_reply"] = semantic.act == "clarify"
        if not guardrail:
            guardrail = terminal_decision(
                [draft.arguments.model_dump(exclude_none=True) for draft in semantic.drafts()]
            )
        if not guardrail and semantic.text and output_violation(semantic.text, assess_topic(event.text)):
            guardrail = TopicDecision(
                "general",
                "refuse",
                "unsafe_semantic_response",
                "Mình chưa thể trả lời hoặc thực hiện yêu cầu này trong giới hạn an toàn. Bạn có thể hỏi theo hướng kiến thức chung nhé.",
            )
        if guardrail:
            # Block the whole compound turn before reducer/scheduler/effects.
            # Retain the existing task; never turn a refusal into execution consent.
            semantic = SemanticTurn(act="reply", text=guardrail.message)
            turn["topic_result"] = guardrail.result()
        # Domain invalidity is a clarification, never a reason to ask a weaker model.
        if any(action.capability not in REGISTRY for action in semantic.drafts()):
            semantic = SemanticTurn(act="unsupported", text="Mình chưa hỗ trợ thao tác đó.")
        turn.update(
            semantic=semantic.model_dump(mode="json"),
            effective_provider=served,
            semantic_source="typed_event"
            if event.kind != "utterance" or event.question_reply
            else "llm"
            if served
            else "fallback",
            provider_attempts=attempts,
        )
        return {"turn": turn}

    async def transition(state):
        previous = await domain.hydrate(SessionState.model_validate(state["session"]), state["event"]["session_id"])
        semantic = SemanticTurn.model_validate(state["turn"]["semantic"])
        reducer_input = previous
        # A typed map event can create a handle and request its selection in
        # the same turn. Validate that action against the accepted event state,
        # not the candidate list from before the event.
        update = state["turn"].get("navigation_result", {})
        # Typed event transitions are already committed by the domain writer.
        if update.get("clear_navigation_history"):
            reducer_input.navigation_workspace.history = []
        if update.get("clear_journey"):
            reducer_input = clear_journey(reducer_input)
        session, message = reduce_turn(
            reducer_input,
            semantic,
            revoke_consent_on_clarify=state["event"]["kind"] == "utterance"
            and not state["turn"].get("rejected_question_reply"),
        )
        for command in session.domain_commands:
            await domain.command(state["event"]["session_id"], command)
        session = await domain.hydrate(session, state["event"]["session_id"])
        session.domain_commands = []
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
        if (
            old
            and old.current_index() is not None
            and (new is None or old.id != new.id or old.revision != new.revision or semantic.act in {"cancel", "deny"})
        ):
            for step in old.steps:
                if step.confirmation and step.status in {"waiting_confirmation", "approved"}:
                    await asyncio.to_thread(
                        services.action_gateway.confirmations.cancel,
                        state["event"]["session_id"],
                        step.confirmation["confirmation_id"],
                    )
        turn = {
            **state["turn"],
            "message": message,
            "can_schedule": message is None
            and semantic.act in {"request", "question", "modify", "confirm", "deny", "resume"},
        }
        return {"session": session.model_dump(mode="json"), "turn": turn}

    async def schedule(state):
        session = await domain.hydrate(SessionState.model_validate(state["session"]), state["event"]["session_id"])
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
        session = await domain.hydrate(SessionState.model_validate(state["session"]), state["event"]["session_id"])
        turn = dict(state["turn"])
        index, task = turn["index"], session.active
        step = task.steps[index]
        spec = REGISTRY[step.draft.capability]
        command_id = step.command_id or f"task:{task.id}:r{task.revision}:step:{step.id}"
        if step.draft.capability == "navigation.start" and session.navigation_workspace.draft:
            plan = session.navigation_workspace.draft
            command_id = f"{plan.id}:r{plan.revision}:start"
            if step.status != "approved":
                result = {"status": "waiting_confirmation", "message": "Bạn xác nhận bắt đầu tuyến đang xem nhé."}
                session = record_step(session, index, status="waiting_confirmation", result=result)
                turn["results"] = [
                    *turn["results"],
                    {"step_id": step.id, "index": index, "capability": step.draft.capability, **result},
                ]
                turn["can_schedule"], turn["dispatch"] = False, False
                return {"session": session.model_dump(mode="json"), "turn": turn}
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
            session.active.steps[index].missing_slots = spec.missing_slots(step.draft)
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
        session = await domain.hydrate(SessionState.model_validate(state["session"]), state["event"]["session_id"])
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
                result = await external_read(
                    runtime.handbook(event, arguments, event.provider, turn["history"]), fence=False
                )
            elif spec.executor == "conversation":
                from src.vivi.conversation.context import conversation_view
                from src.vivi.information.time_context import search_time_context

                result = await external_read(
                    services.conversation.respond(
                        arguments["query"],
                        provider=event.provider,
                        mode=arguments.get("mode", "general"),
                        history=turn["history"],
                        memory=turn["memory"],
                        context={
                            **conversation_view(session),
                            **search_time_context(
                                arguments["query"],
                                services.information.settings.information_timezone
                                if services.information
                                else "Asia/Ho_Chi_Minh",
                            ),
                        },
                    ),
                    fence=False,
                )
            elif spec.executor == "information":
                if services.information is None:
                    result = {
                        "status": "blocked",
                        "message": "Mình chưa kết nối dịch vụ thông tin internet.",
                        "error_code": "information_not_configured",
                    }
                else:
                    result = await external_read(
                        services.information.execute(
                            step.draft.capability,
                            arguments,
                            event,
                            session,
                            provider=event.provider,
                            history=turn["history"],
                            memory=turn["memory"],
                        ),
                        fence=False,
                    )
            else:
                result = await external_read(
                    domain.execute(event, session, step, arguments, command_id, event.provider), fence=False
                )
        except SupersededTurnError:
            raise
        except Exception as exc:
            logger.exception(
                "Capability failed session=%s turn=%s capability=%s",
                event.session_id,
                event.turn_id,
                step.draft.capability,
            )
            result = (
                {
                    "status": "blocked",
                    "message": "Mình chưa tạo được câu trả lời đáng tin cậy. Bạn thử lại nhé.",
                    "error_code": "answer_unavailable",
                }
                if spec.executor in {"conversation", "information"}
                else {
                    "status": "unknown",
                    "message": "Mình chưa xác minh được kết quả bước này. Bạn kiểm tra trước khi yêu cầu lại nhé.",
                }
            )
            turn["errors"] = [
                *turn["errors"],
                {"stage": "capability", "error_type": type(exc).__name__, "message": str(exc)},
            ]
        for stage, elapsed in result.get("timings", {}).items():
            turn["timings"][stage] = round(turn["timings"].get(stage, 0) + elapsed, 2)
        result = CapabilityResult.normalize(result, capability=step.draft.capability, command_id=command_id)
        session = record_step(
            session,
            index,
            status=result["status"],
            result=result,
            confirmation=result.get("confirmation"),
            command_id=command_id if result["status"] != "waiting_slot" else None,
        )
        if result.get("draft_update") and step.draft.capability.startswith("navigation."):
            bound = session.active.steps[index].draft.arguments
            bound.plan_id = result["draft_update"]["id"]
            bound.route_revision = result["draft_update"]["revision"]
        if result["status"] == "completed" and result.get("draft_update"):
            plan = result["draft_update"]
            if (
                plan.get("purpose") == "navigate"
                and (plan.get("offer") or {}).get("status") == "pending"
                and not plan.get("clarification")
                and step.draft.capability != "navigation.start"
            ):
                from src.vivi.dialogue.contracts import TaskStep

                session.active.steps.insert(
                    index + 1,
                    TaskStep(
                        draft={
                            "capability": "navigation.start",
                            "arguments": {"plan_id": plan["id"], "route_revision": plan["revision"]},
                        },
                        status="waiting_confirmation",
                        result={"message": plan["offer"]["question"]},
                    ),
                )
        if spec.executor in {"conversation", "information"}:
            session.conversation_context = {
                **(
                    result.get("information_context")
                    or {"capability": step.draft.capability, "query": arguments.get("query")}
                ),
                "focus": "information",
            }
        elif spec.executor == "navigation":
            session.conversation_context.pop("focus", None)
        session = synchronize_navigation_question(await domain.hydrate(session, event.session_id))
        from src.vivi.dialogue.questions import propose_question
        from src.vivi.dialogue.reducer import synchronize_question

        proposal = propose_question(session)
        if proposal:
            result = {**result, "question_proposal": proposal.model_dump(mode="json")}
        session = synchronize_question(session, proposal)
        turn["results"] = [
            *turn["results"],
            {"step_id": step.id, "index": index, "capability": step.draft.capability, **result},
        ]
        turn["can_schedule"] = result["status"] == "completed"
        return {"session": session.model_dump(mode="json"), "turn": turn}

    async def response(state):
        from src.vivi.dialogue.reducer import synchronize_question

        control = current_control.get()
        if control:
            control.stage = "response"
        event = TurnEvent.model_validate(state["event"])
        session = await domain.hydrate(SessionState.model_validate(state["session"]), event.session_id)
        session = synchronize_question(session)
        if not state["turn"].get("domain_control"):
            session = await domain.allocate_snapshot(session, event.session_id)
        turn = state["turn"]
        semantic = SemanticTurn.model_validate(turn["semantic"])
        results = turn["results"]
        if turn.get("navigation_result"):
            results = [turn["navigation_result"], *results]
        last = (
            {"capability": "conversation.respond", **turn["topic_result"]}
            if turn.get("topic_result")
            else results[-1]
            if results
            else {}
        )
        task = session.active
        current = task.current_index() if task else None
        steps = (
            [
                {
                    "id": s.id,
                    "capability": s.draft.capability,
                    "status": s.status,
                    "arguments": s.draft.arguments.model_dump(exclude_none=True),
                    "result": dialogue_result(s.result) if s.result else s.result,
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
            if last.get("capability", "").startswith(
                ("memory.", "navigation.", "conversation.", "web.", "weather.", "knowledge.")
            )
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
            "unknown": "generation_error" if route == "conversation" else "action_unverified",
        }.get(
            last.get("status"),
            "unsupported" if semantic.act == "unsupported" else "clarify" if semantic.act == "clarify" else "answered",
        )
        if current is not None and task.steps[current].status == "waiting_confirmation" and semantic.act != "clarify":
            status = "confirmation_required"
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
        if (
            last.get("status") == "completed"
            and current is not None
            and event.kind != "navigation"
            and status != "confirmation_required"
        ):
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
                r.get("draft_update") and (r["draft_update"].get("offer") or {}).get("status") == "pending"
                for r in results
            )
        ):
            message += " " + plan.offer["question"]
        confirmation = last.get("confirmation")
        action = last.get("action")
        if confirmation and last.get("capability") == "memory.reset":
            action = {"intent": "memory.reset", "arguments": {}}
        diagnostics = {
            "workflow_version": "core-2",
            "prompt_version": PROMPT_VERSION,
            "requested_provider": event.provider,
            "effective_provider": turn["effective_provider"],
            "semantic_source": turn.get("semantic_source", "typed_event"),
            "model_stages": control.model_attempts if control else [],
            "provider_attempts": turn.get("provider_attempts", []),
            "semantic": turn["semantic"],
            "node_spans": turn["spans"],
            "task_id": task.id if task else None,
            "revision": task.revision if task else None,
            "observation_available": any(item.get("vehicle_state") for item in results),
            "suspended_tasks": len(session.suspended),
            "generation_provider": last.get("generation_provider"),
            "generation_attempts": last.get("generation_attempts", []),
            "information_provider": last.get("information_provider"),
            "information_cache_hit": last.get("cache_hit"),
            "source_count": last.get("source_count"),
            "retrieval_attempts": last.get("retrieval_attempts", []),
            "source_policy": last.get("source_policy"),
            "error_code": last.get("error_code"),
            "failure_kind": last.get("failure_kind"),
            "topic_guardrail": last.get("topic_guardrail"),
            "evidence_stats": last.get("evidence_stats"),
            "retrieval_query": (last.get("information_context") or {}).get("search_query")
            or (last.get("information_context") or {}).get("query"),
            "answer_question": last.get("answer_question"),
        }
        output = AssistantOutput(
            runtime_epoch=runtime_epoch,
            snapshot_sequence=session.snapshot_sequence,
            event_id=(event.navigation_event or {}).get("id", event.turn_id),
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
            answer_kind=last.get("answer_kind"),
            answer_basis=last.get("answer_basis"),
            action_proposal=action,
            execution=last.get("execution"),
            confirmation=confirmation,
            vehicle_state=next(
                (item["vehicle_state"] for item in reversed(results) if item.get("vehicle_state")), None
            ),
            citations=list(
                {item["source_id"]: item for result in results for item in result.get("citations", [])}.values()
            ),
            grounding_status=last.get("grounding_status", "not_applicable"),
            errors=turn["errors"],
            timings=turn["timings"],
            diagnostics=diagnostics,
            control_receipts=session.control_receipts,
            receipt=turn.get("navigation_result", {}).get("receipt"),
            steps=steps,
            ui_events=[item for result in results for item in result.get("ui_events", [])],
        )
        # Public projection hydrates domain fields after the checkpoint. Avoid
        # serializing full geometry here only to throw it away immediately.
        projected = output.model_dump(
            mode="json", exclude={"navigation", "navigation_workspace", "trip_plan", "control_receipts"}
        )
        return {"output": projected, "session": checkpoint_session(session)}

    async def control(event_data, previous, queued, superseded=False, coalesced=None):
        started = time.perf_counter()
        event = TurnEvent.model_validate(event_data)
        session = await domain.hydrate(SessionState.model_validate(previous.get("session") or {}), event.session_id)
        before = session.navigation.status
        if superseded and not event.navigation_event:
            for task in [session.active, *session.suspended]:
                if task:
                    for step in task.steps:
                        if step.status == "running" and step.draft.capability.startswith("navigation."):
                            step.status, step.command_id = "pending", None
            result = {
                "status": "blocked",
                "message": "Trạng thái hành trình vừa thay đổi. Bạn yêu cầu lại nhé.",
                "error_code": "superseded_turn",
            }
        else:
            result, session = await domain.event_locked(event, session, superseded=superseded, coalesced=coalesced)
        if session.navigation_workspace.cleared_at_revision > 0 and session.navigation_workspace.cleared_at_revision > (
            (previous.get("session") or {}).get("navigation_reference") or {}
        ).get("revision", -1):
            session = clear_journey(session)
        result["vehicle_state"] = (await services.action_gateway.vehicle.get_state(event.session_id)).model_dump(
            mode="json"
        )
        elapsed = round((time.perf_counter() - started) * 1000, 2)
        queue_ms = round((started - queued) * 1000, 2)
        turn = {
            "semantic": SemanticTurn(act="reply").model_dump(mode="json"),
            "domain_control": bool(event.navigation_event),
            "results": [],
            "navigation_result": result,
            "message": result.get("message", ""),
            "errors": [],
            "timings": {"queue": queue_ms, "control": elapsed, **result.get("timings", {})},
            "spans": [],
            "effective_provider": None,
            "semantic_source": "typed_event",
            "history_session": event.session_id if profile == "default" else f"profile:{profile}:{event.session_id}",
        }
        state = {"event": event_data, "session": session.model_dump(mode="json"), "turn": turn}
        state.update(await response(state))
        state["output"]["diagnostics"]["navigation_control"] = {
            "event_id": (event.navigation_event or {}).get("id"),
            "kind": (event.navigation_event or {}).get("kind"),
            "requested_gear": (event.navigation_event or {}).get("gear"),
            "before": before,
            "after": session.navigation.status,
            "queue_ms": queue_ms,
            "control_ms": elapsed,
            "reason": result.get("error_code"),
        }
        logger.info(
            "navigation_control session=%s event=%s kind=%s gear=%s before=%s after=%s queue_ms=%s control_ms=%s reason=%s",
            event.session_id,
            (event.navigation_event or {}).get("id"),
            (event.navigation_event or {}).get("kind"),
            (event.navigation_event or {}).get("gear"),
            before,
            session.navigation.status,
            queue_ms,
            elapsed,
            result.get("error_code"),
        )
        if not superseded:
            await persist(state)
        return state

    async def persist(state):
        control = current_control.get()
        if control:
            control.stage = "persist"
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

    def timed(name, function):
        async def node(state):
            started = time.perf_counter()
            control = current_control.get()
            if control:
                control.stage = name
            result = await function(state)
            if "session" in result:
                result["session"] = checkpoint_session(SessionState.model_validate(result["session"]))
            turn = dialogue_turn(result.get("turn", state.get("turn", {})))
            elapsed = round((time.perf_counter() - started) * 1000, 2)
            turn["timings"] = {**turn.get("timings", {}), name: turn.get("timings", {}).get(name, 0) + elapsed}
            if control:
                control.timings = dict(turn["timings"])
                control.stage = "checkpoint"
            session = SessionState.model_validate(result.get("session", state.get("session", {})))
            index = turn.get("index")
            step = (
                session.active.steps[index]
                if session.active and index is not None and index < len(session.active.steps)
                else None
            )
            outcome = (
                turn.get("semantic_source") or turn.get("effective_provider") or "fallback"
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
            owner_context = writer_context.get()
            if owner_context:
                owner, config, _ = owner_context
                owner._dialogue_sessions[config["configurable"]["thread_id"]] = result.get(
                    "session", state.get("session", {})
                )
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
    graph.add_edge("persist", END)
    return AssistantGraph(
        graph.compile(checkpointer=saver),
        saver,
        profile,
        control,
        services.action_gateway.vehicle,
        runtime_epoch,
        navigation.energy,
        domain,
    )
