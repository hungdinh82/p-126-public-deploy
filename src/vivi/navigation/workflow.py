"""Navigation capabilities propose checkpoint updates through the reducer."""

from __future__ import annotations

import asyncio
import math
import time
from datetime import UTC, datetime
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from src.vivi.navigation.destinations import administrative_match, region_for
from src.vivi.navigation.energy import ChargingStation, EnergyPlanner
from src.vivi.navigation.presentation import choice_message, place_label, route_summary, short_address, station_label
from src.vivi.navigation.selection import decide_place, distinct_places
from src.vivi.navigation.vietmap import VietMapError


class Position(BaseModel):
    model_config = ConfigDict(extra="forbid")
    lat: float = Field(ge=-90, le=90)
    lng: float = Field(ge=-180, le=180)
    label: str = "Vị trí của bạn"
    simulated: bool = False
    observed_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class NavigationEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["position", "point", "progress", "charge", "select_plan"]
    id: str = Field(min_length=1, max_length=100)
    revision: int = Field(ge=0)
    position: Position | None = None
    distance_m: float | None = Field(default=None, ge=0)
    elapsed_s: float | None = Field(default=None, ge=0, le=30)
    plan_id: str | None = None


def coordinates(value):
    return value["lat"], value["lng"]


def route_position(route, distance):
    points = route["coordinates"]
    lengths = []
    for a, b in zip(points, points[1:]):
        lat1, lat2 = math.radians(a[1]), math.radians(b[1])
        angle = (
            math.sin((lat2 - lat1) / 2) ** 2
            + math.cos(lat1) * math.cos(lat2) * math.sin(math.radians(b[0] - a[0]) / 2) ** 2
        )
        lengths.append(6371000 * 2 * math.asin(min(1, math.sqrt(angle))))
    remaining = min(1, distance / max(1, route["distance_m"])) * sum(lengths)
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


class NavigationWorkflow:
    def __init__(self, services, ledger=None, resolver=None):
        self.services, self.ledger, self.resolver = services, ledger, resolver
        self.energy = services.energy_planner or EnergyPlanner()
        self.discovery = services.charging_discovery

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
        if nav.status in {"running", "charging"}:
            nav.pending = {"status": "selecting", "message": question}
        else:
            nav.status = "selecting"
            nav.selected, nav.route, nav.energy, nav.summary = None, None, None, None
            nav.alternatives = []
        return self.result(nav, question, "waiting_slot", "navigation.results")

    async def execute(self, event, session, step, arguments, command_id, effective_provider="rules"):
        nav = session.navigation.model_copy(deep=True)
        name = step.draft.capability
        binding_arguments = dict(arguments)
        if name in {"navigation.plan", "navigation.search"} and not (
            step.result and step.result.get("status") == "waiting_slot"
        ):
            nav.request_id = "nav:" + str(uuid4())
        if name == "navigation.cancel":
            if arguments["route_revision"] != nav.revision:
                return {"status": "blocked", "message": "Yêu cầu đã thay đổi.", "error_code": "stale_route"}
            nav.revision += 1
            nav.clarification, nav.pending, nav.candidates = None, None, []
            if nav.status not in {"running", "charging"}:
                nav.status, nav.selected, nav.route, nav.energy = "idle", None, None, None
                nav.summary, nav.alternatives = None, []
            return self.result(nav, "Đã huỷ tìm địa điểm.", "completed", "navigation.cancel")
        if name == "navigation.stop":
            nav.clarification = None
            nav.candidates = []
            nav.pending = None
            vehicle = None
            if hasattr(self.services.action_gateway.vehicle, "set_driving"):
                vehicle = await asyncio.to_thread(
                    self.services.action_gateway.vehicle.set_driving, event.session_id, False
                )
            nav.status = "paused" if nav.route else "idle"
            result = self.result(nav, "Mình đã dừng hành trình.", "completed", "navigation.stop")
            if vehicle:
                result["vehicle_state"] = vehicle.model_dump(mode="json")
            return result
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
        elif nav.status in {"running", "charging"} and nav.route:
            nav.origin = route_position(nav.route, nav.progress_m)
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
                if clarification.get("phase") == "change_destination" and nav.route and nav.selected:
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
                    and not any(region_for(part.strip()) for part in query.split(","))
                ):
                    query += ", " + clarification["region"]
                if not representative:
                    arguments = {"query": query, "destination_scope": "place"}
        elif (
            name in {"navigation.plan", "navigation.search"}
            and not arguments.get("category")
            and not arguments.get("use_search_results")
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
        if name == "navigation.select":
            station_choices = list((nav.energy or {}).get("station_candidates", []))
            if nav.pending:
                station_choices = (nav.pending.get("navigation", {}).get("energy") or {}).get("station_candidates", [])
            selected = next(
                (p for p in [*nav.candidates, *station_choices] if p["handle"] == arguments["place_handle"]), None
            )
            if not selected:
                return {
                    "status": "blocked",
                    "message": "Danh sách địa điểm đã thay đổi. Bạn tìm lại nhé.",
                    "error_code": "stale_place",
                }
        else:
            if not arguments.get("use_search_results"):
                try:
                    nav.candidates = await self.search(arguments, nav.origin, representative=representative)
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
                default_region = {"name": arguments["query"]}
                representative = True
            nav.revision += 1
            nav.clarification = None
            nav.status = "selecting"
            nav.selected = None
            if session.navigation.status not in {"running", "charging"}:
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
                    if session.navigation.status in {"running", "charging"}:
                        active = session.navigation.model_copy(deep=True)
                        active.revision, active.clarification, active.candidates = nav.revision, nav.clarification, []
                        active.pending = {"status": "selecting", "message": nav.clarification["question"]}
                        nav = active
                    return self.result(nav, nav.clarification["question"], "waiting_slot", "navigation.results")
                question = "Chưa tìm được kết quả phù hợp. Bạn cho thêm địa chỉ hoặc khu vực nhé?"
                nav.clarification = {
                    "phase": "specific",
                    "region": "",
                    "question": question,
                    "revision": nav.revision,
                    "choices": [],
                }
                return self.result(nav, question, "waiting_slot", "navigation.results")
            decision = None
            nav.selection_decision = None
            if not representative and name in {"navigation.plan", "navigation.refine"}:
                decision = await decide_place(
                    self.resolver,
                    effective_provider,
                    arguments.get("query", ""),
                    nav.candidates,
                    nearest=bool(arguments.get("nearest")),
                )
            nav.selection_decision = decision
            if decision and decision["mode"] == "propose":
                selected = next(p for p in nav.candidates if p["handle"] == decision["handle"])
                selected["selection_decision"] = decision
            elif representative and len(nav.candidates) == 1:
                selected = nav.candidates[0]
                selected["representative"] = True
                selected["spoken_label"] = "Điểm đại diện " + selected["name"]
            else:
                message = (
                    "Chọn điểm đến cụ thể: " if arguments.get("destination_scope") == "region" else ""
                ) + choice_message(nav.candidates)
                if session.navigation.status in {"running", "charging"}:
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
            except Exception:
                nav.energy = {
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
        observed = await self.services.action_gateway.vehicle.get_state(event.session_id)
        nav.initial_soc, nav.progress_m, nav.stop_index = observed.battery_percent, 0, 0
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
        if default_region:
            nav.summary = route_summary(nav, ask_start=False)
            label = default_region.get("representative_label", "trung tâm " + default_region["name"])
            question = f"Mình lấy {label}. Bạn muốn đổi sang địa điểm cụ thể khác không?"
            nav.clarification = {
                "region": default_region["name"],
                "phase": "change_destination",
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
            if session.navigation.status in {"running", "charging"}:
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
        category = arguments.get("category")
        categories = getattr(self.services, "navigation_categories", {}) or {}
        # Without a verified category code use a bounded alias query plus a
        # local POI identity check, rather than fabricating a vendor code.
        kwargs = {"near": coordinates(origin)}
        if representative:
            kwargs["layers"] = "WARD" if region_for(arguments["query"].split(",")[0]) is None else "CITY"
        if category and categories.get(category):
            kwargs["category"] = categories[category]
        places = await self.services.navigation.search(arguments["query"], **kwargs)
        candidates = []
        for place in places[:10]:
            if representative and not administrative_match(place.model_dump(), arguments["query"].split(",")[0]):
                continue
            if representative and "," in arguments["query"]:
                from src.vivi.text import normalize_text

                region = region_for(arguments["query"].split(",")[-1].strip())
                if region and not any(
                    normalize_text(b.get("name", "")) == normalize_text(region["name"]) for b in place.boundaries
                ):
                    continue
            if category and not categories.get(category):
                from src.vivi.dialogue.parser import poi_matches

                if not poi_matches(place.name, category):
                    continue
            if place.lat is None or place.lng is None:
                try:
                    detail = await self.services.navigation.place(place.ref_id)
                    place = place.model_copy(
                        update={"lat": detail.lat, "lng": detail.lng, "address": place.address or detail.address}
                    )
                except Exception:
                    continue
                if category and not categories.get(category) and not poi_matches(place.name, category):
                    continue
            if place.lat is None or place.lng is None:
                continue
            value = place.model_dump()
            if not representative and not category:
                from src.vivi.dialogue.parser import place_query_matches

                if not place_query_matches(arguments["query"], value):
                    continue
            if all(p["ref_id"] != value["ref_id"] for p in candidates):
                candidates.append(
                    {
                        **value,
                        "handle": "place:" + str(uuid4()),
                        "category": category,
                        "spoken_label": place_label(value),
                        "short_address": short_address(value.get("address", "")),
                    }
                )
            if len(candidates) == 5:
                break
        candidates = distinct_places(candidates)
        distances = None
        if candidates and hasattr(self.services.navigation, "matrix"):
            try:
                distances = await self.services.navigation.matrix([coordinates(origin), *map(coordinates, candidates)])
            except Exception:
                pass
        reachable = []
        for index, place in enumerate(candidates):
            if distances and index < len(distances) and (distances[index] is None or distances[index] < 0):
                continue
            try:
                route = await self.services.navigation.route(coordinates(origin), coordinates(place))
            except Exception:
                continue
            place["distance_m"], place["duration_s"] = route.distance_m, route.duration_s
            place["_rank_distance"] = (
                distances[index]
                if distances and index < len(distances) and distances[index] is not None
                else route.distance_m
            )
            reachable.append(place)
        if arguments.get("nearest"):
            reachable.sort(key=lambda p: p["_rank_distance"])
        for place in reachable:
            place.pop("_rank_distance")
        return reachable[:3]

    async def plan_energy(self, nav, soc, provider):
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
            discovered = await self.discovery.discover(nav.route)
            stations = discovered.stations
            assessment["station_search"] = {
                "source": "vietmap",
                "observed_at": discovered.observed_at,
                "incomplete": discovered.incomplete,
                "errors": discovered.errors,
            }
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
        semaphore = asyncio.Semaphore(4)

        async def edge(a, b):
            async with semaphore:
                try:
                    route = await self.services.navigation.route(points[a], points[b])
                    edges[a, b] = route.model_dump()
                except Exception:
                    assessment["routing_incomplete"] = True

        try:
            async with asyncio.timeout(max(0.01, 25 - (time.monotonic() - started))):
                # Both legs are mandatory. A rest area on the opposite
                # carriageway may require a long detour despite looking nearby.
                await asyncio.gather(
                    *(edge(a, b) for s in stations for a, b in (("origin", s.id), (s.id, "destination")))
                )
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
                await asyncio.gather(
                    *(
                        edge(a, b)
                        for a in ["origin", *(["destination"] if round_trip else []), *(s.id for s in powered)]
                        if a != "destination" or round_trip
                        for b in ["destination", *(["return"] if round_trip else []), *(s.id for s in powered)]
                        if b != "origin" and a != b and (a, b) not in edges
                    )
                )
        except TimeoutError:
            assessment["routing_incomplete"] = True
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
        plan = energy.get("selected_plan")
        if plan:
            suffix = f" Dự kiến còn khoảng {math.floor(plan['arrival_soc'])}% pin khi đến."
            if plan["stops"]:
                suffix += (
                    " Dừng sạc: "
                    + "; ".join(
                        f"{station_label(s)}"
                        + (f", gần {s['road_context']}" if s.get("road_context") else "")
                        + f" khoảng {math.ceil(s['charge_minutes'])} phút"
                        for s in plan["stops"]
                    )
                    + "."
                )
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
            if previous and previous.status in {"running", "charging"}:
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
        if previous and previous.status in {"running", "charging"}:
            active = previous.model_copy(deep=True)
            active.revision = nav.revision
            active.clarification, active.candidates = None, []
            nav.pending = None
            active.pending = {"status": "preview", "navigation": nav.model_dump(mode="json"), "message": message}
            nav = active
        result = self.result(nav, message, "waiting_confirmation", "navigation.preview")
        result.update(
            confirmation=grant,
            action={"intent": step.draft.capability, "arguments": arguments},
            execution={"allowed": False, "executed": False, "verified": False, "risk_class": "R0", "message": message},
        )
        return result

    async def start(self, event, nav, command_id, arguments, capability="navigation.start"):
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
        vehicle = await asyncio.to_thread(self.services.action_gateway.vehicle.set_driving, event.session_id, True)
        nav.status = "running"
        result = self.result(nav, "Đã bắt đầu dẫn đường.", "completed", "navigation.start")
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
        if value.revision != nav.revision:
            return {"status": "blocked", "message": "Sự kiện thuộc tuyến cũ.", "error_code": "stale_route"}
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
            if nav.pending and nav.pending.get("navigation") and nav.status in {"running", "charging"}:
                from src.vivi.dialogue.contracts import NavigationState

                target = NavigationState.model_validate(nav.pending["navigation"])
            plan = next((p for p in target.alternatives if p["id"] == value.plan_id), None)
            if not plan or target.status != "preview":
                return {"status": "blocked", "message": "Phương án không thuộc tuyến xem trước hiện tại."}
            target.energy["selected_plan"] = plan
            target.route = self.compose_route(plan)
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
            if self.energy.profile:
                observed = await self.services.action_gateway.vehicle.get_state(event.session_id)
                expected_observed = nav.simulated_soc if nav.simulated_soc is not None else nav.initial_soc
                # The memory simulator starts empty after process restart. Restore
                # its authorized journey energy from checkpoint, not from browser
                # telemetry. This branch cannot issue effects through MQTT.
                if observed.state_version == 0 and not observed.driving and expected_observed is not None:
                    await asyncio.to_thread(
                        self.services.action_gateway.vehicle.set_energy,
                        event.session_id,
                        expected_observed,
                        expected_observed / self.energy.soc_cost(1000),
                    )
                    await asyncio.to_thread(self.services.action_gateway.vehicle.set_driving, event.session_id, True)
                    observed = await self.services.action_gateway.vehicle.get_state(event.session_id)
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
                            candidate.origin = route_position(nav.route, nav.progress_m)
                            candidate.pending, candidate.alerts = None, []
                            try:
                                fresh = await self.services.navigation.route(
                                    coordinates(candidate.origin), coordinates(nav.selected)
                                )
                                candidate.route = fresh.model_dump()
                                candidate.energy, candidate.alternatives = await self.plan_energy(
                                    candidate, soc, "rules"
                                )
                                candidate.initial_soc, candidate.progress_m, candidate.stop_index = soc, 0, 0
                                candidate.status = "preview"
                                plan = candidate.energy.get("selected_plan")
                                if plan:
                                    candidate.route = self.compose_route(plan)
                                nav.pending = {
                                    "status": candidate.energy["status"],
                                    "navigation": candidate.model_dump(mode="json"),
                                    "message": "Bạn xem phương án sạc mới nhé."
                                    if plan
                                    else "Không có trạm sạc có thể tới trong dữ liệu hiện tại.",
                                }
                            except Exception:
                                nav.pending = {
                                    "status": "unavailable",
                                    "message": "Chưa tính lại được đường đến trạm sạc.",
                                }
                    else:
                        nav.alerts = []
                    nav.last_assessment_m, nav.last_assessment_soc = nav.progress_m, soc
            if nav.progress_m >= nav.route["distance_m"] and nav.status != "charging":
                nav.status = "completed"
                await asyncio.to_thread(self.services.action_gateway.vehicle.set_driving, event.session_id, False)
        nav.applied_events = [*nav.applied_events[-99:], value.id]
        result = self.result(nav, "", "completed", "navigation.progress")
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
