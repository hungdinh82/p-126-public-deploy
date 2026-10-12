"""Trip calculations share navigation services, never mutate a running route."""

from __future__ import annotations

import asyncio
import logging
import math

from src.vivi.dialogue.contracts import ActionDraft, NavigationState, TripPlanState
from src.vivi.navigation.presentation import route_summary
from src.vivi.navigation.vietmap import VietMapError
from src.vivi.navigation.workflow import NavigationWorkflow, coordinates, current_origin
from src.vivi.persistence.runtime_store import fingerprint

logger = logging.getLogger(__name__)


class TripWorkflow(NavigationWorkflow):
    async def preview(self, event, nav, step, arguments, command_id, previous=None, *, brief=False):
        nav.summary = route_summary(nav, ask_start=False)
        speech = nav.summary["speech"]
        complete = (nav.energy or {}).get("selected_plan") is not None
        if not complete:
            if not (nav.energy or {}).get("message"):
                speech += " Kế hoạch chưa hoàn chỉnh."
        return self.result(nav, speech, "completed", "trip.summary")

    @staticmethod
    def project(plan, result):
        update = result.pop("navigation_update", None)
        if update:
            for field in type(plan).model_fields:
                if field in update:
                    setattr(plan, field, update[field])
        complete = bool((plan.energy or {}).get("selected_plan")) and not plan.clarification
        plan.calculation_status = (
            "ready" if complete else "draft" if plan.clarification or not plan.selected else "incomplete"
        )
        if result["status"] == "completed" and complete:
            plan.offer = {
                "capability": "trip.start",
                "plan_id": plan.id,
                "route_revision": plan.revision,
                "question": "Bạn muốn bắt đầu hành trình này không?",
                "status": "pending",
            }
        else:
            plan.offer = None
        result["trip_plan_update"] = plan.model_dump(mode="json")
        result["ui_events"] = [
            {**e, "type": e["type"].replace("navigation.", "trip."), "plan_id": plan.id, "revision": plan.revision}
            for e in result.get("ui_events", [])
        ]
        return result

    async def execute(self, event, session, step, arguments, command_id, effective_provider="rules"):
        name = step.draft.capability
        plan = session.trip_plan.model_copy(deep=True) if session.trip_plan else TripPlanState()
        if name != "trip.plan" and (
            arguments.get("plan_id") != plan.id or arguments.get("route_revision") != plan.revision
        ):
            return {
                "status": "blocked",
                "message": "Kế hoạch đã thay đổi. Bạn chọn lại nhé.",
                "error_code": "stale_plan",
            }
        if name == "trip.cancel":
            plan.revision += 1
            plan.calculation_status = "cancelled"
            plan.offer, plan.clarification, plan.candidates = None, None, []
            return {
                "status": "completed",
                "message": "Đã huỷ kế hoạch.",
                "trip_plan_update": plan.model_dump(mode="json"),
                "ui_events": [{"type": "trip.cancel", "plan_id": plan.id, "revision": plan.revision}],
            }
        if name == "trip.decline":
            if plan.offer:
                plan.offer["status"] = "declined"
            return {
                "status": "completed",
                "message": "Mình giữ kế hoạch để bạn xem lại.",
                "trip_plan_update": plan.model_dump(mode="json"),
            }
        if name == "trip.start":
            if (
                plan.clarification
                and plan.clarification.get("phase") == "change_destination"
                and plan.selected
                and (plan.energy or {}).get("selected_plan")
            ):
                plan.clarification = None
                plan.calculation_status = "ready"
                plan.offer = {
                    "capability": "trip.start",
                    "plan_id": plan.id,
                    "route_revision": plan.revision,
                    "question": "Bạn muốn bắt đầu hành trình này không?",
                    "status": "pending",
                }
            return await self.start_plan(event, session, plan, arguments)
        if name == "trip.plan":
            # A new request gets a new identity; slot resumption retains its draft.
            resuming = step.result and step.result.get("status") == "waiting_slot"
            if not resuming:
                plan = TripPlanState(origin=event.position or await current_origin(session.navigation))
        if not plan.origin and session.navigation.origin:
            plan.origin = session.navigation.origin
        scratch = session.model_copy(deep=True)
        scratch.navigation = NavigationState.model_validate(plan.model_dump(include=set(NavigationState.model_fields)))
        mapped = step.model_copy(deep=True)
        args = {k: v for k, v in arguments.items() if k not in {"plan_id", "route_revision"}}
        mapped.draft = ActionDraft(capability=name.replace("trip.", "navigation."), arguments=args)
        result = await super().execute(event, scratch, mapped, args, command_id, effective_provider)
        plan.calculation_basis = self.calculation_basis()
        return self.project(plan, result)

    def calculation_basis(self):
        assumptions = getattr(self.discovery, "assumptions", None)
        return fingerprint(
            {
                "profile": self.energy.profile.model_dump(mode="json") if self.energy.profile else None,
                "assumptions": assumptions.model_dump(mode="json") if assumptions else None,
                "charging_algorithm": "corridor-coverage-2",
            }
        )

    async def start_plan(self, event, session, plan, arguments):
        stable_command = f"{plan.id}:r{plan.revision}:start"
        if self.ledger:
            cached = await asyncio.to_thread(self.ledger.command_result, event.session_id, stable_command)
            if cached:
                # Do not replace a newer active route with an old cached projection.
                known = cached["result"]
                if (
                    session.navigation.revision < known.get("navigation_update", {}).get("revision", 0)
                    and plan.started_revision is None
                ):
                    plan.started_revision = plan.revision
                    plan.offer["status"] = "accepted"
                    return {**known, "trip_plan_update": plan.model_dump(mode="json")}
                return {"status": "completed", "message": "Kế hoạch này đã được bắt đầu.", "verified": True}
        if plan.started_revision == plan.revision:
            return {"status": "completed", "message": "Kế hoạch này đã được bắt đầu."}
        if plan.trip_kind is None or plan.calculation_status != "ready" or not plan.offer or plan.clarification:
            return {
                "status": "blocked",
                "message": "Bạn hoàn tất điểm đến và kế hoạch trước nhé.",
                "error_code": "plan_not_ready",
            }
        observed = await self.services.action_gateway.vehicle.get_state(event.session_id)
        origin = event.position or await current_origin(session.navigation) or plan.origin
        moved = (
            origin
            and math.hypot((origin["lat"] - plan.origin["lat"]) * 111000, (origin["lng"] - plan.origin["lng"]) * 103000)
            > 100
        )
        changed = (
            moved
            or plan.initial_soc is None
            or abs(observed.battery_percent - plan.initial_soc) >= 1
            or plan.calculation_basis != self.calculation_basis()
        )
        if changed:
            nav = NavigationState.model_validate(plan.model_dump(include=set(NavigationState.model_fields)))
            nav.origin = origin
            try:
                outbound = await self.services.navigation.route(coordinates(origin), coordinates(nav.selected))
                nav.waypoint_routes = [outbound.model_dump()]
                if nav.trip_kind == "round_trip":
                    returning = await self.services.navigation.route(coordinates(nav.selected), coordinates(origin))
                    nav.waypoint_routes.append(returning.model_dump())
                nav.route = self.compose_route(
                    {"legs": nav.waypoint_routes, "distance_m": sum(r["distance_m"] for r in nav.waypoint_routes)}
                )
                nav.initial_soc = observed.battery_percent
                nav.energy, nav.alternatives = await self.plan_energy(nav, observed.battery_percent, "rules")
                if (nav.energy or {}).get("selected_plan"):
                    nav.route = self.compose_route(nav.energy["selected_plan"])
                nav.revision += 1
                result = await self.preview(event, nav, None, arguments, stable_command)
                result["message"] = "Vị trí hoặc pin đã thay đổi; mình cập nhật kế hoạch. " + result["message"]
                plan.calculation_basis = self.calculation_basis()
                return self.project(plan, result)
            except (VietMapError, TimeoutError):
                logger.warning(
                    "Trip replan unavailable session=%s turn=%s", event.session_id, event.turn_id, exc_info=True
                )
                plan.calculation_status, plan.offer = "incomplete", None
                return {
                    "status": "completed",
                    "message": "Chưa cập nhật được kế hoạch theo vị trí/pin mới. Bạn tính lại nhé.",
                    "trip_plan_update": plan.model_dump(mode="json"),
                }
        # Explicit voice/UI acceptance is consent to this exact server-owned offer.
        # Issue and consume a single-use grant at this point, not during calculation.
        binding = {
            "capability": "trip.start",
            "plan_id": plan.id,
            "revision": plan.revision,
            "command_id": stable_command,
        }
        grants = self.services.action_gateway.confirmations
        grant = await asyncio.to_thread(grants.create_generic, event.session_id, binding, plan.offer["question"])
        if not await asyncio.to_thread(grants.consume_generic, event.session_id, grant["confirmation_id"], binding):
            return {"status": "blocked", "message": "Bạn xác nhận bắt đầu lại nhé.", "error_code": "stale_plan"}
        nav = NavigationState.model_validate(plan.model_dump(include=set(NavigationState.model_fields)))
        # Navigation revisions are independent of the plan's revision.
        nav.revision = session.navigation.revision + 1
        result = await super().start(
            event,
            nav,
            stable_command,
            arguments,
            "trip.start",
            keep_parked=session.navigation.status == "paused" and session.navigation.resume_status is not None,
        )
        if result["status"] == "completed":
            plan.started_revision = plan.revision
            plan.offer["status"] = "accepted"
        result["trip_plan_update"] = plan.model_dump(mode="json")
        if self.ledger and result.get("verified"):
            await asyncio.to_thread(self.ledger.finish_command, event.session_id, stable_command, result)
        return result
