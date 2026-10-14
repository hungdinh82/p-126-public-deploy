"""Navigation capabilities propose checkpoint updates through the reducer."""

from __future__ import annotations

import asyncio
import logging
import math
import time
from datetime import UTC, datetime
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

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
    resume_message,
    route_summary,
    station_label,
)
from src.vivi.navigation.reads import read_many
from src.vivi.navigation.resolver import PlaceResolver, satisfies_area
from src.vivi.navigation.selection import decide_place
from src.vivi.navigation.vietmap import VietMapError
from src.vivi.persistence.runtime_store import fingerprint
from src.vivi.turn_control import current_control

logger = logging.getLogger(__name__)


class Position(BaseModel):
    model_config = ConfigDict(extra="forbid")
    lat: float = Field(ge=-90, le=90)
    lng: float = Field(ge=-180, le=180)
    label: str = "Vị trí của bạn"
    simulated: bool = False
    observed_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


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


def coordinates(value):
    return value["lat"], value["lng"]


def read_budget(maximum):
    control = current_control.get()
    # Reserve time for the domain commit, response, checkpoint and turn receipt.
    return max(0.01, min(maximum, control.deadline - time.monotonic() - 5)) if control else maximum


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
        self.places = PlaceResolver(services.navigation, getattr(services, "navigation_categories", None))

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
        if has_active_journey(nav):
            nav.pending = {"status": "selecting", "message": question}
        else:
            nav.status = "selecting"
            nav.selected, nav.route, nav.energy, nav.summary = None, None, None, None
            nav.alternatives = []
        return self.result(nav, question, "waiting_slot", "navigation.results")

    async def execute(self, event, session, step, arguments, command_id, effective_provider="rules"):
        nav = session.navigation.model_copy(deep=True)
        name = step.draft.capability
        if name == "navigation.set_gear":
            return await self.event(
                event.model_copy(
                    update={
                        "navigation_event": {
                            "kind": "gear",
                            "gear": arguments["gear"],
                            "id": command_id,
                            "revision": nav.revision,
                        }
                    }
                ),
                session,
            )
        if arguments.get("destination"):
            destination = DestinationSpec.from_arguments(arguments)
            arguments = {**arguments, "query": destination.query()}
            if destination.nearest:
                arguments["nearest"] = True
            if destination.selection == "region":
                arguments["destination_scope"] = "region"
            if destination.selection == "category" and destination.entity_type in {"pharmacy", "charging"}:
                arguments["category"] = destination.entity_type
        binding_arguments = dict(arguments)
        if name in {"navigation.plan", "navigation.search"} and not (
            step.result and step.result.get("status") == "waiting_slot"
        ):
            nav.request_id = "nav:" + str(uuid4())
        if name == "navigation.decline":
            if arguments.get("route_revision") != nav.revision:
                return {"status": "blocked", "message": "Tuyến đã thay đổi."}
            target = nav.pending.get("navigation") if nav.pending else None
            offer = target.get("offer") if target else nav.offer
            if offer:
                offer["status"] = "declined"
            return self.result(nav, "Mình giữ kế hoạch để bạn xem lại.", "completed", "navigation.decline")
        if name == "navigation.cancel":
            if arguments["route_revision"] != nav.revision:
                return {"status": "blocked", "message": "Yêu cầu đã thay đổi.", "error_code": "stale_route"}
            nav.revision += 1
            nav.clarification, nav.pending, nav.candidates = None, None, []
            if not has_active_journey(nav):
                nav.status, nav.selected, nav.route, nav.energy = "idle", None, None, None
                nav.summary, nav.alternatives = None, []
            return self.result(nav, "Đã huỷ tìm địa điểm.", "completed", "navigation.cancel")
        if name == "navigation.end":
            return await self.end(event, nav)
        if name == "navigation.stop":
            return await self.pause(event, nav)
        if self.services.navigation is None:
            return {
                "status": "blocked",
                "message": "Dịch vụ bản đồ chưa sẵn sàng.",
                "error_code": "navigation_unavailable",
            }
        if step.confirmation:
            if nav.pending and nav.pending.get("navigation"):
                from src.vivi.dialogue.contracts import NavigationState

                nav = NavigationState.model_validate(nav.pending["navigation"])
            refreshed = await self.revalidate_preview(event, nav, session.navigation, step, arguments, command_id)
            if refreshed is not None:
                return refreshed
            binding = {
                "command_id": command_id,
                "capability": name,
                "arguments": arguments,
                "route_revision": nav.revision,
            }
            if not await asyncio.to_thread(
                self.services.action_gateway.confirmations.consume_generic,
                event.session_id,
                step.confirmation["confirmation_id"],
                binding,
            ):
                return {
                    "status": "blocked",
                    "message": "Xác nhận tuyến đã hết hạn hoặc tuyến đã thay đổi.",
                    "error_code": "stale_route",
                }
            return await self.start(event, nav, command_id, arguments, name)
        if name == "navigation.start":
            from src.vivi.dialogue.contracts import NavigationState

            candidate = nav
            if nav.pending and nav.pending.get("navigation"):
                candidate = NavigationState.model_validate(nav.pending["navigation"])
            offer = candidate.offer
            if arguments.get("route_revision") == nav.revision and offer and not candidate.clarification:
                refreshed = await self.revalidate_preview(event, candidate, nav, step, arguments, command_id)
                if refreshed is not None:
                    return refreshed
                binding = offer["binding"]
                consumed = await asyncio.to_thread(
                    self.services.action_gateway.confirmations.consume_generic,
                    event.session_id,
                    offer["grant"]["confirmation_id"],
                    binding,
                )
                if not consumed:
                    renewed = await self.preview(event, candidate, step, arguments, command_id, nav, brief=True)
                    renewed["message"] = "Xác nhận trước không còn hiệu lực. " + renewed["message"]
                    return renewed
                keep_parked = nav.status == "paused" and nav.resume_status is not None
                candidate.offer = None
                return await self.start(event, candidate, command_id, arguments, keep_parked=keep_parked)
            if nav.trip_kind is None:
                return {
                    "status": "blocked",
                    "message": "Kế hoạch cũ chưa rõ một chiều hay khứ hồi. Bạn lập lại tuyến nhé.",
                    "error_code": "trip_kind_required",
                }
            if arguments.get("plan_id") == "pending" and nav.pending and nav.pending.get("navigation"):
                from src.vivi.dialogue.contracts import NavigationState

                candidate = NavigationState.model_validate(nav.pending["navigation"])
                if arguments["route_revision"] != nav.revision:
                    return {"status": "blocked", "message": "Đề xuất thuộc tuyến cũ."}
                candidate.revision = nav.revision + 1
                return await self.preview(event, candidate, step, arguments, command_id, session.navigation)
            if arguments["route_revision"] != nav.revision or nav.status not in {"preview", "paused"}:
                return {
                    "status": "blocked",
                    "message": "Tuyến xem trước đã thay đổi. Bạn chọn lại tuyến nhé.",
                    "error_code": "stale_route",
                }
            return await self.preview(event, nav, step, arguments, command_id, brief=nav.status == "preview")
        if event.position:
            nav.origin = Position.model_validate(event.position).model_dump(mode="json")
        elif nav.status in {"running", "charging", "paused", "completed"} and nav.route:
            nav.origin = await current_origin(nav)
        if (
            step.result
            and step.result.get("status") == "waiting_confirmation"
            and (nav.status == "preview" or nav.pending)
        ):
            if nav.pending and nav.pending.get("navigation"):
                from src.vivi.dialogue.contracts import NavigationState

                candidate = NavigationState.model_validate(nav.pending["navigation"])
                return await self.preview(event, candidate, step, arguments, command_id, nav)
            return await self.preview(event, nav, step, arguments, command_id)
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
                if clarification.get("phase") in {"change_destination", "confirm_destination"} and nav.route and nav.selected:
                    if nav.pending and nav.pending.get("navigation"):
                        from src.vivi.dialogue.contracts import NavigationState

                        nav = NavigationState.model_validate(nav.pending["navigation"])
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
                    nav, "Bạn nói địa điểm xuất phát cụ thể hơn nhé.", "waiting_slot", "navigation.results"
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
                (p for p in [*nav.candidates, *station_choices, *([nav.selected] if reference and nav.selected else [])]
                 if p["handle"] == (reference or arguments["place_handle"])), None
            )
            if not selected:
                return {
                    "status": "blocked",
                    "message": "Danh sách địa điểm đã thay đổi. Bạn tìm lại nhé.",
                    "error_code": "stale_place",
                }
            if reference and not satisfies_area(selected, DestinationSpec.from_arguments(arguments).area):
                return {"status": "blocked", "message": "Địa điểm đã chọn không thuộc khu vực bạn yêu cầu. Bạn tìm lại nhé.",
                        "error_code": "destination_area_mismatch"}
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
                    resolution = await self.places.resolve(destination, nav.origin, representative=representative,
                                                           with_travel=name == "navigation.search" or bool(arguments.get("nearest")))
                    nav.candidates = resolution.candidates
                    if resolution.status == "provider_unavailable":
                        raise VietMapError("Không kết nối được bản đồ")
                except VietMapError:
                    nav.revision += 1
                    nav.candidates = []
                    if nav.clarification:
                        nav.clarification.update(
                            phase="specific",
                            choices=[],
                            revision=nav.revision,
                            question="Chưa kết nối được bản đồ. Bạn thử lại hoặc nói địa điểm khác nhé.",
                        )
                        return self.result(nav, nav.clarification["question"], "waiting_slot", "navigation.results")
                    return self.result(
                        nav,
                        "Chưa tìm được địa điểm vì lỗi bản đồ. Bạn thử tìm lại nhé.",
                        "blocked",
                        "navigation.results",
                    )
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
            if not has_active_journey(session.navigation):
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
                    if has_active_journey(session.navigation):
                        active = session.navigation.model_copy(deep=True)
                        active.revision, active.clarification, active.candidates = nav.revision, nav.clarification, []
                        active.pending = {"status": "selecting", "message": nav.clarification["question"]}
                        nav = active
                    return self.result(nav, nav.clarification["question"], "waiting_slot", "navigation.results")
                query = arguments.get("query", "")
                required_slot = resolution.required_slot if not arguments.get("use_search_results") else "area"
                question = (
                    f"Mình chưa tìm được {query}. Địa điểm này ở tỉnh/thành hoặc địa chỉ nào?"
                    if required_slot == "area"
                    else f"Mình chưa xác minh được {query}. Bạn cho tên địa điểm cụ thể hoặc địa chỉ khác nhé."
                ) if not arguments.get("category") else (
                    "Chưa tìm được kết quả phù hợp. Bạn cho thêm địa chỉ hoặc khu vực nhé?"
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
                nav.selection_decision = {"resolution": resolution.status, "reason": resolution.reason,
                                          "required_slot": resolution.required_slot, "queries": resolution.queries} if not arguments.get("use_search_results") else None
                if has_active_journey(session.navigation):
                    active = session.navigation.model_copy(deep=True)
                    active.revision, active.request_id = nav.revision, nav.request_id
                    active.candidates, active.clarification = [], nav.clarification
                    active.pending = {"status": "selecting", "message": question}
                    nav = active
                return self.result(nav, question, "waiting_slot", "navigation.results")
            decision = None
            nav.selection_decision = None
            if not representative and name in {"navigation.plan", "navigation.refine"}:
                decision = await decide_place(
                    self.resolver,
                    effective_provider,
                    (nav.destination.model_copy(update={"area": None}).query() if nav.destination and nav.destination.area and nav.destination.area.constraint == "preferred"
                     else arguments.get("query", "")),
                    nav.candidates,
                    nearest=bool(arguments.get("nearest")),
                )
            nav.selection_decision = decision
            if decision and not arguments.get("use_search_results"):
                nav.selection_decision = {**decision, "resolution": resolution.status,
                                          "evidence": resolution.evidence, "queries": resolution.queries}
            if decision and decision["mode"] == "propose":
                selected = next(p for p in nav.candidates if p["handle"] == decision["handle"])
                selected["selection_decision"] = decision
            elif representative and len(nav.candidates) == 1:
                selected = nav.candidates[0]
                selected["representative"] = True
                selected["spoken_label"] = "Điểm đại diện " + place_label(selected)
                if default_region and not default_region.get("representative_label"):
                    default_region = {**default_region, "representative_label": "điểm đại diện " + place_label(selected)}
            else:
                message = (
                    "Chọn điểm đến cụ thể: " if arguments.get("destination_scope") == "region" else ""
                ) + choice_message(nav.candidates)
                if has_active_journey(session.navigation):
                    active = session.navigation.model_copy(deep=True)
                    active.candidates, active.revision = nav.candidates, nav.revision
                    active.clarification = None
                    active.pending = {"status": "selecting", "message": message}
                    nav = active
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
            if has_active_journey(session.navigation):
                active = session.navigation.model_copy(deep=True)
                active.revision, active.request_id, active.clarification, active.candidates = (
                    nav.revision,
                    nav.request_id,
                    nav.clarification,
                    nav.candidates,
                )
                active.pending = {"status": "selecting", "navigation": nav.model_dump(mode="json"), "message": question}
                nav = active
            return self.result(nav, question, "waiting_slot", "navigation.results")
        return await self.preview(event, nav, step, binding_arguments, command_id, session.navigation)

    async def search(self, arguments, origin, *, representative=False):
        """Compatibility list API over the shared PlaceResolver."""
        destination = DestinationSpec.from_arguments(arguments)
        result = await self.places.resolve(destination, origin, representative=representative,
                                           with_travel=arguments.get("_travel", True))
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
            candidate.energy, candidate.alternatives = await self.plan_energy(
                candidate, observed.battery_percent, "rules"
            )
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

    def station_basis(self, nav):
        assumptions = getattr(self.discovery, "assumptions", None)
        return fingerprint({
            "destination": coordinates(nav.selected),
            "trip_kind": nav.trip_kind,
            "profile": self.energy.profile.model_dump(mode="json"),
            "assumptions": assumptions.model_dump(mode="json") if assumptions else None,
            "equipment": [s.model_dump(mode="json") for s in getattr(self.discovery, "equipment", [])],
        })

    async def cached_discovery(self, nav, basis):
        from src.vivi.navigation.charging import StationDiscovery, reproject_stations

        previous = nav.energy or {}
        search = previous.get("station_search") or {}
        if previous.get("status") != "planned" or search.get("basis") != basis:
            return None
        try:
            observed = datetime.fromisoformat(search["observed_at"])
            age = (datetime.now(UTC) - observed).total_seconds()
            if not 0 <= age <= 600:
                return None
            stations = [ChargingStation.model_validate(s) for s in previous.get("corridor_stations", [])]
        except (KeyError, TypeError, ValueError):
            return None
        stations = await asyncio.to_thread(reproject_stations, stations, nav.route, self.discovery.radius_m)
        if not stations:
            return None
        return StationDiscovery(
            stations=stations, observed_at=search["observed_at"],
            incomplete=search.get("incomplete", False), errors=list(search.get("errors", [])),
        )

    async def plan_energy(self, nav, soc, provider, *, reuse_stations=True):
        assessment = self.energy.assess(nav.route, soc)
        if not self.energy.profile:
            return assessment, []
        origin, destination = coordinates(nav.origin), coordinates(nav.selected)
        round_trip = nav.trip_kind == "round_trip"
        waypoints = ["destination", "return"] if round_trip else ["destination"]
        direct = nav.waypoint_routes or [nav.route]
        edges = {("origin", "destination"): direct[0]}
        if round_trip:
            if len(direct) != 2:
                return {"status": "incomplete", "message": "Chưa có đường về để tính kế hoạch khứ hồi."}, []
            edges["destination", "return"] = direct[1]
        if assessment["status"] == "sufficient":
            choices = self.energy.solve(origin, destination, soc, edges, [], waypoints=waypoints)
            return {**assessment, "selected_plan": choices[0]}, choices
        started = time.monotonic()
        if self.discovery:
            from src.vivi.navigation.charging import StationDiscovery, VietMapChargingDiscovery

            basis = self.station_basis(nav)
            discovered = await self.cached_discovery(nav, basis) if reuse_stations else None
            cache_hit = discovered is not None
            if discovered is None:
                try:
                    budget = read_budget(getattr(self.discovery, "budget_seconds", 8))
                    # The built-in discovery owns its deadline and returns partial
                    # results. An identical outer deadline used to discard them.
                    work = (
                        self.discovery.discover(nav.route, budget_seconds=budget)
                        if isinstance(self.discovery, VietMapChargingDiscovery)
                        else self.discovery.discover(nav.route)
                    )
                    async with asyncio.timeout(None if isinstance(self.discovery, VietMapChargingDiscovery) else budget):
                        discovered = (
                            await self.services.navigation.run_read(work)
                            if hasattr(self.services.navigation, "run_read")
                            else await work
                        )
                except TimeoutError:
                    discovered = StationDiscovery(incomplete=True, errors=["station_search_budget"])
            stations = discovered.stations
            assessment["station_search"] = {
                "source": "vietmap",
                "observed_at": discovered.observed_at,
                "incomplete": discovered.incomplete,
                "errors": discovered.errors,
                "basis": basis,
                "cache_hit": cache_hit,
            }
            assessment["corridor_stations"] = [s.model_dump(mode="json") for s in stations]
        else:
            # Explicit offline/test adapters can still use their own catalogue.
            # Production wires the live provider, with no synthetic fallback.
            from src.vivi.navigation.charging import route_context

            stations = sorted(
                self.energy.compatible_stations(), key=lambda s: route_context((s.lat, s.lng), nav.route)[0]
            )[:8]
        stations = [
            s
            for s in stations
            if s.accessible
            and s.connector in {"unknown", self.energy.profile.connector}
            and s.network.lower() in {"vinfast", "v-green", "vgreen"}
        ]
        points = {"origin": origin, "destination": destination, **{s.id: (s.lat, s.lng) for s in stations}}
        if round_trip:
            points["return"] = origin

        async def collect_edges(pairs):
            pairs = list(pairs)
            results = await read_many(
                self.services.navigation, [("route", (points[a], points[b]), {}) for a, b in pairs]
            )
            for (a, b), result in zip(pairs, results):
                if isinstance(result, (VietMapError, TimeoutError)):
                    assessment["routing_incomplete"] = True
                    assessment.setdefault("routing_errors", []).append(
                        {"from": a, "to": b, "error_type": type(result).__name__}
                    )
                elif isinstance(result, Exception):
                    raise result
                else:
                    edges[a, b] = result.model_dump()

        try:
            async with asyncio.timeout(read_budget(max(0.01, 25 - (time.monotonic() - started)))):
                # Both legs are mandatory. A rest area on the opposite
                # carriageway may require a long detour despite looking nearby.
                await collect_edges((a, b) for s in stations for a, b in (("origin", s.id), (s.id, "destination")))
                if self.discovery and not round_trip:
                    stations = [
                        s.model_copy(
                            update={
                                "detour_m": max(
                                    0,
                                    edges["origin", s.id]["distance_m"]
                                    + edges[s.id, "destination"]["distance_m"]
                                    - nav.route["distance_m"],
                                )
                            }
                        )
                        for s in stations
                        if ("origin", s.id) in edges
                        and (s.id, "destination") in edges
                        and edges["origin", s.id]["distance_m"] + edges[s.id, "destination"]["distance_m"]
                        <= nav.route["distance_m"] + self.discovery.max_detour_m
                    ]
                powered = EnergyPlanner(self.energy.profile, stations).compatible_stations()
                progress = {s.id: s.route_progress_m for s in powered}
                capacities = {s.id: s.max_soc for s in powered}
                from src.vivi.navigation.charging import distance

                await collect_edges(
                    (a, b)
                    for a in ["origin", *(["destination"] if round_trip else []), *(s.id for s in powered)]
                    if a != "destination" or round_trip
                    for b in ["destination", *(["return"] if round_trip else []), *(s.id for s in powered)]
                    if b != "origin"
                    and a != b
                    and (a, b) not in edges
                    # One-way corridor plans visit later route segments. Avoid
                    # expanding permutations that drive back through earlier
                    # chargers and exhaust the bounded solver before arrival.
                    and not (
                        self.discovery
                        and not round_trip
                        and a in progress
                        and b in progress
                        and progress[a] is not None
                        and progress[b] is not None
                        and progress[b] <= progress[a]
                    )
                    # Air distance is a lower bound on every road route. Do
                    # not request roads that cannot fit even a full charge.
                    and self.energy.soc_cost(distance(points[a], points[b]))
                    <= capacities.get(a, soc if a == "origin" else 100) - self.energy.reserve
                )
        except TimeoutError:
            logger.warning("Charging route calculation budget exceeded", exc_info=True)
            assessment["routing_incomplete"] = True
            assessment.setdefault("routing_errors", []).append({"error_type": "TimeoutError", "stage": "route_budget"})
            if self.discovery and not round_trip:
                stations = [
                    s
                    for s in stations
                    if ("origin", s.id) in edges
                    and (s.id, "destination") in edges
                    and edges["origin", s.id]["distance_m"] + edges[s.id, "destination"]["distance_m"]
                    <= nav.route["distance_m"] + self.discovery.max_detour_m
                ]
        reachable = []
        for station in stations:
            incoming = edges.get(("origin", station.id))
            if not incoming:
                continue
            arrival = soc - self.energy.soc_cost(incoming["distance_m"])
            if arrival < self.energy.reserve:
                continue
            reachable.append(
                {
                    **station.model_dump(),
                    "ref_id": station.map_ref_id or station.id,
                    "handle": "place:" + str(uuid4()),
                    "category": "charging",
                    "spoken_label": station_label(station.model_dump()),
                    "distance_m": incoming["distance_m"],
                    "duration_s": incoming["duration_s"],
                    "arrival_soc": round(arrival, 2),
                    "equipment_status": station.equipment_basis
                    if station in EnergyPlanner(self.energy.profile, [station]).compatible_stations()
                    else "missing",
                    "queue_status": "unknown",
                }
            )
        from src.vivi.dialogue.parser import charging_poi_info

        reachable.sort(
            key=lambda s: (
                not bool(s.get("rest_area_name") or (charging_poi_info(s["name"]) or {}).get("rest_area")),
                -(s["route_progress_m"] or 0),
                s["detour_m"] or 0,
            )
        )
        assessment["station_candidates"] = reachable[:3]
        powered = EnergyPlanner(self.energy.profile, stations).compatible_stations()
        allowed = None
        if round_trip and self.discovery:
            allowed = {}
            for phase, (source, target, road) in enumerate(
                (("origin", "destination", direct[0]), ("destination", "return", direct[1]))
            ):
                allowed[phase] = [
                    s.id
                    for s in powered
                    if (source, s.id) in edges
                    and (s.id, target) in edges
                    and edges[source, s.id]["distance_m"] + edges[s.id, target]["distance_m"]
                    <= road["distance_m"] + self.discovery.max_detour_m
                ]
        choices = self.energy.solve(
            origin, destination, soc, edges, powered, waypoints=waypoints, allowed_stations_by_phase=allowed
        )
        if not choices and self.discovery and assessment["station_search"].get("cache_hit"):
            # A changed road corridor may require additional stations even when
            # old POIs remain nearby. Refresh once rather than declaring failure.
            return await self.plan_energy(nav, soc, provider, reuse_stations=False)
        if self.energy.last_search_incomplete:
            assessment["search_limit_reached"] = True
        high = self.energy.solve(
            origin, destination, soc, edges, powered, reserve=20, waypoints=waypoints, allowed_stations_by_phase=allowed
        )
        if high and len(choices) < 3:
            choices.append({**high[0], "strategy": "higher_reserve"})
        if not choices:
            return {
                **assessment,
                "status": "incomplete"
                if assessment.get("routing_incomplete")
                or assessment.get("search_limit_reached")
                or assessment.get("station_search", {}).get("incomplete")
                else "station_data_required"
                if any(s["equipment_status"] == "missing" for s in reachable)
                else "no_feasible_plan",
                "message": "Kế hoạch chưa hoàn chỉnh do thiếu dữ liệu tuyến/trạm hoặc vượt giới hạn tính toán."
                if assessment.get("routing_incomplete")
                or assessment.get("search_limit_reached")
                or assessment.get("station_search", {}).get("incomplete")
                else "Chưa có thông số trụ để tính thời gian sạc."
                if any(s["equipment_status"] == "missing" for s in reachable)
                else "Chưa có lộ trình sạc khả thi trong dữ liệu trạm hiện tại.",
            }, []
        selected, explanation = choices[0], "Ưu tiên ít điểm dừng, giữ dự phòng ít nhất 15%."
        if self.resolver and provider in self.resolver.providers and len(choices) > 1:
            client = self.resolver.providers[provider].client
            schema = {
                "type": "object",
                "properties": {
                    "plan_id": {"type": "string", "enum": [p["id"] for p in choices]},
                    "reason": {"type": "string", "enum": ["fewest_stops", "faster", "higher_reserve"]},
                },
                "required": ["plan_id", "reason"],
                "additionalProperties": False,
            }
            try:
                import json

                async with asyncio.timeout(3):
                    payload = await client.agenerate_json(
                        system="Chọn ID và reason từ các phương án đã tính. Mặc định ít dừng; cân nhắc phương án tiết kiệm ít nhất 20 phút. Không tạo trạm hoặc số liệu.",
                        user=json.dumps(
                            [
                                {k: p[k] for k in ("id", "strategy", "total_seconds", "arrival_soc", "stops")}
                                for p in choices
                            ],
                            ensure_ascii=False,
                        ),
                        schema=schema,
                        schema_name="charging_plan_choice",
                    )
                    value = json.loads(payload)
                    selected = next(p for p in choices if p["id"] == value["plan_id"])
                    if selected["strategy"] == "higher_reserve":
                        explanation = f"Phương án giữ dự phòng ít nhất {selected['reserve_soc']:g}%."
                    elif selected["strategy"] == "fastest":
                        saved = max(0, round((choices[0]["total_seconds"] - selected["total_seconds"]) / 60))
                        explanation = f"Phương án nhanh nhất, tiết kiệm khoảng {saved} phút so với phương án ít dừng."
            except Exception:
                pass
        return {
            **assessment,
            "status": "planned",
            "selected_plan": selected,
            "explanation": explanation,
            "simulated": selected["simulated"],
        }, choices

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
            if previous and has_active_journey(previous):
                previous = previous.model_copy(deep=True)
                nav.pending = None
                previous.pending = {
                    "status": energy["status"],
                    "navigation": nav.model_dump(mode="json"),
                    "message": message,
                }
                previous.revision = nav.revision
                nav = previous
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
        if previous and has_active_journey(previous):
            active = previous.model_copy(deep=True)
            active.revision = nav.revision
            active.clarification, active.candidates = None, []
            nav.pending = None
            active.pending = {"status": "preview", "navigation": nav.model_dump(mode="json"), "message": message}
            nav = active
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

    async def end(self, event, nav):
        if not nav.route or nav.status not in {"running", "charging", "paused"}:
            return {"status": "blocked", "message": "Không có hành trình đang hoạt động."}
        result = await self.pause(event, nav)
        nav.status, nav.resume_status = "ended", None
        nav.offer = None
        ended = self.result(nav, "Đã kết thúc hành trình.", "completed", "navigation.end")
        ended["vehicle_state"] = result.get("vehicle_state")
        return ended

    async def pause(self, event, nav):
        if nav.status in {"running", "charging"}:
            nav.resume_status, nav.status = nav.status, "paused"
        vehicle = None
        if hasattr(self.services.action_gateway.vehicle, "set_driving"):
            vehicle = await vehicle_effect(self.services.action_gateway.vehicle.set_driving, event.session_id, False)
        message = "Đã về P, hành trình tạm dừng. Chuyển D để tiếp tục." if nav.status == "paused" else "Đã chuyển về P."
        result = self.result(nav, message, "completed", "navigation.pause")
        if vehicle:
            result["vehicle_state"] = vehicle.model_dump(mode="json")
        return result

    async def resume(self, event, nav):
        if nav.status != "paused" or not nav.route:
            return {"status": "blocked", "message": "Không có hành trình đang tạm dừng.", "error_code": "not_paused"}
        if nav.resume_status is None:
            # Legacy previews may have been marked paused without ever starting.
            result = self.result(
                nav, "Bạn xác nhận lại tuyến trước khi tiếp tục nhé.", "completed", "navigation.preview"
            )
            result["request_action"] = {"capability": "navigation.start", "arguments": {"route_revision": nav.revision}}
            return result
        if not hasattr(self.services.action_gateway.vehicle, "set_driving"):
            return {"status": "blocked", "message": "Xe hiện tại chưa hỗ trợ chuyển D qua mô phỏng."}
        observed = await self.services.action_gateway.vehicle.get_state(event.session_id)
        if any(door.open for _, door in observed.door_states) or observed.hood_open or observed.trunk_open:
            return {
                "status": "blocked",
                "message": "Chưa thể chuyển D khi cửa, capo hoặc cốp đang mở.",
                "error_code": "simulation_policy",
            }
        if self.energy.profile and nav.resume_status == "running":
            next_distance = nav.route["distance_m"]
            plan = (nav.energy or {}).get("selected_plan") or {}
            stops = plan.get("stops", [])
            if nav.stop_index < len(stops):
                stop = stops[nav.stop_index]
                end = stop.get("leg_index")
                if end is None:
                    end = next(
                        (i for i, leg in enumerate(plan.get("legs", [])) if leg.get("to") == stop["station_id"]), None
                    )
                if end is not None:
                    next_distance = sum(leg["distance_m"] for leg in plan["legs"][: end + 1])
            if observed.battery_percent - self.energy.soc_cost(max(0, next_distance - nav.progress_m)) < 15:
                return {
                    "status": "blocked",
                    "message": "Pin chưa đủ dự phòng tới điểm dừng tiếp theo. Bạn cập nhật kế hoạch sạc nhé.",
                    "error_code": "insufficient_energy",
                }
        try:
            vehicle = await vehicle_effect(
                self.services.action_gateway.vehicle.set_driving,
                event.session_id,
                nav.resume_status == "running",
                **(
                    {"expected_state_version": event.navigation_event["expected_vehicle_version"]}
                    if (event.navigation_event or {}).get("expected_vehicle_version") is not None
                    else {}
                ),
            )
        except ValueError as exc:
            return {"status": "blocked", "message": str(exc), "error_code": "simulation_policy"}
        nav.status, nav.resume_status = nav.resume_status, None
        result = self.result(nav, resume_message(nav), "completed", "navigation.resume")
        result["vehicle_state"] = vehicle.model_dump(mode="json")
        return result

    @staticmethod
    def compose_route(plan):
        coordinates_, steps = [], []
        for leg in plan["legs"]:
            offset = max(0, len(coordinates_) - 1)
            coordinates_.extend(leg["coordinates"][1:] if coordinates_ else leg["coordinates"])
            steps.extend({**step, "point_index": step["point_index"] + offset} for step in leg.get("steps", []))
        return {
            "distance_m": plan["distance_m"],
            "duration_s": sum(leg["duration_s"] for leg in plan["legs"]),
            "coordinates": coordinates_,
            "steps": steps,
            "bbox": None,
        }

    async def event(self, event, session):
        value = NavigationEvent.model_validate(event.navigation_event)
        nav = session.navigation.model_copy(deep=True)
        if value.id in nav.applied_events:
            return self.result(nav, "", "completed", "navigation.progress")
        if value.kind != "gear" and value.revision != nav.revision:
            return {"status": "blocked", "message": "Sự kiện thuộc tuyến cũ.", "error_code": "stale_route"}
        if value.kind == "history_clear":
            nav.applied_events = [*nav.applied_events[-99:], value.id]
            result = self.result(nav, "Đã xoá lịch sử hành trình trong phiên.", "completed", "navigation.history_clear")
            result["clear_navigation_history"] = True
            return result
        if value.kind == "gear":
            if value.gear not in {"P", "D"}:
                return {"status": "blocked", "message": "Thiếu trạng thái P hoặc D."}
            if value.gear == "P":
                if nav.status == "charging":
                    result = self.result(nav, "Xe đang ở P để sạc.", "completed", "navigation.gear")
                    result["vehicle_state"] = (
                        await self.services.action_gateway.vehicle.get_state(event.session_id)
                    ).model_dump(mode="json")
                else:
                    result = await self.pause(event, nav)
            elif nav.status == "paused":
                result = await self.resume(event, nav)
            elif nav.status == "charging":
                return {
                    "status": "blocked",
                    "message": "Xe đang sạc tới mức pin cần thiết để tiếp tục.",
                    "error_code": "charging",
                }
            elif hasattr(self.services.action_gateway.vehicle, "set_driving"):
                try:
                    vehicle = await vehicle_effect(
                        self.services.action_gateway.vehicle.set_driving,
                        event.session_id,
                        True,
                        **(
                            {"expected_state_version": value.expected_vehicle_version}
                            if value.expected_vehicle_version is not None
                            else {}
                        ),
                    )
                except ValueError as exc:
                    return {"status": "blocked", "message": str(exc), "error_code": "simulation_policy"}
                result = self.result(nav, "Đã chuyển D.", "completed", "navigation.gear")
                result["vehicle_state"] = vehicle.model_dump(mode="json")
            else:
                return {"status": "blocked", "message": "Xe chưa hỗ trợ đổi chế độ lái qua mô phỏng."}
            if result.get("navigation_update"):
                result["navigation_update"]["applied_events"] = [*nav.applied_events[-99:], value.id]
            return result
        if value.kind in {"pause", "resume", "clear", "end"}:
            if value.kind == "pause":
                result = await self.pause(event, nav)
            elif value.kind == "resume":
                result = await self.resume(event, nav)
            elif value.kind == "end":
                result = await self.end(event, nav)
            else:
                from src.vivi.dialogue.contracts import NavigationState

                vehicle = await self.services.action_gateway.vehicle.get_state(event.session_id)
                if nav.status in {"running", "charging"} or vehicle.driving:
                    return {
                        "status": "blocked",
                        "message": "Chuyển P trước khi xoá hành trình.",
                        "error_code": "journey_running",
                    }
                origin = await current_origin(nav)
                nav = NavigationState(origin=origin, revision=nav.revision + 1)
                result = self.result(
                    nav, "Đã xoá tuyến và kế hoạch đã lưu trong phiên.", "completed", "navigation.clear"
                )
                result["clear_journey"] = True
            if result.get("navigation_update"):
                result["navigation_update"]["applied_events"] = [*nav.applied_events[-99:], value.id]
            return result
        if value.kind == "point":
            nav.request_id = "nav:" + str(uuid4())
            if not value.position:
                raise ValueError("position is required")
            place = {
                **value.position.model_dump(mode="json"),
                "name": value.position.label,
                "address": "Điểm được chọn trên bản đồ",
                "handle": "place:" + str(uuid4()),
            }
            nav.candidates = [place]
            nav.clarification, nav.pending = None, None
            nav.applied_events = [*nav.applied_events[-99:], value.id]
            result = self.result(nav, "", "completed", "navigation.results")
            result["request_action"] = {
                "capability": "navigation.select",
                "arguments": {"place_handle": place["handle"]},
            }
            return result
        if value.kind == "position":
            if not value.position:
                raise ValueError("position is required")
            if nav.status in {"running", "charging"}:
                return {"status": "blocked", "message": "Dừng hành trình trước khi đổi điểm xuất phát."}
            nav.origin = value.position.model_dump(mode="json")
        elif value.kind == "select_plan":
            target = nav
            if nav.pending and nav.pending.get("navigation") and has_active_journey(nav):
                from src.vivi.dialogue.contracts import NavigationState

                target = NavigationState.model_validate(nav.pending["navigation"])
            plan = next((p for p in target.alternatives if p["id"] == value.plan_id), None)
            if not plan or target.status != "preview":
                return {"status": "blocked", "message": "Phương án không thuộc tuyến xem trước hiện tại."}
            target.offer = None
            target.energy["selected_plan"] = plan
            target.route = self.compose_route(plan)
            target.route_id = "route:" + str(uuid4())
            nav.revision += 1
            target.revision = nav.revision
            if target is not nav:
                nav.pending["navigation"] = target.model_dump(mode="json")
            # Invalidate the grant bound to the old revision; a new start request
            # will create a fresh preview and grant, never accept the old grant.
            nav.applied_events = [*nav.applied_events[-99:], value.id]
            result = self.result(
                nav, "Đã đổi phương án. Bạn xác nhận lại tuyến nhé.", "completed", "navigation.preview"
            )
            result["request_action"] = {
                "capability": "navigation.start",
                "arguments": {"route_revision": nav.revision, **({"plan_id": "pending"} if target is not nav else {})},
            }
            return result
        else:
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
                    stop_distances[nav.stop_index][0]
                    if nav.stop_index < len(stop_distances)
                    else nav.route["distance_m"]
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
                                    "message": "Pin dự kiến không đủ dự phòng tới điểm dừng kế tiếp. Bạn xem đề xuất trạm sạc nhé.",
                                }
                            ]
                            if announce:
                                nav.last_alert_at, nav.last_alert_severity = now, severity
                            candidate = nav.model_copy(deep=True)
                            candidate.origin = await route_position(nav.route, nav.progress_m)
                            candidate.pending, candidate.alerts = None, []
                            try:
                                await self.refresh_remaining_route(candidate, nav)
                                candidate.energy, candidate.alternatives = await self.plan_energy(
                                    candidate, soc, "rules"
                                )
                                candidate.initial_soc, candidate.progress_m, candidate.stop_index = soc, 0, 0
                                candidate.route_id = "route:" + str(uuid4())
                                candidate.simulated_soc, candidate.charge_elapsed_s = None, 0
                                candidate.last_assessment_m, candidate.last_assessment_soc = 0, None
                                candidate.status = "preview"
                                plan = candidate.energy.get("selected_plan")
                                if plan:
                                    candidate.route = self.compose_route(plan)
                                candidate.summary = route_summary(candidate, ask_start=False)
                                nav.pending = {
                                    "status": candidate.energy["status"],
                                    "navigation": candidate.model_dump(mode="json"),
                                    "message": "Bạn xem phương án sạc mới nhé."
                                    if plan
                                    else "Không có trạm sạc có thể tới trong dữ liệu hiện tại.",
                                }
                            except (VietMapError, TimeoutError):
                                logger.warning(
                                    "Charging replan unavailable session=%s turn=%s",
                                    event.session_id,
                                    event.turn_id,
                                    exc_info=True,
                                )
                                nav.pending = {
                                    "status": "unavailable",
                                    "message": "Chưa tính lại được đường đến trạm sạc.",
                                }
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
