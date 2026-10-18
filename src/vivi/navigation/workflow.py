"""Navigation capabilities propose checkpoint updates through the reducer."""

from __future__ import annotations

import asyncio
import logging
import math
from datetime import UTC, datetime
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.vivi.navigation.categories import CATEGORIES
from src.vivi.navigation.contracts import DestinationSpec
from src.vivi.navigation.destinations import (
    administrative_layer,
    administrative_match,
    center_requested,
    region_for,
)
from src.vivi.navigation.effects import vehicle_effect
from src.vivi.navigation.energy import ChargingStation, EnergyPlanner
from src.vivi.navigation.presentation import (
    charging_message,
    choice_message,
    next_leg_message,
    place_label,
    route_summary,
    station_label,
)
from src.vivi.navigation.reads import coordinates as coordinates
from src.vivi.navigation.reads import read_budget as read_budget
from src.vivi.navigation.resolver import PlaceResolver, satisfies_area
from src.vivi.navigation.selection import decide_place
from src.vivi.navigation.vietmap import VietMapError
from src.vivi.persistence.runtime_store import fingerprint

logger = logging.getLogger(__name__)


class Position(BaseModel):
    model_config = ConfigDict(extra="forbid")
    lat: float = Field(ge=-90, le=90)
    lng: float = Field(ge=-180, le=180)
    label: str = "Vị trí của bạn"
    simulated: bool = False
    observed_at: datetime | None = None


class NavigationEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal[
        "position",
        "point",
        "progress",
        "charge",
        "select_plan",
        "pause",
        "resume",
        "clear",
        "gear",
        "end",
        "history_clear",
    ]
    id: str = Field(min_length=1, max_length=100)
    revision: int = Field(ge=0)
    position: Position | None = None
    distance_m: float | None = Field(default=None, ge=0)
    elapsed_s: float | None = Field(default=None, ge=0, le=30)
    gear: Literal["P", "D"] | None = None
    plan_id: str | None = None
    target_id: str | None = Field(default=None, max_length=100)
    client_id: str | None = Field(default=None, min_length=1, max_length=100)
    control_sequence: int | None = Field(default=None, ge=1)
    runtime_epoch: str | None = Field(default=None, max_length=100)
    expected_vehicle_version: int | None = Field(default=None, ge=0)
    expected_control_revision: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def ordered_control(self):
        if (self.client_id is None) != (self.control_sequence is None):
            raise ValueError("client_id and control_sequence must be provided together")
        return self


async def route_position(route, distance):
    """Interpolate off the event loop; the worker only reads the route."""
    return await asyncio.to_thread(_route_position, route, distance)


def _route_position(route, distance):
    points = route["coordinates"]
    lengths = []
    for a, b in zip(points, points[1:]):
        lat1, lat2 = math.radians(a[1]), math.radians(b[1])
        angle = (
            math.sin((lat2 - lat1) / 2) ** 2
            + math.cos(lat1) * math.cos(lat2) * math.sin(math.radians(b[0] - a[0]) / 2) ** 2
        )
        lengths.append(6371000 * 2 * math.asin(min(1, math.sqrt(angle))))
    remaining = max(0, min(1, distance / max(1, route["distance_m"]))) * sum(lengths)
    for a, b, length in zip(points, points[1:], lengths):
        if remaining <= length:
            ratio = remaining / max(1e-9, length)
            return {
                "lat": a[1] + (b[1] - a[1]) * ratio,
                "lng": a[0] + (b[0] - a[0]) * ratio,
                "label": "Vị trí mô phỏng hiện tại",
                "simulated": True,
            }
        remaining -= length
    return {"lat": points[-1][1], "lng": points[-1][0], "label": "Vị trí mô phỏng hiện tại", "simulated": True}


async def current_origin(nav):
    """Use the actual last position rather than the route's departure point."""
    if nav.route and nav.status in {"running", "charging", "paused", "completed", "ended"}:
        distance = nav.route["distance_m"] if nav.status == "completed" else nav.progress_m
        return await route_position(nav.route, distance)
    return nav.origin


def has_active_journey(nav):
    return nav.status in {"running", "charging"} or nav.status == "paused" and nav.resume_status is not None


class NavigationWorkflow:
    def __init__(self, services, ledger=None, resolver=None):
        self.services, self.ledger, self.resolver = services, ledger, resolver
        self.energy = services.energy_planner or EnergyPlanner()
        self.discovery = services.charging_discovery
        from src.vivi.navigation.calculation import RouteCalculator

        self.calculator = RouteCalculator(services, self.energy, self.discovery)
        self.places = PlaceResolver(services.navigation, getattr(services, "navigation_categories", None))

    async def plan_energy(self, nav, soc, provider=None, *, reuse_stations=True):
        self.calculator.energy = self.energy
        return await self.calculator.plan_energy(nav, soc, provider, reuse_stations=reuse_stations)

    @staticmethod
    def compose_route(plan):
        from src.vivi.navigation.calculation import RouteCalculator

        return RouteCalculator.compose_route(plan)

    def calculation_basis(self):

        assumptions = getattr(self.discovery, "assumptions", None)
        return fingerprint(
            {
                "profile": self.energy.profile.model_dump(mode="json") if self.energy.profile else None,
                "assumptions": assumptions.model_dump(mode="json") if assumptions else None,
                "charging_algorithm": "corridor-coverage-3",
            }
        )

    async def execute_plan(self, event, domain, step, arguments, command_id, provider=None):
        """Plan against a draft only; the service commits draft/journey separately."""
        from types import SimpleNamespace

        from src.vivi.navigation.models import ActiveJourney, NavigationState, PlanDraft

        name = step.draft.capability
        workspace = domain.navigation_workspace
        plan = workspace.draft.model_copy(deep=True) if workspace.draft else None
        fresh = name in {"navigation.plan", "navigation.search"} and not (
            step.result and step.result.get("status") in {"waiting_slot", "waiting_confirmation"}
        )
        if fresh:
            purpose = arguments.get("purpose", "navigate")
            origin = event.position or await current_origin(domain.navigation) or workspace.origin
            plan = PlanDraft(purpose=purpose, origin=origin, revision=workspace.revision + 1)
        elif plan is None:
            return {
                "status": "blocked",
                "message": "Không có kế hoạch hiện tại. Bạn lập tuyến trước nhé.",
                "error_code": "plan_not_found",
            }
        if not fresh and (
            arguments.get("plan_id") not in {None, plan.id}
            or arguments.get("route_revision") not in {None, plan.revision}
        ):
            return {
                "status": "blocked",
                "message": "Kế hoạch đã thay đổi. Bạn chọn lại nhé.",
                "error_code": "stale_plan",
            }
        if name.endswith(".cancel"):
            return {"status": "completed", "message": "Đã hủy kế hoạch đang chờ.", "clear_draft": True}
        if name.endswith(".decline"):
            if plan.offer:
                plan.offer["status"] = "declined"
            return {
                "status": "completed",
                "message": "Mình giữ kế hoạch để bạn xem lại.",
                "draft_update": plan.model_dump(mode="json"),
            }
        if name.endswith(".start") and plan.started_revision == plan.revision:
            return {"status": "completed", "message": "Kế hoạch này đã được bắt đầu.", "verified": True}
        nav = NavigationState.model_validate(plan.model_dump(mode="json", include=set(NavigationState.model_fields)))
        mapped = step.model_copy(deep=True)
        args = {k: v for k, v in arguments.items() if k not in {"plan_id", "purpose"}}
        if name.endswith(".start"):
            if not plan.offer or plan.offer.get("status") != "pending" or plan.clarification or not plan.route:
                return {
                    "status": "blocked",
                    "message": "Bạn hoàn tất địa điểm và xác nhận tuyến hiện tại nhé.",
                    "error_code": "plan_not_ready",
                }
            if plan.calculation_basis != self.calculation_basis():
                nav.initial_soc = None
            args["route_revision"] = plan.revision
            args["plan_id"] = plan.id
            # Consent belongs to the draft offer, never to the planning step.
            mapped.confirmation = None
            result = await self.execute(
                event, SimpleNamespace(navigation=nav), mapped, args, f"{plan.id}:r{plan.revision}:start", provider
            )
        else:
            result = await self.execute(event, SimpleNamespace(navigation=nav), mapped, args, command_id, provider)
        update = result.pop("navigation_update", None)
        if update:
            updated = NavigationState.model_validate(update)
            if updated.status in {"running", "charging", "paused"} and name.endswith(".start"):
                plan.started_revision = plan.revision
                if plan.offer:
                    plan.offer["status"] = "accepted"
                result["journey_update"] = ActiveJourney.model_validate(update).model_dump(mode="json")
            else:
                plan = PlanDraft.model_validate({**plan.model_dump(mode="json"), **update})
                plan.phase = (
                    "needs_origin"
                    if not plan.origin
                    else "needs_destination"
                    if plan.clarification or not plan.route
                    else "ready"
                    if (plan.energy or {}).get("selected_plan")
                    else "incomplete"
                )
                plan.calculation_status = (
                    "ready" if plan.phase == "ready" else "incomplete" if plan.phase == "incomplete" else "draft"
                )
                plan.calculation_basis = self.calculation_basis()
                if plan.purpose == "itinerary" and plan.route and not plan.clarification:
                    result.pop("confirmation", None)
                    result.pop("action", None)
                    result.pop("execution", None)
                    result["message"] = route_summary(plan, ask_start=False)["speech"]
            result["draft_update"] = plan.model_dump(mode="json")
        return result

    def clarify_region(self, nav, region):
        nav.revision += 1
        label = region.get("representative_label", "điểm đại diện " + region["name"])
        question = f"Bạn muốn đến {label} hay địa điểm cụ thể nào ở {region['name']}?"
        nav.clarification = {
            "region": region["name"],
            "question": question,
            "phase": "scope",
            "revision": nav.revision,
            "choices": [
                {
                    "handle": "choice:" + str(uuid4()),
                    "label": label[0].upper() + label[1:],
                    "kind": "representative",
                    "query": region.get("representative_query", region["name"]),
                },
                {"handle": "choice:" + str(uuid4()), "label": "Địa điểm cụ thể khác", "kind": "specific"},
            ],
        }
        nav.candidates = []
        nav.status = "selecting"
        nav.selected, nav.route, nav.energy, nav.summary = None, None, None, None
        nav.alternatives = []
        return self.result(nav, question, "waiting_slot", "navigation.results")

    async def execute(self, event, session, step, arguments, command_id, effective_provider=None):
        nav = session.navigation.model_copy(deep=True)
        name = step.draft.capability
        if arguments.get("destination"):
            destination = DestinationSpec.from_arguments(arguments)
            arguments = {**arguments, "query": destination.query()}
            if destination.nearest:
                arguments["nearest"] = True
            if destination.selection == "region":
                arguments["destination_scope"] = "region"
            if destination.selection == "category" and destination.entity_type in CATEGORIES:
                arguments["category"] = destination.entity_type
        binding_arguments = dict(arguments)
        if name in {"navigation.plan", "navigation.search"} and not (
            step.result and step.result.get("status") == "waiting_slot"
        ):
            nav.request_id = "nav:" + str(uuid4())
        if self.services.navigation is None:
            return {
                "status": "blocked",
                "message": "Dịch vụ bản đồ chưa sẵn sàng.",
                "error_code": "navigation_unavailable",
            }
        if name == "navigation.start":
            if arguments.get("route_revision") != nav.revision or not nav.offer or nav.clarification:
                return {
                    "status": "blocked",
                    "message": "Bạn xác nhận đúng tuyến xem trước hiện tại nhé.",
                    "error_code": "stale_plan",
                }
            refreshed = await self.revalidate_preview(event, nav, nav, step, arguments, command_id)
            if refreshed is not None:
                return refreshed
            offer = nav.offer
            consumed = await asyncio.to_thread(
                self.services.action_gateway.confirmations.consume_generic,
                event.session_id,
                offer["grant"]["confirmation_id"],
                offer["binding"],
            )
            if not consumed:
                renewed = await self.preview(event, nav, step, arguments, command_id, brief=True)
                renewed["message"] = "Xác nhận trước không còn hiệu lực. " + renewed["message"]
                return renewed
            nav.offer = None
            return await self.start(event, nav, command_id, arguments)
        if event.position:
            nav.origin = Position.model_validate(event.position).model_dump(mode="json")
        elif nav.status in {"running", "charging", "paused", "completed"} and nav.route:
            nav.origin = await current_origin(nav)
        representative = False
        default_region = None
        nav.trip_kind = arguments.get("trip_kind") or nav.trip_kind or "one_way"
        if arguments.get("query"):
            nav.destination = DestinationSpec.from_arguments(arguments)
        if name == "navigation.refine":
            clarification = nav.clarification
            if not clarification and (
                nav.status == "selecting" or nav.pending and nav.pending.get("status") == "selecting"
            ):
                clarification = {"phase": "specific", "region": "", "revision": nav.revision, "choices": []}
                nav.clarification = clarification
            if not clarification or clarification.get("revision") != nav.revision:
                return {
                    "status": "blocked",
                    "message": "Lựa chọn đã thay đổi. Bạn nói lại điểm đến nhé.",
                    "error_code": "stale_choice",
                }
            if arguments.get("reject_candidates"):
                nav.revision += 1
                nav.candidates = []
                nav.clarification = {
                    "phase": "specific",
                    "region": clarification.get("region", ""),
                    "revision": nav.revision,
                    "choices": [],
                    "question": "Bạn muốn tìm địa điểm nào khác? Nói thêm tên hoặc khu vực nhé.",
                }
                return self.result(nav, nav.clarification["question"], "waiting_slot", "navigation.results")
            if arguments.get("choice_handle"):
                choice = next((c for c in clarification["choices"] if c["handle"] == arguments["choice_handle"]), None)
                if not choice:
                    return {
                        "status": "blocked",
                        "message": "Lựa chọn đã thay đổi. Bạn chọn lại nhé.",
                        "error_code": "stale_choice",
                    }
                if choice["kind"] == "specific":
                    nav.revision += 1
                    nav.clarification.update(
                        phase="specific",
                        choices=[],
                        revision=nav.revision,
                        question=f"Bạn muốn đến địa điểm nào ở {clarification['region']}?",
                    )
                    return self.result(nav, nav.clarification["question"], "waiting_slot", "navigation.results")
                if (
                    clarification.get("phase") in {"change_destination", "confirm_destination"}
                    and nav.route
                    and nav.selected
                ):
                    nav.clarification = None
                    return await self.preview(event, nav, step, binding_arguments, command_id, session.navigation)
                arguments = {"query": choice["query"], "destination_scope": "place"}
                representative = True
            else:
                query = arguments["query"]
                region = region_for(query)
                if region:
                    self.clarify_region(nav, region)
                    default_region = region
                    arguments = {"query": nav.clarification["choices"][0]["query"]}
                    representative = True
                # Search within the region only when the reply didn't name one.
                if (
                    not representative
                    and not arguments.get("destination")
                    and clarification.get("region")
                    and not administrative_layer(query)
                    and not any(region_for(part.strip()) for part in query.split(","))
                ):
                    query += ", " + clarification["region"]
                if not representative:
                    arguments = {**arguments, "query": query, "destination_scope": "place"}
        elif (
            name in {"navigation.plan", "navigation.search"}
            and not arguments.get("category")
            and not arguments.get("use_search_results")
            and not (arguments.get("destination") or {}).get("reference")
        ):
            region = region_for(arguments.get("query"))
            if region or arguments.get("destination_scope") == "region":
                default_region = region or {"name": arguments["query"]}
                self.clarify_region(nav, default_region)
                arguments = {"query": nav.clarification["choices"][0]["query"]}
                representative = True
        if binding_arguments.get("origin_query"):
            origins = await self.services.navigation.search(binding_arguments["origin_query"])
            resolved = []
            for place in origins[:3]:
                if place.lat is None or place.lng is None:
                    place = await self.services.navigation.place(place.ref_id)
                if place.lat is not None and place.lng is not None:
                    resolved.append(place.model_dump())
            if len(resolved) != 1:
                return self.result(
                    nav,
                    f"Mình chưa xác định được điểm xuất phát {binding_arguments['origin_query']}. "
                    "Bạn nói cụ thể hơn, hoặc nói đi từ vị trí xe nhé.",
                    "waiting_slot",
                    "navigation.results",
                )
            nav.origin = {**resolved[0], "label": resolved[0]["name"]}
        if not nav.origin:
            return {
                "status": "waiting_slot",
                "message": "Bạn chọn điểm xuất phát hoặc cho phép lấy vị trí nhé.",
                "error_code": "origin_required",
                "ui_events": [{"type": "navigation.locate"}],
            }
        reference = (arguments.get("destination") or {}).get("reference")
        if name == "navigation.select" or reference:
            station_choices = list((nav.energy or {}).get("station_candidates", []))
            if nav.pending:
                station_choices = (nav.pending.get("navigation", {}).get("energy") or {}).get("station_candidates", [])
            selected = next(
                (
                    p
                    for p in [
                        *nav.candidates,
                        *station_choices,
                        *([nav.selected] if reference and nav.selected else []),
                    ]
                    if p["handle"] == (reference or arguments["place_handle"])
                ),
                None,
            )
            if not selected:
                return {
                    "status": "blocked",
                    "message": "Danh sách địa điểm đã thay đổi. Bạn tìm lại nhé.",
                    "error_code": "stale_place",
                }
            if reference and not satisfies_area(selected, DestinationSpec.from_arguments(arguments).area):
                return {
                    "status": "blocked",
                    "message": "Địa điểm đã chọn không thuộc khu vực bạn yêu cầu. Bạn tìm lại nhé.",
                    "error_code": "destination_area_mismatch",
                }
            if reference:
                nav.destination = DestinationSpec.from_arguments(arguments)
        else:
            if not arguments.get("use_search_results"):
                try:
                    destination = DestinationSpec.from_arguments(arguments)
                    # Legacy refinement appends a region to query. Merge it
                    # into the typed slot instead of losing an existing name.
                    if destination.query() != arguments.get("query") and not destination.area:
                        scoped = DestinationSpec.from_arguments({"query": arguments["query"]})
                        destination = destination.model_copy(update={"area": scoped.area})
                    nav.destination = destination
                    if destination.selection == "category" and destination.entity_type in CATEGORIES:
                        # The host-inferred category replaces a model's guess downstream too.
                        arguments = {**arguments, "category": destination.entity_type}
                    if destination.nearest:
                        arguments = {**arguments, "nearest": True}
                    resolution = await self.places.resolve(
                        destination,
                        nav.origin,
                        representative=representative,
                        with_travel=name == "navigation.search" or destination.nearest,
                    )
                    nav.candidates = resolution.candidates
                    if resolution.status == "provider_unavailable":
                        raise VietMapError("Không kết nối được bản đồ")
                except VietMapError:
                    result = self.result(
                        nav,
                        "Chưa kết nối được bản đồ. Yêu cầu được giữ để bạn thử lại.",
                        "blocked",
                        "navigation.results",
                    )
                    return {**result, "error_code": "map_unavailable", "failure_kind": "dependency"}
            if (
                not representative
                and not arguments.get("category")
                and any(administrative_match(p, arguments.get("query", "")) for p in nav.candidates)
            ):
                nav.candidates = [p for p in nav.candidates if administrative_match(p, arguments["query"])]
                if not administrative_layer(arguments["query"]):
                    default_region = {"name": arguments["query"]}
                representative = True
            nav.revision += 1
            nav.clarification = None
            nav.status = "selecting"
            nav.selected = None
            nav.route, nav.energy, nav.alternatives, nav.summary = None, None, [], None
            if not nav.candidates:
                if representative:
                    nav.clarification = (
                        session.navigation.clarification
                        or {"region": (default_region or {}).get("name", arguments["query"])}
                    ).copy()
                    nav.clarification.update(
                        phase="specific",
                        choices=[],
                        revision=nav.revision,
                        question="Mình chưa xác minh được điểm đại diện. Bạn muốn đến địa điểm cụ thể nào?",
                    )
                    return self.result(nav, nav.clarification["question"], "waiting_slot", "navigation.results")
                query = arguments.get("query", "")
                required_slot = resolution.required_slot if not arguments.get("use_search_results") else "area"
                question = (
                    (
                        f"Mình chưa tìm được {query}. Địa điểm này ở tỉnh/thành hoặc địa chỉ nào?"
                        if required_slot == "area"
                        else f"Mình chưa xác minh được {query}. Bạn cho tên địa điểm cụ thể hoặc địa chỉ khác nhé."
                    )
                    if not arguments.get("category")
                    else f"Mình không tìm thấy {CATEGORIES[arguments['category']].label} nào trong bán kính "
                    f"{resolution.evidence[0]['radius_m'] // 1000} km quanh xe. Bạn muốn tìm ở khu vực nào?"
                    if resolution.reason == "no_nearby_candidates"
                    else "Chưa tìm được kết quả phù hợp. Bạn cho thêm địa chỉ hoặc khu vực nhé?"
                )
                nav.clarification = {
                    "phase": "place_area" if required_slot == "area" else "specific",
                    "destination": nav.destination.model_dump(mode="json") if nav.destination else None,
                    "query": query,
                    "region": "",
                    "question": question,
                    "revision": nav.revision,
                    "choices": [],
                }
                nav.selection_decision = (
                    {
                        "resolution": resolution.status,
                        "reason": resolution.reason,
                        "required_slot": resolution.required_slot,
                        "queries": resolution.queries,
                    }
                    if not arguments.get("use_search_results")
                    else None
                )
                return self.result(nav, question, "waiting_slot", "navigation.results")
            decision = None
            nav.selection_decision = None
            if not representative and name in {"navigation.plan", "navigation.refine"}:
                decision = await decide_place(
                    self.resolver,
                    effective_provider,
                    (
                        nav.destination.model_copy(update={"area": None}).query()
                        if nav.destination and nav.destination.area and nav.destination.area.constraint == "preferred"
                        else arguments.get("query", "")
                    ),
                    nav.candidates,
                    nearest=bool(arguments.get("nearest")),
                )
            nav.selection_decision = decision
            if decision and not arguments.get("use_search_results"):
                nav.selection_decision = {
                    **decision,
                    "resolution": resolution.status,
                    "evidence": resolution.evidence,
                    "queries": resolution.queries,
                }
            if decision and decision["mode"] == "propose":
                selected = next(p for p in nav.candidates if p["handle"] == decision["handle"])
                selected["selection_decision"] = decision
            elif representative and len(nav.candidates) == 1:
                selected = nav.candidates[0]
                selected["representative"] = True
                selected["spoken_label"] = "Điểm đại diện " + place_label(selected)
                if default_region and not default_region.get("representative_label"):
                    default_region = {
                        **default_region,
                        "representative_label": "điểm đại diện " + place_label(selected),
                    }
            else:
                message = (
                    "Chọn điểm đến cụ thể: " if arguments.get("destination_scope") == "region" else ""
                ) + choice_message(nav.candidates)
                return self.result(
                    nav,
                    message,
                    "waiting_slot" if name in {"navigation.plan", "navigation.refine"} else "completed",
                    "navigation.results",
                )
        nav.clarification = None
        try:
            route = await self.services.navigation.route(coordinates(nav.origin), coordinates(selected))
        except VietMapError:
            return self.result(
                nav,
                "Chưa tính được tuyến tới địa điểm này. Bạn chọn lại hoặc thử nơi khác nhé.",
                "waiting_slot",
                "navigation.results",
            )
        nav.selected, nav.route = selected, route.model_dump()
        nav.waypoint_routes = [nav.route]
        if nav.trip_kind == "round_trip":
            try:
                returning = await self.services.navigation.route(coordinates(selected), coordinates(nav.origin))
            except (VietMapError, TimeoutError):
                logger.warning(
                    "Return route unavailable session=%s turn=%s", event.session_id, event.turn_id, exc_info=True
                )
                nav.energy = {
                    "error_code": "return_route_unavailable",
                    "status": "incomplete",
                    "message": "Chưa tính được đường về; kế hoạch khứ hồi chưa hoàn chỉnh.",
                }
                nav.status = "preview"
                return self.result(nav, nav.energy["message"], "blocked", "navigation.preview")
            nav.waypoint_routes.append(returning.model_dump())
            nav.route = self.compose_route(
                {"legs": nav.waypoint_routes, "distance_m": sum(r["distance_m"] for r in nav.waypoint_routes)}
            )
        nav.revision += 1
        nav.route_id = "route:" + str(uuid4())
        nav.pending, nav.resume_status = None, None
        observed = await self.services.action_gateway.vehicle.get_state(event.session_id)
        nav.initial_soc, nav.progress_m, nav.stop_index = observed.battery_percent, 0, 0
        nav.simulated_soc, nav.charge_elapsed_s = None, 0
        nav.last_assessment_m, nav.last_assessment_soc, nav.alerts = 0, None, []
        if self.energy.profile and (
            self.energy.profile.vehicle_model != event.vehicle_model
            or self.energy.profile.model_year != event.model_year
        ):
            nav.energy, nav.alternatives = (
                {"status": "configuration_required", "message": "Hồ sơ pin không khớp phiên bản xe hiện tại."},
                [],
            )
        else:
            nav.energy, nav.alternatives = await self.plan_energy(nav, observed.battery_percent, effective_provider)
        selected_plan = (nav.energy or {}).get("selected_plan")
        if selected_plan:
            nav.route = self.compose_route(selected_plan)
        nav.status = "preview"
        if default_region and not center_requested(binding_arguments.get("query")):
            nav.summary = route_summary(nav, ask_start=False)
            label = default_region.get("representative_label", "trung tâm " + default_region["name"])
            question = f"Mình đề xuất {label}. Bạn xác nhận dùng điểm này nhé, hoặc nói địa điểm muốn đổi."
            nav.clarification = {
                "region": default_region["name"],
                "phase": "confirm_destination",
                "revision": nav.revision,
                "question": question,
                "choices": [
                    {
                        "handle": "choice:" + str(uuid4()),
                        "label": "Giữ điểm mặc định",
                        "kind": "representative",
                        "query": nav.selected["name"],
                    },
                    {"handle": "choice:" + str(uuid4()), "label": "Đổi địa điểm", "kind": "specific"},
                ],
            }
            return self.result(nav, question, "waiting_slot", "navigation.results")
        return await self.preview(event, nav, step, binding_arguments, command_id, session.navigation)

    async def search(self, arguments, origin, *, representative=False):
        """Compatibility list API over the shared PlaceResolver."""
        destination = DestinationSpec.from_arguments(arguments)
        result = await self.places.resolve(
            destination, origin, representative=representative, with_travel=arguments.get("_travel", True)
        )
        if result.status == "provider_unavailable":
            raise VietMapError("Không kết nối được bản đồ")
        return result.candidates

    async def revalidate_preview(self, event, nav, current, step, arguments, command_id):
        observed = await self.services.action_gateway.vehicle.get_state(event.session_id)
        origin = event.position or await current_origin(current) or nav.origin
        moved = (
            math.hypot((origin["lat"] - nav.origin["lat"]) * 111000, (origin["lng"] - nav.origin["lng"]) * 103000) > 100
        )
        if not moved and nav.initial_soc is not None and abs(observed.battery_percent - nav.initial_soc) < 1:
            return None
        candidate = nav.model_copy(deep=True)
        candidate.origin = origin
        try:
            await self.refresh_remaining_route(candidate, nav)
            candidate.energy, candidate.alternatives = await self.plan_energy(candidate, observed.battery_percent, None)
        except (VietMapError, TimeoutError):
            logger.warning(
                "Route confirmation revalidation unavailable session=%s turn=%s",
                event.session_id,
                event.turn_id,
                exc_info=True,
            )
            return {
                "status": "blocked",
                "message": "Vị trí hoặc pin đã thay đổi; chưa cập nhật được tuyến. Bạn tính lại nhé.",
                "error_code": "route_revalidation_unavailable",
            }
        candidate.revision = max(nav.revision, current.revision) + 1
        candidate.route_id = "route:" + str(uuid4())
        candidate.initial_soc, candidate.progress_m, candidate.stop_index = observed.battery_percent, 0, 0
        candidate.simulated_soc, candidate.charge_elapsed_s = None, 0
        candidate.last_assessment_m, candidate.last_assessment_soc, candidate.alerts = 0, None, []
        candidate.status = "preview"
        plan = (candidate.energy or {}).get("selected_plan")
        if plan:
            candidate.route = self.compose_route(plan)
        result = await self.preview(event, candidate, step, arguments, command_id, current)
        result["message"] = "Vị trí hoặc pin đã thay đổi; mình cập nhật tuyến. " + result["message"]
        return result

    async def refresh_remaining_route(self, candidate, previous):
        if previous.trip_kind == "round_trip":
            traveled, outbound_end = 0, None
            for leg in ((previous.energy or {}).get("selected_plan") or {}).get("legs", []):
                traveled += leg["distance_m"]
                if leg.get("to") == "destination":
                    outbound_end = traveled
                    break
            if outbound_end is None:
                if len(previous.waypoint_routes) != 2:
                    raise VietMapError("Missing round-trip waypoint evidence")
                outbound_end = previous.waypoint_routes[0]["distance_m"]
            if previous.progress_m >= outbound_end:
                # Once the outward destination is passed, route only to the
                # original endpoint, never back to that outward destination.
                lng, lat = previous.route["coordinates"][-1]
                candidate.trip_kind = "one_way"
                candidate.selected = {
                    "lat": lat,
                    "lng": lng,
                    "name": (previous.origin or {}).get("label", "Điểm xuất phát ban đầu"),
                    "address": "",
                    "handle": "place:" + str(uuid4()),
                }
            else:
                outgoing = await self.services.navigation.route(
                    coordinates(candidate.origin), coordinates(candidate.selected)
                )
                lng, lat = previous.route["coordinates"][-1]
                returning = await self.services.navigation.route(coordinates(candidate.selected), (lat, lng))
                candidate.waypoint_routes = [outgoing.model_dump(), returning.model_dump()]
                candidate.route = self.compose_route(
                    {"legs": candidate.waypoint_routes, "distance_m": outgoing.distance_m + returning.distance_m}
                )
                return
        route = await self.services.navigation.route(coordinates(candidate.origin), coordinates(candidate.selected))
        candidate.waypoint_routes = [route.model_dump()]
        candidate.route = route.model_dump()

    async def preview(self, event, nav, step, arguments, command_id, previous=None, *, brief=False):
        if not nav.route or not nav.selected:
            return {"status": "blocked", "message": "Bạn chọn tuyến trước nhé."}
        name = (
            ("Điểm đại diện " + nav.selected["name"])
            if nav.selected.get("representative")
            else place_label(nav.selected)
        )
        nav.selected["spoken_label"] = name
        energy = nav.energy or {}
        suffix = energy.get("message", "")
        if energy.get("status") in {
            "no_feasible_plan",
            "charging_required",
            "station_data_required",
            "incomplete",
            "configuration_required",
        }:
            message = f"Tuyến đến {name}, khoảng {nav.route['distance_m'] / 1000:.1f} km. {suffix} Pin dự kiến chưa đủ mức dự phòng; mình chưa thể bắt đầu tuyến này."
            candidates = energy.get("station_candidates", [])
            if candidates:
                first = candidates[0]
                location = f", gần {first['road_context']}" if first.get("road_context") else ""
                message = (
                    f"Pin chưa đủ dự phòng tới {name}. Có {station_label(first)}{location}, "
                    f"cách khoảng {round(first['distance_m'] / 1000)} km. {suffix} "
                    "Bạn có thể chọn đến trạm một."
                )
            return self.result(nav, message, "blocked", "navigation.preview")
        nav.summary = route_summary(nav)
        message = f"Bạn xác nhận dẫn đường đến {name} nhé." if brief else nav.summary["speech"]
        binding = {
            "command_id": command_id,
            "capability": step.draft.capability,
            "arguments": arguments,
            "route_revision": nav.revision,
        }
        grant = await asyncio.to_thread(
            self.services.action_gateway.confirmations.create_generic, event.session_id, binding, message
        )
        nav.offer = {"grant": grant, "binding": binding, "question": message, "status": "pending"}
        result = self.result(nav, message, "completed", "navigation.preview")
        result.update(
            confirmation=grant,
            action={"intent": step.draft.capability, "arguments": arguments},
            execution={"allowed": False, "executed": False, "verified": False, "risk_class": "R0", "message": message},
        )
        return result

    async def start(self, event, nav, command_id, arguments, capability="navigation.start", *, keep_parked=False):
        assumed_stops = [
            s
            for s in ((nav.energy or {}).get("selected_plan") or {}).get("stops", [])
            if s.get("equipment_basis") == "assumed"
        ]
        assumptions = getattr(self.discovery, "assumptions", None)
        if assumed_stops and (
            not assumptions
            or any(
                s.get("power_kw") != assumptions.power_kw
                or s.get("connector") != assumptions.connector
                or s.get("max_soc") != assumptions.max_soc
                for s in assumed_stops
            )
        ):
            return {
                "status": "blocked",
                "message": "Thông số ước tính đã thay đổi. Bạn tính lại tuyến để xác nhận nhé.",
                "error_code": "charging_assumptions_changed",
            }
        if self.discovery and any(
            s.get("simulated") or not s.get("map_ref_id")
            for s in ((nav.energy or {}).get("selected_plan") or {}).get("stops", [])
        ):
            return {
                "status": "blocked",
                "message": "Tuyến sạc cũ cần tính lại từ trạm trên bản đồ.",
                "error_code": "station_source_changed",
            }
        if (nav.energy or {}).get("status") in {
            "no_feasible_plan",
            "charging_required",
            "station_data_required",
            "incomplete",
            "configuration_required",
        }:
            return {
                "status": "blocked",
                "message": "Pin dự kiến không đủ mức dự phòng và chưa có kế hoạch sạc khả thi.",
                "error_code": "insufficient_energy",
            }
        if not hasattr(self.services.action_gateway.vehicle, "set_driving"):
            return {"status": "blocked", "message": "Chế độ chạy hiện tại chỉ hỗ trợ xe mô phỏng."}
        observed = await self.services.action_gateway.vehicle.get_state(event.session_id)
        if any(door.open for _, door in observed.door_states) or observed.hood_open or observed.trunk_open:
            return {
                "status": "blocked",
                "message": "Chưa thể bắt đầu khi cửa, capo hoặc cốp đang mở. Tuyến xem trước vẫn được giữ.",
                "error_code": "simulation_policy",
            }
        if self.ledger:
            cached = await asyncio.to_thread(self.ledger.command_result, event.session_id, command_id)
            if cached:
                return cached["result"]
            await asyncio.to_thread(
                self.ledger.reserve_command,
                event.session_id,
                command_id,
                {"intent": capability, "arguments": arguments},
            )
        vehicle = await vehicle_effect(
            self.services.action_gateway.vehicle.set_driving, event.session_id, not keep_parked
        )
        nav.status = "paused" if keep_parked else "running"
        nav.resume_status = "running" if keep_parked else None
        nav.offer = None
        nav.candidates, nav.clarification, nav.pending = [], None, None
        result = self.result(
            nav,
            "Đã thay tuyến. Chuyển D để tiếp tục."
            if keep_parked
            else "Đã bắt đầu dẫn đường."
            + (" " + next_leg_message(nav) if ((nav.energy or {}).get("selected_plan") or {}).get("stops") else ""),
            "completed",
            "navigation.start",
        )
        result.update(
            verified=True,
            vehicle_state=vehicle.model_dump(mode="json"),
            action={"intent": capability, "arguments": arguments},
            execution={
                "allowed": True,
                "executed": True,
                "verified": True,
                "risk_class": "R0",
                "message": result["message"],
            },
        )
        if self.ledger:
            await asyncio.to_thread(self.ledger.finish_command, event.session_id, command_id, result)
        return result

    async def event(self, event, session):
        value = NavigationEvent.model_validate(event.navigation_event)
        nav = session.navigation.model_copy(deep=True)
        if value.id in nav.applied_events:
            return self.result(nav, "", "completed", "navigation.progress")
        if value.kind != "gear" and value.revision != nav.revision:
            return {"status": "blocked", "message": "Sự kiện thuộc tuyến cũ.", "error_code": "stale_route"}
        if not hasattr(self.services.action_gateway.vehicle, "set_energy") or nav.status not in {
            "running",
            "charging",
        }:
            return {"status": "blocked", "message": "Tiến trình chỉ áp dụng cho hành trình mô phỏng đang chạy."}
        plan = (nav.energy or {}).get("selected_plan")
        stop_distances = []
        if plan:
            traveled = 0
            cumulative = []
            for leg in plan["legs"]:
                traveled += leg["distance_m"]
                cumulative.append(traveled)
            search_from = 0
            for stop in plan["stops"]:
                index = stop.get("leg_index")
                if index is None:
                    index = next(
                        (
                            i
                            for i in range(search_from, len(plan["legs"]))
                            if plan["legs"][i]["to"] == stop["station_id"]
                        ),
                        None,
                    )
                if index is not None:
                    stop_distances.append((cumulative[index], stop))
                    search_from = index + 1
        if value.kind == "charge":
            if nav.status != "charging" or value.elapsed_s is None or nav.stop_index >= len(stop_distances):
                return {"status": "blocked", "message": "Xe chưa ở điểm dừng sạc."}
            nav.charge_elapsed_s += value.elapsed_s
            stop = stop_distances[nav.stop_index][1]
            if nav.charge_elapsed_s >= stop["charge_minutes"] * 60:
                nav.stop_index += 1
                nav.charge_elapsed_s, nav.status = 0, "running"
        else:
            if value.distance_m is None or value.distance_m < nav.progress_m:
                return {"status": "blocked", "message": "Tiến trình không hợp lệ."}
            limit = (
                stop_distances[nav.stop_index][0] if nav.stop_index < len(stop_distances) else nav.route["distance_m"]
            )
            nav.progress_m = min(value.distance_m, limit)
            if nav.stop_index < len(stop_distances) and nav.progress_m >= limit:
                nav.status = "charging"
        if nav.status != session.navigation.status and nav.status in {"running", "charging"}:
            await vehicle_effect(
                self.services.action_gateway.vehicle.set_driving, event.session_id, nav.status == "running"
            )
        if self.energy.profile:
            observed = await self.services.action_gateway.vehicle.get_state(event.session_id)
            expected_observed = nav.simulated_soc if nav.simulated_soc is not None else nav.initial_soc
            if expected_observed is not None and abs(observed.battery_percent - math.floor(expected_observed)) >= 1:
                nav.initial_soc += observed.battery_percent - expected_observed
            charged = sum(s["departure_soc"] - s["arrival_soc"] for _, s in stop_distances[: nav.stop_index])
            if nav.status == "charging":
                stop = stop_distances[nav.stop_index][1]
                station = (
                    ChargingStation.model_validate(stop)
                    if "connector" in stop
                    else next((s for s in self.energy.stations if s.id == stop["station_id"]), None)
                )
                if station:
                    charged += (
                        self.energy.soc_after_charge(
                            station, stop["arrival_soc"], stop["departure_soc"], nav.charge_elapsed_s
                        )
                        - stop["arrival_soc"]
                    )
            soc = max(0, (nav.initial_soc or 0) + charged - self.energy.soc_cost(nav.progress_m))
            nav.simulated_soc = soc
            await asyncio.to_thread(
                self.services.action_gateway.vehicle.set_energy,
                event.session_id,
                soc,
                soc / self.energy.soc_cost(1000),
            )
            if (
                nav.last_assessment_soc is None
                or abs(soc - nav.last_assessment_soc) >= 1
                or nav.progress_m - nav.last_assessment_m >= 1000
            ):
                next_m = (
                    stop_distances[nav.stop_index][0]
                    if nav.stop_index < len(stop_distances)
                    else nav.route["distance_m"]
                )
                remaining_soc = soc - self.energy.soc_cost(max(0, next_m - nav.progress_m))
                active = bool(nav.alerts)
                if remaining_soc < (17 if active else 15) and nav.status != "charging":
                    severity = "critical" if soc < 10 else "warning"
                    if not active or nav.alerts[0]["severity"] != severity:
                        now = datetime.now(UTC)
                        announce = (
                            nav.last_alert_at is None
                            or (now - nav.last_alert_at).total_seconds() >= 300
                            or (severity == "critical" and nav.last_alert_severity != "critical")
                        )
                        nav.alerts = [
                            {
                                "alert_id": str(uuid4()),
                                "code": "ROUTE_ENERGY_LOW",
                                "severity": severity,
                                "source": "route",
                                "announce": announce,
                                "message": "Pin dự kiến không đủ dự phòng tới điểm dừng kế tiếp. Bạn yêu cầu lập lại tuyến tới trạm sạc nhé.",
                            }
                        ]
                        if announce:
                            nav.last_alert_at, nav.last_alert_severity = now, severity
                else:
                    nav.alerts = []
                nav.last_assessment_m, nav.last_assessment_soc = nav.progress_m, soc
        if nav.progress_m >= nav.route["distance_m"] and nav.status != "charging":
            nav.status = "completed"
            await vehicle_effect(self.services.action_gateway.vehicle.set_driving, event.session_id, False)
        nav.applied_events = [*nav.applied_events[-99:], value.id]
        message, ui_type = "", "navigation.progress"
        if value.kind == "progress" and nav.status == "charging" and session.navigation.status != "charging":
            message, ui_type = charging_message(nav), "navigation.charging"
        elif value.kind == "charge" and nav.stop_index > session.navigation.stop_index:
            message, ui_type = "Đã sạc đủ pin. " + next_leg_message(nav), "navigation.next_leg"
        result = self.result(nav, message, "completed", ui_type)
        if message:
            result["ui_events"][0].update(message=message, id=value.id, route_id=nav.route_id)
        if value.kind in {"progress", "charge"}:
            observed = await self.services.action_gateway.vehicle.get_state(event.session_id)
            result["vehicle_state"] = observed.model_dump(mode="json")
        return result

    @staticmethod
    def result(nav, message, status, ui_type):
        return {
            "status": status,
            "message": message,
            "navigation_update": nav.model_dump(mode="json"),
            "ui_events": [{"type": ui_type, "id": str(uuid4()), "revision": nav.revision}],
        }
