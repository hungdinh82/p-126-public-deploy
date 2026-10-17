"""Discover real charging POIs along a road route; equipment is separate data."""

from __future__ import annotations

import asyncio
import math
from dataclasses import dataclass, field
from datetime import UTC, datetime
from hashlib import sha256

from src.vivi.navigation.energy import ChargingStation
from src.vivi.navigation.poi import charging_poi_info, normalize_text, rest_area_poi

MAP_SOURCE = "https://maps.vietmap.vn/docs/map-api/place-v4/"


def distance(a, b):
    lat1, lat2 = math.radians(a[0]), math.radians(b[0])
    angle = (
        math.sin((lat2 - lat1) / 2) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin(math.radians(b[1] - a[1]) / 2) ** 2
    )
    return 6371000 * 2 * math.asin(min(1, math.sqrt(angle)))


def geometry(route):
    points = [(lat, lng) for lng, lat in route["coordinates"]]
    cumulative = [0.0]
    for a, b in zip(points, points[1:]):
        cumulative.append(cumulative[-1] + distance(a, b))
    return points, cumulative


def sample_points(route, maximum=64, *, prepared=None):
    points, cumulative = prepared if prepared is not None else geometry(route)
    count = min(maximum, max(2, math.ceil(route["distance_m"] / 35000) + 1))
    samples, segment = [], 0
    for index in range(count):
        target = cumulative[-1] * index / (count - 1)
        while segment < len(points) - 2 and cumulative[segment + 1] < target:
            segment += 1
        ratio = (target - cumulative[segment]) / max(1e-9, cumulative[segment + 1] - cumulative[segment])
        a, b = points[segment], points[segment + 1]
        samples.append((a[0] + (b[0] - a[0]) * ratio, a[1] + (b[1] - a[1]) * ratio))
    return samples


def route_context(point, route, *, prepared=None):
    """Project onto geometry for shortlisting, never for drive distance or SOC."""
    points, cumulative = prepared if prepared is not None else geometry(route)
    scale = math.cos(math.radians(point[0]))
    closest = (float("inf"), 0, 0.0)
    for index, (a, b) in enumerate(zip(points, points[1:])):
        ax, ay = (a[1] - point[1]) * scale, a[0] - point[0]
        bx, by = (b[1] - point[1]) * scale, b[0] - point[0]
        dx, dy = bx - ax, by - ay
        ratio = min(1, max(0, -(ax * dx + ay * dy) / max(1e-18, dx * dx + dy * dy)))
        # The projection already uses a local planar metric. Compare squared
        # offsets here and calculate the geodesic gap only for the best segment.
        gap = (ax + dx * ratio) ** 2 + (ay + dy * ratio) ** 2
        if gap < closest[0]:
            closest = (gap, index, ratio)
    gap, index, ratio = closest
    a, b = points[index], points[index + 1]
    gap = distance(point, (a[0] + (b[0] - a[0]) * ratio, a[1] + (b[1] - a[1]) * ratio))
    progress = (
        (cumulative[index] + ratio * (cumulative[index + 1] - cumulative[index]))
        / max(1, cumulative[-1])
        * route["distance_m"]
    )
    step = next((step for step in reversed(route.get("steps", [])) if step.get("point_index", 0) <= index), {})
    return gap, progress, step.get("street", "")


@dataclass
class StationDiscovery:
    stations: list[ChargingStation] = field(default_factory=list)
    incomplete: bool = False
    errors: list[str] = field(default_factory=list)
    observed_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())


def reproject_stations(stations, route, radius_m):
    """Reuse map identities, with fresh corridor progress for a changed origin."""
    prepared = geometry(route)
    kept = []
    for station in stations:
        if station.simulated or not station.map_ref_id:
            continue
        gap, progress, road = route_context((station.lat, station.lng), route, prepared=prepared)
        if gap <= radius_m:
            kept.append(station.model_copy(update={"route_progress_m": round(progress), "road_context": road}))
    return kept


class VietMapChargingDiscovery:
    def __init__(
        self, maps, equipment=(), *, category="", radius_m=20000, budget_seconds=8, max_detour_m=15000, assumptions=None
    ):
        self.maps, self.equipment, self.category = maps, list(equipment), category
        self.radius_m, self.budget_seconds, self.max_detour_m = radius_m, budget_seconds, max_detour_m
        self.assumptions = assumptions

    def equipment_for(self, place):
        matches = [
            station
            for station in self.equipment
            if not station.simulated
            and distance((station.lat, station.lng), (place.lat, place.lng)) <= 100
            and (
                station.map_ref_id == place.ref_id
                or (not station.map_ref_id and normalize_text(station.name) == normalize_text(place.name))
            )
        ]
        return matches[0] if len(matches) == 1 else None

    async def discover(self, route, *, budget_seconds=None):
        result, groups, rest_groups, rest_places = StationDiscovery(), {}, {}, []
        prepared = await asyncio.to_thread(geometry, route)
        samples = sample_points(route, prepared=prepared)
        budget = min(self.budget_seconds, budget_seconds) if budget_seconds is not None else self.budget_seconds
        if route["distance_m"] > (len(samples) - 1) * 35000:
            result.incomplete = True
            result.errors.append("route_sampling_limit")
        semaphore = asyncio.Semaphore(4)

        async def search(point, query):
            try:
                async with semaphore:
                    places = await self.maps.search(
                        query,
                        near=point,
                        category=(self.category or None) if query == "trạm sạc" else None,
                        radius_m=self.radius_m,
                        layers="POI",
                    )
                groups[point, query] = sorted(
                    [place for place in places if charging_poi_info(place.name)],
                    key=lambda place: not charging_poi_info(place.name)["rest_area"],
                )
                rest_groups[point, query] = [place for place in places if rest_area_poi(place.name)]
            except Exception:
                result.incomplete = True
                result.errors.append("station_search_failed")

        async def hydrate(place):
            try:
                if place.lat is None or place.lng is None:
                    async with semaphore:
                        detail = await self.maps.place(place.ref_id)
                    # Place v4 may omit the POI name; retain the vendor search
                    # name/address instead of turning the place into an address.
                    place = detail.model_copy(update={"name": place.name, "address": place.address or detail.address})
                info = charging_poi_info(place.name)
                if not info or place.lat is None or place.lng is None:
                    return
                gap, progress, road = await asyncio.to_thread(
                    route_context, (place.lat, place.lng), route, prepared=prepared
                )
                if gap > self.radius_m:
                    return
                equipment = self.equipment_for(place)
                if equipment and not equipment.accessible:
                    return
                assumed = (
                    self.assumptions
                    if not equipment and self.assumptions and info["network"] in self.assumptions.networks
                    else None
                )
                result.stations.append(
                    ChargingStation(
                        id="vietmap:" + sha256(place.ref_id.encode()).hexdigest()[:24],
                        name=place.name,
                        address=place.address,
                        lat=place.lat,
                        lng=place.lng,
                        map_ref_id=place.ref_id,
                        location_source=MAP_SOURCE,
                        network=equipment.network if equipment else info["network"],
                        connector=equipment.connector if equipment else assumed.connector if assumed else "unknown",
                        power_kw=equipment.power_kw if equipment else assumed.power_kw if assumed else None,
                        max_soc=equipment.max_soc if equipment else assumed.max_soc if assumed else 80,
                        source=equipment.source if equipment else assumed.source if assumed else MAP_SOURCE,
                        updated_at=equipment.updated_at
                        if equipment
                        else assumed.updated_at
                        if assumed
                        else result.observed_at,
                        equipment_basis="verified"
                        if equipment and equipment.power_kw
                        else "assumed"
                        if assumed
                        else "missing",
                        simulated=False,
                        road_context=road,
                        route_progress_m=round(progress),
                    )
                )
            except Exception:
                result.incomplete = True
                result.errors.append("station_detail_failed")

        async def hydrate_rest(place):
            try:
                if place.lat is None or place.lng is None:
                    async with semaphore:
                        detail = await self.maps.place(place.ref_id)
                    place = detail.model_copy(update={"name": place.name, "address": place.address or detail.address})
                if (
                    place.lat is not None
                    and place.lng is not None
                    and (await asyncio.to_thread(route_context, (place.lat, place.lng), route, prepared=prepared))[0]
                    <= self.radius_m
                ):
                    rest_places.append(place)
            except Exception:
                result.incomplete = True
                result.errors.append("rest_area_detail_failed")

        try:
            async with asyncio.timeout(budget):
                # A slow search must leave time to hydrate successful results.
                # Cancellation drains pending reads before using partial groups.
                try:
                    async with asyncio.timeout(budget * 0.6):
                        await asyncio.gather(
                            *(search(point, query) for point in samples for query in ("trạm sạc", "trạm dừng nghỉ"))
                        )
                except TimeoutError:
                    result.incomplete = True
                    result.errors.append("station_search_timeout")
                found = {}
                # Round robin across route samples: dense origin results must
                # not consume the whole detail budget before later segments.
                # Merge both query variants per sample before round robin.
                # Response completion order must not favor the origin or let
                # duplicate queries consume coverage of later route segments.
                sampled_groups = [
                    list(
                        {
                            p.ref_id: p
                            for query in ("trạm sạc", "trạm dừng nghỉ")
                            for p in groups.get((point, query), [])
                        }.values()
                    )
                    for point in samples
                ]
                for rank in range(10):
                    for group in sampled_groups:
                        if rank < len(group):
                            place = group[rank]
                            found.setdefault(place.ref_id, place)
                detail_limit = min(128, max(24, len(samples) * 2))
                if len(found) > detail_limit:
                    result.incomplete = True
                    result.errors.append("station_candidate_limit")
                rest = {}
                for rank in range(10):
                    for group in rest_groups.values():
                        if rank < len(group):
                            rest.setdefault(group[rank].ref_id, group[rank])
                await asyncio.gather(
                    *(hydrate(place) for place in list(found.values())[:detail_limit]),
                    *(hydrate_rest(place) for place in list(rest.values())[:12] if place.ref_id not in found),
                )
        except TimeoutError:
            result.incomplete = True
            result.errors.append("station_search_timeout")
        for station in result.stations:
            nearby = sorted(rest_places, key=lambda place: distance((station.lat, station.lng), (place.lat, place.lng)))
            if nearby and distance((station.lat, station.lng), (nearby[0].lat, nearby[0].lng)) <= 300:
                station.rest_area_name, station.rest_area_ref_id = nearby[0].name, nearby[0].ref_id
        # Keep route coverage instead of consuming the whole shortlist at origin.
        ordered = sorted(result.stations, key=lambda station: station.route_progress_m)
        # Keep about one station per 90 km on long routes. A fixed eight-node
        # graph leaves gaps larger than the usable charging range across VN.
        shortlist_limit = min(24, max(8, math.ceil(route["distance_m"] / 90000) + 1))
        if len(ordered) > shortlist_limit:
            # Space by distance, not vendor result rank: urban clusters must
            # not steal slots from the sparse later corridor.
            chosen = []
            for i in range(shortlist_limit):
                target = ordered[0].route_progress_m + i / (shortlist_limit - 1) * (
                    ordered[-1].route_progress_m - ordered[0].route_progress_m
                )
                station = min((s for s in ordered if s not in chosen), key=lambda s: abs(s.route_progress_m - target))
                chosen.append(station)
            # A vendor-named rest area gets priority, without inventing a rest
            # area label for other POIs merely near an expressway.
            rest = [s for s in ordered if s.rest_area_name or charging_poi_info(s.name)["rest_area"]]
            for station in rest[:3]:
                if station not in chosen:
                    # Prefer a rest area only within its own corridor segment.
                    # Replacing the final slots with earlier rest areas removes
                    # late-route coverage and can make long trips impossible.
                    index = min(
                        range(len(chosen)), key=lambda i: abs(chosen[i].route_progress_m - station.route_progress_m)
                    )
                    if abs(chosen[index].route_progress_m - station.route_progress_m) <= route["distance_m"] / (
                        (len(chosen) - 1) * 3
                    ):
                        chosen[index] = station
            ordered = sorted(chosen, key=lambda station: station.route_progress_m)
            result.incomplete = True
        result.stations, result.errors = ordered, sorted(set(result.errors))
        return result
