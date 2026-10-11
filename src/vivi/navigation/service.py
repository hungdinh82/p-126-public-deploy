"""One navigation writer for voice, touch, telemetry and recovery."""

from __future__ import annotations

import asyncio
import inspect
import time
from contextvars import ContextVar
from dataclasses import replace
from weakref import WeakValueDictionary

from src.vivi.dialogue.contracts import SessionState
from src.vivi.dialogue.reducer import clear_journey, record_navigation, record_trip_plan
from src.vivi.graphs.concurrency import PriorityLock, SupersededTurnError
from src.vivi.navigation.effects import vehicle_effect
from src.vivi.navigation.metrics import MeasuredEnergy, navigation_routes, navigation_timings, record
from src.vivi.navigation.store import DOMAIN_FIELDS, NavigationRepository, domain_payload
from src.vivi.navigation.transitions import CONTROL_KINDS, NavigationTransitions
from src.vivi.navigation.trips import TripWorkflow
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
        self.trips = TripWorkflow(adapted, ledger, resolver)
        self.workflow.energy = MeasuredEnergy(self.workflow.energy)
        self.trips.energy = MeasuredEnergy(self.trips.energy)
        self.transitions = NavigationTransitions(self.vehicle, self.workflow.energy)

    def key(self, session_id):
        return "conversation:3:" + fingerprint({"profile": self.profile, "session": session_id})

    def lock(self, key):
        return self._locks.setdefault(key, PriorityLock())

    def generation(self, key):
        return self._generations.get(key, 0)

    async def hydrate(self, session, session_id):
        key = self.key(session_id)
        if key not in self._cache:
            stored = await self.repository.run(self.repository.load, key)
            self._cache[key] = stored or domain_payload(session)
        state = session.model_copy(deep=True)
        data = {**state.model_dump(mode="json"), **self._cache[key]}
        return SessionState.model_validate(data)

    async def save(self, session, session_id, *, receipt=None, invalidate=False):
        key = self.key(session_id)
        await self.repository.run(self.repository.save, key, session, receipt)
        self._cache[key] = domain_payload(session)
        if invalidate:
            self._generations[key] = self.generation(key) + 1
        return session

    async def recover(self, session, session_id):
        key = self.key(session_id)
        if key in self._recovered:
            return session
        journey = session.navigation_workspace.journey
        observed = await self.vehicle.get_state(session_id)
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

    async def commit_session(self, session, before, session_id):
        """Persist only explicit domain changes from a dialogue reducer node."""
        ignored = {"snapshot_sequence", "control_revision", "control_receipts"}
        fields = DOMAIN_FIELDS - ignored
        if session.model_dump(mode="json", include=fields) == before.model_dump(mode="json", include=fields):
            return await self.hydrate(session, session_id)
        key = self.key(session_id)
        async with self.lock(key):
            latest = await self.hydrate(before, session_id)
            # A newer control may have advanced the active route while this node
            # was awaiting model/history I/O. Preserve that progress and gear.
            old, new = latest.navigation_workspace.journey, session.navigation_workspace.journey
            if old and new and old.route_id == new.route_id and latest.snapshot_sequence > before.snapshot_sequence:
                session.navigation_workspace.journey = old.model_copy(deep=True)
            session.snapshot_sequence = latest.snapshot_sequence
            session.control_receipts = latest.control_receipts
            session.control_revision = latest.control_revision
            return await self.save(session, session_id, invalidate=True)

    async def allocate_snapshot(self, session, session_id):
        key = self.key(session_id)
        async with self.lock(key):
            session = await self.hydrate(session, session_id)
            session.snapshot_sequence += 1
            return await self.save(session, session_id)

    @staticmethod
    def apply(session, result):
        if result.get("navigation_update"):
            session = record_navigation(session, result["navigation_update"])
        if result.get("trip_plan_update"):
            session = record_trip_plan(session, result["trip_plan_update"])
        if result.get("clear_journey"):
            session = clear_journey(session)
        if result.get("clear_navigation_history"):
            session.navigation_workspace.history = []
        return session

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
                session.trip_plan.id if session.trip_plan else None,
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
            else:
                token = navigation_writer.set((self, key, self.lock(key)))
                timings = {}
                metrics_token = navigation_timings.set(timings)
                routes_token = navigation_routes.set({})
                try:
                    result = await self.workflow.event(event, session)
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
                workflow = self.trips if name.startswith("trip.") else self.workflow
                result = await workflow.execute(event, session, step, arguments, command_id, provider)
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
