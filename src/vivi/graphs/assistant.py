"""One checkpoint-owned conversation workflow, independent of model provider."""

from __future__ import annotations

import asyncio
import logging
import time
from contextlib import aclosing, nullcontext
from datetime import UTC, datetime
from typing import TypedDict
from uuid import uuid4
from weakref import WeakValueDictionary

from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from src.vivi.agents.contracts import AssistantOutput
from src.vivi.capabilities.execution import CapabilityRuntime
from src.vivi.capabilities.registry import REGISTRY, catalog
from src.vivi.dialogue.contracts import SemanticTurn, SessionState, TurnEvent
from src.vivi.dialogue.planner import PROMPT_VERSION, SemanticResolver
from src.vivi.dialogue.reducer import (
    clear_journey,
    expire,
    prepare_step,
    record_navigation,
    record_step,
    record_trip_plan,
    reduce_turn,
    upgrade_contract,
)
from src.vivi.graphs.concurrency import PriorityLock, SupersededTurnError, event_priority, external_read, writer_context
from src.vivi.navigation.presentation import result_messages
from src.vivi.navigation.trips import TripWorkflow
from src.vivi.navigation.workflow import NavigationWorkflow
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
    def __init__(self, compiled, saver, profile, control, vehicle, runtime_epoch, energy):
        self.compiled, self.saver, self.profile = compiled, saver, profile
        self.control, self.vehicle, self.runtime_epoch = control, vehicle, runtime_epoch
        self.energy = energy
        self._generations = {}
        self._latest_sessions = {}
        self._pending_telemetry = {}
        self._gear_requests = {}
        self.default_provider = "rules"
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

    def thread_lock(self, config):
        return self._thread_locks.setdefault(config["configurable"]["thread_id"], PriorityLock())

    def generation(self, config):
        return self._generations.get(config["configurable"]["thread_id"], 0)

    def dialogue_lock(self, config):
        return self._dialogue_locks.setdefault(config["configurable"]["thread_id"], asyncio.Lock())

    async def reconcile_vehicle(self, session, session_id):
        nav = session.navigation
        vehicle = await self.vehicle.get_state(session_id)
        if nav.status == "running" and not vehicle.driving:
            if (
                vehicle.state_version == 0
                and nav.simulated_soc is not None
                and self.energy.profile
                and hasattr(self.vehicle, "set_energy")
            ):
                await asyncio.to_thread(
                    self.vehicle.set_energy,
                    session_id,
                    nav.simulated_soc,
                    nav.simulated_soc / self.energy.soc_cost(1000),
                )
            nav.resume_status, nav.status = nav.status, "paused"
            session = record_navigation(session, nav.model_dump(mode="json"))
        return session

    async def snapshot(self, session_id):
        config = self.config({"session_id": session_id})
        async with self.thread_lock(config):
            snapshot = await self.compiled.aget_state(config)
            session = SessionState.model_validate(snapshot.values.get("session") or {})
            observed = await self.reconcile_vehicle(session, session_id)
            if observed is not session:
                session = observed
                session.snapshot_sequence += 1
                await self.compiled.aupdate_state(
                    config, {"session": session.model_dump(mode="json")}, as_node="await_input"
                )
                self._generations[config["configurable"]["thread_id"]] = self.generation(config) + 1
            return {
                "runtime_epoch": self.runtime_epoch,
                "snapshot_sequence": session.snapshot_sequence,
                "event_id": None,
                "vehicle_state": (await self.vehicle.get_state(session_id)).model_dump(mode="json"),
                "navigation": session.navigation.model_dump(mode="json"),
                "navigation_workspace": session.navigation_workspace.model_dump(mode="json"),
                "trip_plan": session.trip_plan.model_dump(mode="json") if session.trip_plan else None,
                "ui_events": [],
                "control_receipts": session.control_receipts,
            }

    async def ainvoke(self, state):
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
                }
                self._pending_telemetry[telemetry_key] = batch
            else:
                if value["id"] not in batch["ids"]:
                    batch["event"] = event
                    batch["elapsed"][value["id"]] = value.get("elapsed_s") or 0
                batch["ids"].add(value["id"])
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
        lock = self.thread_lock(config)
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
                            )
                        result = dict(batch["result"])
                        result["output"] = {**result["output"], "turn_id": event["turn_id"]}
                        result["output"]["diagnostics"] = {
                            **result["output"]["diagnostics"],
                            "coalesced_events": batch["count"],
                        }
                        return self.project(result)
                    return await self._invoke_locked(event, config, queued, gear_order)
                finally:
                    writer_context.reset(token)
        finally:
            event_priority.reset(priority_token)

    async def _invoke_locked(self, event, config, queued, gear_order=None, coalesced_ids=None, coalesced_elapsed=None):
        snapshot = await self.compiled.aget_state(config)
        old = snapshot.values.get("event", {})
        if (
            old.get("turn_id") == event["turn_id"]
            and snapshot.values.get("output", {}).get("turn_id") == event["turn_id"]
        ):
            if old != event:
                raise ValueError("turn_id đã được dùng cho một yêu cầu khác.")
            return self.project(snapshot.values)
        if (event.get("navigation_event") or {}).get("kind") in CONTROL_EVENTS:
            original_event = event
            previous = dict(snapshot.values)
            previous["session"] = (
                await self.reconcile_vehicle(
                    SessionState.model_validate(previous.get("session") or {}), event["session_id"]
                )
            ).model_dump(mode="json")
            if event["navigation_event"]["kind"] == "charge" and coalesced_elapsed:
                applied = set(previous["session"]["navigation_workspace"]["applied_events"])
                remaining = {key: elapsed for key, elapsed in coalesced_elapsed.items() if key not in applied}
                if remaining:
                    event = {
                        **event,
                        "navigation_event": {
                            **event["navigation_event"],
                            "id": next(reversed(remaining)),
                            "elapsed_s": min(30, sum(remaining.values())),
                        },
                    }
            latest_gear = self._gear_requests.get(config["configurable"]["thread_id"])
            superseded = bool(
                gear_order
                and event["navigation_event"].get("gear") == "D"
                and latest_gear
                and latest_gear[1] == "P"
                and latest_gear[0] > gear_order
            )
            result = await self.control(event, previous, queued, superseded=superseded)
            result["event"] = original_event
            if coalesced_ids:
                applied = result["session"]["navigation_workspace"]["applied_events"]
                result["session"]["navigation_workspace"]["applied_events"] = list(
                    dict.fromkeys([*applied, *sorted(coalesced_ids)])
                )[-100:]
                result["output"]["navigation"]["applied_events"] = result["session"]["navigation_workspace"][
                    "applied_events"
                ]
                result["output"]["navigation_workspace"] = result["session"]["navigation_workspace"]
            await self.compiled.aupdate_state(config, result, as_node="await_input")
            key = config["configurable"]["thread_id"]
            self._latest_sessions[key] = result["session"]
            telemetry = event["navigation_event"]["kind"] in {"progress", "charge"}
            before = SessionState.model_validate(snapshot.values.get("session") or {}).navigation.status
            after = SessionState.model_validate(result["session"]).navigation.status
            if not telemetry or before != after:
                self._generations[key] = self.generation(config) + 1
            return self.project(result)
        try:
            result = await self.compiled.ainvoke(await self.input(event, config), config, durability="sync")
        except SupersededTurnError:
            latest = await self.compiled.aget_state(config)
            result = await self.control(event, latest.values, queued, superseded=True)
            await self.compiled.aupdate_state(config, result, as_node="await_input")
        self._generations[config["configurable"]["thread_id"]] = self.generation(config) + 1
        return self.project(result)

    async def astream(self, state, stream_mode):
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
                        yield item
            except SupersededTurnError:
                latest = await self.compiled.aget_state(config)
                result = await self.control(event, latest.values, time.perf_counter(), superseded=True)
                await self.compiled.aupdate_state(config, result, as_node="await_input")
                yield ("values", result)
            finally:
                writer_context.reset(token)
            self._generations[config["configurable"]["thread_id"]] = self.generation(config) + 1

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
    runtime_epoch = str(uuid4())
    resolver = resolver or SemanticResolver()
    runtime = CapabilityRuntime(
        services, store, generators, budgets, resolver.breaker_seconds, provider_chains=resolver.fallback_chains
    )
    navigation = NavigationWorkflow(services, store, resolver)
    trips = TripWorkflow(services, store, resolver)
    profile = services.memory_profile_id

    async def context(state):
        event = TurnEvent.model_validate(state["event"])
        previous = SessionState.model_validate(state.get("session") or {})
        session = expire(previous)
        legacy = state.get("session") or {}
        if "navigation" in legacy and "navigation_workspace" not in legacy:
            nav = session.navigation
            nav.pending, nav.clarification, nav.candidates, nav.offer = None, None, [], None
            if nav.status in {"running", "charging"}:
                nav.resume_status, nav.status = nav.status, "paused"
            session.navigation = nav
            if session.trip_plan:
                session.trip_plan.offer = None
            for task in [session.active, *session.suspended]:
                if task:
                    for step in task.steps:
                        if step.draft.capability.startswith(("navigation.", "trip.")) and step.status != "completed":
                            step.status, step.confirmation = "cancelled", None
        session = upgrade_contract(session)
        nav = session.navigation
        if nav.status == "running":
            observed = await services.action_gateway.vehicle.get_state(event.session_id)
            if not observed.driving:
                if (
                    observed.state_version == 0
                    and hasattr(services.action_gateway.vehicle, "set_energy")
                    and nav.simulated_soc is not None
                    and navigation.energy.profile
                ):
                    await asyncio.to_thread(
                        services.action_gateway.vehicle.set_energy,
                        event.session_id,
                        nav.simulated_soc,
                        nav.simulated_soc / navigation.energy.soc_cost(1000),
                    )
                nav.status, nav.resume_status = "paused", "running"
                session = record_navigation(session, nav.model_dump(mode="json"))
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
            nav = session.navigation
            candidate = (nav.pending or {}).get("navigation") or nav.model_dump(mode="json")
            offer = candidate.get("offer") or {}
            if event.confirmation_id == (offer.get("grant") or {}).get(
                "confirmation_id"
            ) and event.confirmation_decision in {"approve", "deny"}:
                semantic = SemanticTurn(
                    act="request",
                    actions=[
                        {
                            "capability": "navigation.start"
                            if event.confirmation_decision == "approve"
                            else "navigation.cancel",
                            "arguments": {"route_revision": nav.revision},
                        }
                    ],
                )
            elif grant is None or event.confirmation_id != grant["confirmation_id"]:
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
            semantic, served, attempts = await external_read(
                resolver.resolve(
                    event.text,
                    session,
                    requested=event.provider,
                    history=turn["history"],
                    memory=turn["memory"],
                    catalog=catalog(services.navigation is not None),
                )
            )
        # Domain invalidity is a clarification, never a reason to ask a weaker model.
        if any(action.capability not in REGISTRY for action in semantic.drafts()):
            semantic = SemanticTurn(act="unsupported", text="Mình chưa hỗ trợ thao tác đó.")
        turn.update(semantic=semantic.model_dump(mode="json"), effective_provider=served, provider_attempts=attempts)
        return {"turn": turn}

    async def transition(state):
        previous = SessionState.model_validate(state["session"])
        semantic = SemanticTurn.model_validate(state["turn"]["semantic"])
        reducer_input = previous
        # A typed map event can create a handle and request its selection in
        # the same turn. Validate that action against the accepted event state,
        # not the candidate list from before the event.
        update = state["turn"].get("navigation_result", {})
        if update.get("navigation_update"):
            reducer_input = record_navigation(previous, update["navigation_update"])
        if update.get("clear_navigation_history"):
            reducer_input.navigation_workspace.history = []
        if update.get("clear_journey"):
            reducer_input = clear_journey(reducer_input)
        session, message = reduce_turn(
            reducer_input, semantic, revoke_consent_on_clarify=state["event"]["kind"] == "utterance"
        )
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
                result = await external_read(
                    runtime.handbook(event, arguments, turn["effective_provider"], turn["history"])
                )
            else:
                workflow = trips if step.draft.capability.startswith("trip.") else navigation
                call = workflow.execute(event, session, step, arguments, command_id, turn["effective_provider"])
                result = (
                    await external_read(call)
                    if step.draft.capability.rsplit(".", 1)[-1] in {"plan", "search", "select", "refine"}
                    else await call
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
            result = {
                "status": "unknown",
                "message": "Mình chưa xác minh được kết quả bước này. Bạn kiểm tra trước khi yêu cầu lại nhé.",
            }
            turn["errors"] = [
                *turn["errors"],
                {"stage": "capability", "error_type": type(exc).__name__, "message": str(exc)},
            ]
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
        session.snapshot_sequence += 1
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
        if last.get("status") == "completed" and current is not None and event.kind != "navigation":
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
            navigation_workspace=session.navigation_workspace.model_dump(mode="json"),
            navigation=session.navigation.model_dump(mode="json"),
            trip_plan=session.trip_plan.model_dump(mode="json") if session.trip_plan else None,
            ui_events=[item for result in results for item in result.get("ui_events", [])],
        )
        return {"output": output.model_dump(mode="json"), "session": session.model_dump(mode="json")}

    async def control(event_data, previous, queued, superseded=False):
        started = time.perf_counter()
        event = TurnEvent.model_validate(event_data)
        session = SessionState.model_validate(previous.get("session") or {})
        before = session.navigation.status
        if superseded:
            for task in [session.active, *session.suspended]:
                if task:
                    for step in task.steps:
                        if step.status == "running" and step.draft.capability.startswith(("navigation.", "trip.")):
                            step.status, step.command_id = "pending", None
            result = {
                "status": "blocked",
                "message": "Trạng thái hành trình vừa thay đổi. Bạn yêu cầu lại nhé.",
                "error_code": "superseded_turn",
            }
        else:
            result = await navigation.event(event, session)
            if result.get("navigation_update"):
                session = record_navigation(session, result["navigation_update"])
            if result.get("clear_journey"):
                session = clear_journey(session)
            if result.get("clear_navigation_history"):
                session.navigation_workspace.history = []
        if event.navigation_event and event.navigation_event.get("kind") not in {"progress", "charge"}:
            session.control_receipts = [
                *session.control_receipts[-31:],
                {
                    "event_id": event.navigation_event["id"],
                    "kind": event.navigation_event["kind"],
                    "gear": event.navigation_event.get("gear"),
                    "status": result["status"],
                    "snapshot_sequence": session.snapshot_sequence + 1,
                },
            ]
        result["vehicle_state"] = (await services.action_gateway.vehicle.get_state(event.session_id)).model_dump(
            mode="json"
        )
        elapsed = round((time.perf_counter() - started) * 1000, 2)
        queue_ms = round((started - queued) * 1000, 2)
        turn = {
            "semantic": SemanticTurn(act="reply").model_dump(mode="json"),
            "results": [],
            "navigation_result": result,
            "message": result.get("message", ""),
            "errors": [],
            "timings": {"queue": queue_ms, "control": elapsed},
            "spans": [],
            "effective_provider": "event",
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
            context = writer_context.get()
            if context:
                owner, config, _ = context
                latest = owner._latest_sessions.get(config["configurable"]["thread_id"])
                if latest:
                    current = SessionState.model_validate(latest)
                    updated = SessionState.model_validate(result.get("session", state.get("session", {})))
                    if current.snapshot_sequence > updated.snapshot_sequence:
                        old_journey, new_journey = (
                            current.navigation_workspace.journey,
                            updated.navigation_workspace.journey,
                        )
                        if old_journey and new_journey and old_journey.route_id == new_journey.route_id:
                            updated.navigation_workspace.journey = old_journey.model_copy(deep=True)
                        updated.navigation_workspace.event_sequence = max(
                            updated.navigation_workspace.event_sequence, current.navigation_workspace.event_sequence
                        )
                        updated.snapshot_sequence = current.snapshot_sequence
                        updated.control_receipts = current.control_receipts
                        result["session"] = updated.model_dump(mode="json")
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
    return AssistantGraph(
        graph.compile(checkpointer=saver),
        saver,
        profile,
        control,
        services.action_gateway.vehicle,
        runtime_epoch,
        navigation.energy,
    )
