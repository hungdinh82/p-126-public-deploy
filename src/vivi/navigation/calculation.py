"""Deterministic road and charging calculations, shared by every plan purpose."""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import UTC, datetime
from uuid import uuid4

from src.vivi.navigation.energy import ChargingStation, EnergyPlanner
from src.vivi.navigation.presentation import (
    station_label,
)
from src.vivi.navigation.reads import coordinates, read_budget, read_many
from src.vivi.navigation.vietmap import VietMapError
from src.vivi.persistence.runtime_store import fingerprint

logger = logging.getLogger(__name__)


class RouteCalculator:
    def __init__(self, services, energy, discovery):
        self.services, self.energy, self.discovery = services, energy, discovery

    def station_basis(self, nav):
        assumptions = getattr(self.discovery, "assumptions", None)
        return fingerprint(
            {
                "destination": coordinates(nav.selected),
                "trip_kind": nav.trip_kind,
                "profile": self.energy.profile.model_dump(mode="json"),
                "assumptions": assumptions.model_dump(mode="json") if assumptions else None,
                "equipment": [s.model_dump(mode="json") for s in getattr(self.discovery, "equipment", [])],
            }
        )

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
            stations=stations,
            observed_at=search["observed_at"],
            incomplete=search.get("incomplete", False),
            errors=list(search.get("errors", [])),
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
                    async with asyncio.timeout(
                        None if isinstance(self.discovery, VietMapChargingDiscovery) else budget
                    ):
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
        from src.vivi.navigation.poi import charging_poi_info

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
        return {
            **assessment,
            "status": "planned",
            "selected_plan": selected,
            "explanation": explanation,
            "simulated": selected["simulated"],
        }, choices

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
