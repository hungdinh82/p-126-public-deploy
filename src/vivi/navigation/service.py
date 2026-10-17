"""One navigation writer for voice, touch, telemetry and recovery."""

from __future__ import annotations

import asyncio
import inspect
import json
import time
from contextvars import ContextVar
from dataclasses import replace
from weakref import WeakValueDictionary

from src.vivi.graphs.concurrency import PriorityLock, SupersededTurnError
from src.vivi.navigation.effects import vehicle_effect
from src.vivi.navigation.metrics import MeasuredEnergy, navigation_routes, navigation_timings, record
from src.vivi.navigation.models import ActiveJourney, PlanDraft, clear_navigation, record_navigation
from src.vivi.navigation.store import DOMAIN_FIELDS, NavigationRepository, domain_payload
from src.vivi.navigation.transitions import CONTROL_KINDS, NavigationTransitions
from src.vivi.navigation.vietmap import VietMapError
from src.vivi.navigation.workflow import NavigationEvent, NavigationWorkflow
from src.vivi.persistence.runtime_store import fingerprint

navigation_writer = ContextVar("navigation_writer", default=None)


class ReadClient:
    """Release the domain writer during upstream I/O and fence stale results."""

    def __init__(self, client):
        self.client = client

    def __getattr__(self, name):
        method = getattr(self.client, name)
        if not inspect.iscoroutinefunction(method):
            return method

        async def read(*args, **kwargs):
            return await self._read(self._measured(name, args, kwargs))

        return read

    async def _measured(self, name, args, kwargs):
        cache = navigation_routes.get() if name == "route" else None
        identity = fingerprint({"args": args, "kwargs": kwargs}) if cache is not None else None
        if cache is not None and identity in cache:
            return cache[identity]
        started = time.perf_counter()
        try:
            result = await getattr(self.client, name)(*args, **kwargs)
            if cache is not None:
                cache[identity] = result
            return result
        finally:
            record("navigation_" + ("place_detail" if name == "place" else name), started)

    async def _read(self, awaitable):
        context = navigation_writer.get()
        if context is None:
            return await awaitable
        owner, key, lock = context
        generation = owner.generation(key)
        lock.release()
        try:
            result = await awaitable
        finally:
            await lock.acquire()
        if owner.generation(key) != generation:
            raise SupersededTurnError("Navigation changed while upstream I/O was pending")
        return result

    async def batch(self, operations):
        """A read batch releases/fences the writer once, never once per child."""
        semaphore = asyncio.Semaphore(3)

        async def single(operation):
            async with semaphore:
                name, args, kwargs = operation
                return await self._measured(name, args, kwargs)

        async def collect():
            # TaskGroup drains children on cancellation before reacquiring lock.
            tasks = []

            async def outcome(operation):
                try:
                    return await single(operation)
                except (VietMapError, TimeoutError) as exc:
                    return exc

            async with asyncio.TaskGroup() as group:
                tasks = [group.create_task(outcome(operation)) for operation in operations]
            return [task.result() for task in tasks]

        return await self._read(collect())

    async def run_read(self, awaitable):
        """Discovery owns its child HTTP requests; fence the whole read phase."""
        started = time.perf_counter()
        try:
            return await self._read(awaitable)
        finally:
            record("navigation_charging_discovery", started)


class NavigationService:
    def __init__(self, services, ledger, saver, resolver, epoch):
        self.repository = NavigationRepository(ledger)
        self.repository.migrate(saver)
        self.epoch, self.profile = epoch, services.memory_profile_id
        self.vehicle = services.action_gateway.vehicle
        self.energy = services.energy_planner
        self._locks = WeakValueDictionary()
        self._cache, self._generations, self._recovered = {}, {}, set()
        adapted = replace(services, navigation=ReadClient(services.navigation) if services.navigation else None)
        self.workflow = NavigationWorkflow(adapted, ledger, resolver)
        self.workflow.energy = MeasuredEnergy(self.workflow.energy)
        self.transitions = NavigationTransitions(self.vehicle, self.workflow.energy)

    def key(self, session_id):
        return "conversation:4:" + fingerprint({"profile": self.profile, "session": session_id})

    def lock(self, key):
        return self._locks.setdefault(key, PriorityLock())

    def generation(self, key):
        return self._generations.get(key, 0)

    async def hydrate(self, session, session_id):
        key = self.key(session_id)
        if key not in self._cache:
            stored = await self.repository.run(self.repository.load_payload, key)
            self._cache[key] = stored or json.dumps(domain_payload(session), ensure_ascii=False)
        # Domain values are replaced from the repository. Do not copy and
        # serialize the old geometry first; isolate the cached dictionaries.
        # Cache immutable JSON; parsing it isolates mutable state considerably
        # faster than recursively deep-copying duplicated route point arrays.
        data = {**session.model_dump(mode="json", exclude=DOMAIN_FIELDS), **json.loads(self._cache[key])}
        return type(session).model_validate(data)

    async def save(self, session, session_id, *, receipt=None, invalidate=False):
        key = self.key(session_id)
        self._cache[key] = await self.repository.run(self.repository.save, key, session, receipt)
        if invalidate:
            self._generations[key] = self.generation(key) + 1
        return session

    async def recover(self, session, session_id):
        key = self.key(session_id)
        if key in self._recovered:
            return session
        journey = session.navigation_workspace.journey
        observed = await self.vehicle.get_state(session_id)
        if await self.repository.run(self.repository.reset_park, key):
            if observed.driving and hasattr(self.vehicle, "set_driving"):
                await vehicle_effect(self.vehicle.set_driving, session_id, False)
            await self.repository.run(lambda: self.repository.reset_park(key, complete=True))
        if journey and journey.status in {"running", "charging"}:
            if (
                observed.state_version == 0
                and journey.simulated_soc is not None
                and self.workflow.energy.profile
                and hasattr(self.vehicle, "set_energy")
            ):
                await asyncio.to_thread(
                    self.vehicle.set_energy,
                    session_id,
                    journey.simulated_soc,
                    journey.simulated_soc / self.workflow.energy.soc_cost(1000),
                )
            if hasattr(self.vehicle, "set_driving"):
                await vehicle_effect(self.vehicle.set_driving, session_id, False)
            journey.resume_status, journey.status = journey.status, "paused"
            session.snapshot_sequence += 1
            await self.save(session, session_id, invalidate=True)
        elif (
            observed.driving
            and hasattr(self.vehicle, "set_driving")
            and await self.repository.run(self.repository.has_drive_receipt, key)
        ):
            await vehicle_effect(self.vehicle.set_driving, session_id, False)
            session.snapshot_sequence += 1
            await self.save(session, session_id, invalidate=True)
        self._recovered.add(key)
        return session

    async def synchronize(self, session, session_id):
        session = await self.hydrate(session, session_id)
        return await self.recover(session, session_id)

    async def reconcile(self, session_id, result):
        from src.vivi.navigation.models import NavigationDomain

        nav = result.get("navigation_update")
        arguments = (result.get("action") or {}).get("arguments", {})
        if not result.get("verified") or not nav:
            return
        key = self.key(session_id)
        async with self.lock(key):
            domain = await self.hydrate(NavigationDomain(), session_id)
            plan = domain.navigation_workspace.draft
            if not plan or plan.id != arguments.get("plan_id") or plan.revision != arguments.get("route_revision"):
                return
            if plan.started_revision == plan.revision:
                return
            domain.navigation_workspace.journey = ActiveJourney.model_validate(nav)
            plan.started_revision = plan.revision
            if plan.offer:
                plan.offer["status"] = "accepted"
            domain.snapshot_sequence += 1
            await self.save(domain, session_id, invalidate=True)

    async def command(self, session_id, command):
        from src.vivi.navigation.models import NavigationDomain

        key = self.key(session_id)
        async with self.lock(key):
            domain = await self.hydrate(NavigationDomain(), session_id)
            draft = domain.navigation_workspace.draft
            if command["kind"] == "cancel_draft" and draft and command.get("plan_id") == draft.id:
                domain.navigation_workspace.draft = None
                domain.navigation_workspace.revision += 1
                domain.snapshot_sequence += 1
                await self.save(domain, session_id, invalidate=True)

    async def allocate_snapshot(self, session, session_id):
        key = self.key(session_id)
        async with self.lock(key):
            session = await self.hydrate(session, session_id)
            session.snapshot_sequence += 1
            return await self.save(session, session_id)

    @staticmethod
    def apply(session, result):
        if result.get("draft_update"):
            session.navigation_workspace.draft = PlanDraft.model_validate(result["draft_update"])
            session.navigation_workspace.revision = max(
                session.navigation_workspace.revision, session.navigation_workspace.draft.revision
            )
        if result.get("journey_update"):
            session.navigation_workspace.journey = ActiveJourney.model_validate(result["journey_update"])
        if result.get("clear_draft"):
            session.navigation_workspace.draft = None
            session.navigation_workspace.revision += 1
        if result.get("navigation_update"):
            session = record_navigation(session, result["navigation_update"])
        if result.get("clear_journey"):
            session = clear_navigation(session)
        if result.get("clear_navigation_history"):
            session.navigation_workspace.history = []
        return session

    async def draft_event(self, event, session, value):
        from types import SimpleNamespace
        from uuid import uuid4

        from src.vivi.navigation.models import NavigationState
        from src.vivi.navigation.presentation import route_summary
        from src.vivi.navigation.workflow import current_origin

        workspace = session.navigation_workspace
        plan = workspace.draft
        kind = value["kind"]
        if kind == "position":
            workspace.origin = value["position"]
            if plan:
                plan.origin = value["position"]
                plan.revision += 1
                plan.offer = None
                plan.phase = "needs_destination" if not plan.selected else "incomplete"
            return {
                "status": "completed",
                "message": "Đã cập nhật điểm xuất phát.",
                "domain_applied": True,
                "draft_update": plan.model_dump(mode="json") if plan else None,
            }
        if kind == "point":
            plan = PlanDraft(
                origin=workspace.origin or await current_origin(session.navigation),
                request_id="nav:" + str(uuid4()),
                revision=workspace.revision + 1,
                status="selecting",
            )
            point = value["position"]
            place = {
                **point,
                "handle": "place:" + str(uuid4()),
                "name": point.get("label") or "Điểm trên bản đồ",
                "address": "Điểm được chọn trên bản đồ",
            }
            plan.candidates = [place]
            return {
                "status": "completed",
                "message": "",
                "draft_update": plan.model_dump(mode="json"),
                "request_action": {
                    "capability": "navigation.select",
                    "arguments": {"plan_id": plan.id, "route_revision": plan.revision, "place_handle": place["handle"]},
                },
            }
        alternative = next((p for p in plan.alternatives if p["id"] == value["plan_id"]), None) if plan else None
        if not alternative or not plan.route:
            return {
                "status": "blocked",
                "message": "Phương án không thuộc kế hoạch hiện tại.",
                "error_code": "stale_plan",
            }
        plan.revision += 1
        plan.route_id = "route:" + str(uuid4())
        plan.energy["selected_plan"] = alternative
        plan.route = self.workflow.compose_route(alternative)
        plan.summary = route_summary(plan, ask_start=False)
        nav = NavigationState.model_validate(plan.model_dump(mode="json"))
        result = await self.workflow.preview(
            event,
            nav,
            SimpleNamespace(draft=SimpleNamespace(capability="navigation.plan")),
            {"plan_id": plan.id, "route_revision": plan.revision},
            value["id"],
        )
        plan.offer = result["navigation_update"]["offer"]
        return {
            "status": "completed",
            "message": "Đã đổi phương án. Bạn xác nhận lại tuyến nhé.",
            "draft_update": plan.model_dump(mode="json"),
        }

    async def event_locked(self, event, session, *, superseded=False, coalesced=None):
        key = self.key(event.session_id)
        session = await self.synchronize(session, event.session_id)
        value = NavigationEvent.model_validate(event.navigation_event).model_dump(mode="json")
        identifiers = {
            identifier: NavigationEvent.model_validate(member).model_dump(mode="json")
            for identifier, member in (coalesced or {value["id"]: value}).items()
        }
        replay, fresh = {}, {}
        for identifier, member in identifiers.items():
            known = await self.repository.run(self.repository.reserve, key, member, session.control_revision)
            if known:
                replay[identifier] = known
            else:
                fresh[identifier] = member
        if not fresh:
            receipt = replay[value["id"]]
            return {
                "status": receipt["status"],
                "message": receipt.get("message", ""),
                "receipt": receipt,
                "error_code": receipt.get("error_code"),
                "replay": True,
            }, session
        if value["kind"] == "charge":
            value = {
                **value,
                "id": next(reversed(fresh)),
                "elapsed_s": min(30, sum(member["elapsed_s"] for member in fresh.values())),
            }
            event = event.model_copy(update={"navigation_event": value})
        error = None
        if value.get("runtime_epoch") and value["runtime_epoch"] != self.epoch:
            error = "stale_epoch"
        elif (
            value.get("target_id")
            and value["kind"] != "gear"
            and value["target_id"]
            not in {
                session.navigation_workspace.journey.route_id if session.navigation_workspace.journey else None,
                session.navigation_workspace.draft.request_id if session.navigation_workspace.draft else None,
                session.navigation_workspace.draft.id if session.navigation_workspace.draft else None,
            }
        ):
            error = "stale_target"
        elif not await self.repository.run(self.repository.accept_sequence, key, value) or superseded:
            error = "superseded_control"
        elif (
            value.get("gear") == "D"
            and value.get("expected_control_revision") is not None
            and value["expected_control_revision"] != session.control_revision
        ):
            error = "stale_control"
        elif value.get("gear") == "D" and value.get("expected_vehicle_version") is not None:
            observed = await self.vehicle.get_state(event.session_id)
            if value["expected_vehicle_version"] != observed.state_version:
                error = "stale_vehicle"
        if error:
            result = {
                "status": "blocked",
                "message": "Trạng thái xe đã thay đổi. Bạn đồng bộ và thử lại nhé.",
                "error_code": error,
            }
        else:
            if value["kind"] in CONTROL_KINDS:
                result, session = await self.transitions.apply(event, session, value)
            elif value["kind"] in {"position", "point", "select_plan"}:
                result = await self.draft_event(event, session, value)
                session = self.apply(session, result)
            else:
                token = navigation_writer.set((self, key, self.lock(key)))
                timings = {}
                metrics_token = navigation_timings.set(timings)
                routes_token = navigation_routes.set({})
                try:
                    from types import SimpleNamespace

                    from src.vivi.navigation.models import NavigationState

                    journey = session.navigation_workspace.journey
                    nav = NavigationState.model_validate(journey.model_dump(mode="json") if journey else {})
                    nav.revision = session.navigation_workspace.revision
                    result = await self.workflow.event(event, SimpleNamespace(navigation=nav))
                except SupersededTurnError:
                    session = await self.hydrate(session, event.session_id)
                    result = {
                        "status": "blocked",
                        "message": "Trạng thái hành trình đã thay đổi trong lúc tính tuyến.",
                        "error_code": "superseded_turn",
                    }
                finally:
                    navigation_timings.reset(metrics_token)
                    navigation_routes.reset(routes_token)
                    navigation_writer.reset(token)
                result["timings"] = timings
                session = self.apply(session, result)
        workspace = session.navigation_workspace
        if result.get("navigation_update") or coalesced and result.get("error_code") != "stale_route":
            workspace.applied_events = list(dict.fromkeys([*workspace.applied_events, *fresh]))[-100:]
        session.snapshot_sequence += 1
        if value["kind"] in {"gear", "pause", "resume", "end", "clear"} and not error:
            session.control_revision += 1
        for identifier, member in fresh.items():
            receipt = {
                "event_id": identifier,
                "kind": member["kind"],
                "gear": member.get("gear"),
                "client_id": member.get("client_id"),
                "control_sequence": member.get("control_sequence"),
                "status": result["status"],
                "message": result.get("message", ""),
                "error_code": result.get("error_code"),
                "snapshot_sequence": session.snapshot_sequence,
            }
            if member["kind"] not in {"progress", "charge"}:
                session.control_receipts = [*session.control_receipts[-31:], receipt]
            await self.save(session, event.session_id, receipt=receipt)
        if value["kind"] not in {"progress", "charge"} or result.get("navigation_update", {}).get("status") in {
            "charging",
            "completed",
        }:
            self._generations[key] = self.generation(key) + 1
        result["receipt"] = await self.repository.run(self.repository.receipt, key, event.navigation_event["id"])
        return result, session

    async def event(self, event, session, **kwargs):
        async with self.lock(self.key(event.session_id)):
            return await self.event_locked(event, session, **kwargs)

    async def execute(self, event, session, step, arguments, command_id, provider):
        key = self.key(event.session_id)
        lock = self.lock(key)
        async with lock:
            session = await self.synchronize(session, event.session_id)
            name = step.draft.capability
            if name in {"navigation.set_gear", "navigation.stop", "navigation.end"}:
                value = {
                    "kind": "gear"
                    if name == "navigation.set_gear"
                    else "pause"
                    if name == "navigation.stop"
                    else "end",
                    "gear": arguments.get("gear"),
                    "id": command_id,
                    "revision": session.navigation_workspace.revision,
                }
                result, _ = await self.event_locked(event.model_copy(update={"navigation_event": value}), session)
                return result
            token = navigation_writer.set((self, key, lock))
            timings = {}
            metrics_token = navigation_timings.set(timings)
            routes_token = navigation_routes.set({})
            try:
                result = await self.workflow.execute_plan(event, session, step, arguments, command_id, provider)
                latest = await self.hydrate(session, event.session_id)
                updated = self.apply(latest, result)
                old, new = latest.navigation_workspace.journey, updated.navigation_workspace.journey
                if (
                    old
                    and new
                    and old.route_id == new.route_id
                    and latest.snapshot_sequence > session.snapshot_sequence
                ):
                    updated.navigation_workspace.journey = old.model_copy(deep=True)
                updated.snapshot_sequence = latest.snapshot_sequence + 1
                await self.save(updated, event.session_id, invalidate=True)
                result["timings"] = timings
                return result
            finally:
                navigation_timings.reset(metrics_token)
                navigation_routes.reset(routes_token)
                navigation_writer.reset(token)
