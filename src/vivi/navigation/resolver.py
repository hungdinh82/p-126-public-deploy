"""Place resolution is shared by navigation and trip; no vehicle effects."""

import asyncio
import json
import math
from collections import OrderedDict
from time import monotonic, perf_counter
from uuid import uuid4

from src.vivi.navigation.categories import CATEGORIES, matches_category
from src.vivi.navigation.contracts import DestinationSpec, PlaceResolution
from src.vivi.navigation.destinations import (
    accented_name_match,
    administrative_layer,
    administrative_match,
    landmark_for,
    landmark_query,
)
from src.vivi.navigation.metrics import record
from src.vivi.navigation.presentation import place_label, short_address, short_name
from src.vivi.navigation.reads import read_many
from src.vivi.navigation.selection import destination_rank, distinct_places
from src.vivi.navigation.vietmap import Place, VietMapError
from src.vivi.text import normalize_text


def coordinates(place):
    return place["lat"], place["lng"]


# Nearby search widens in rings and stops at the first ring with enough results.
NEARBY_RINGS_M = (2000, 5000, 15000, 30000)
NEARBY_ENOUGH = 3


def straight_distance(a, b):
    dlat, dlng = math.radians(b[0] - a[0]), math.radians(b[1] - a[1])
    h = math.sin(dlat / 2) ** 2 + math.cos(math.radians(a[0])) * math.cos(math.radians(b[0])) * math.sin(dlng / 2) ** 2
    return 6371000 * 2 * math.asin(min(1, math.sqrt(h)))


def prefer_spoken_accents(places, query):
    """Folded matching equates "Phủ Lý" with "Phú Lý"; keep the spoken one."""
    exact = [p for p in places if accented_name_match(p.model_dump(), query)]
    return exact or places


def satisfies_area(place, area):
    if area is None or area.constraint != "required":
        return True
    from src.vivi.navigation.destinations import region_for, region_name

    region = region_for(area.text)
    aliases = [region["name"], *region.get("aliases", [])] if region else [region_name(area.text)]
    address_areas = [region_for(p.strip()) for p in place.get("address", "").split(",")]
    boundary_areas = [region_for(b.get(key, "")) for b in place.get("boundaries", []) for key in ("full_name", "name")]
    known_areas = [r for r in [*address_areas, *boundary_areas] if r]
    if region and known_areas and region not in known_areas:
        return False
    locality = normalize_text(
        " ".join(
            [place.get("address", ""), *(b.get("full_name") or b.get("name", "") for b in place.get("boundaries", []))]
        )
    )
    if not any(address_areas):
        locality += " " + normalize_text(",".join(place.get("name", "").split(",")[1:]))
    return any(normalize_text(a) in locality for a in aliases)


class PlaceResolver:
    def __init__(self, client, categories=None, *, budget_seconds=4, cache_seconds=30):
        self.client, self.categories = client, categories or {}
        self.budget_seconds = budget_seconds
        self.cache_seconds = cache_seconds
        self._cache = OrderedDict()

    async def provider_search(self, query, **kwargs):
        key = json.dumps([query, kwargs], sort_keys=True, ensure_ascii=False)
        cached = self._cache.get(key)
        if cached and monotonic() - cached[0] <= self.cache_seconds:
            self._cache.move_to_end(key)
            return [Place.model_validate(p) for p in json.loads(cached[1])]
        places = await self.client.search(query, **kwargs)
        # Cache immutable provider metadata, never UI handles or route evidence.
        if places and self.cache_seconds > 0:
            self._cache[key] = (monotonic(), json.dumps([p.model_dump(mode="json") for p in places]))
            while len(self._cache) > 128:
                self._cache.popitem(last=False)
        return places

    @staticmethod
    def query_plan(destination):
        query = destination.query()
        if destination.area and destination.area.constraint == "preferred":
            original = landmark_query(destination.model_copy(update={"area": None}).query())
            if destination.area.source == "context":
                return [(landmark_query(query), "context"), (original, "original")][:2]
            return [(original, "original"), (query, "hypothesis")][:2]
        canonical = landmark_query(query)
        result = [(canonical, "verified_alias" if canonical != query else "original")]
        if canonical != query:
            result.append((query, "original"))
        result.extend((q, "hypothesis") for q in destination.search_hypotheses if q not in {canonical, query})
        return result[:2]

    async def resolve(self, destination, origin, *, representative=False, with_travel=False):
        started = perf_counter()
        try:
            return await self._resolve(destination, origin, representative=representative, with_travel=with_travel)
        finally:
            record("navigation_place_resolution", started)

    async def _resolve(self, destination, origin, *, representative=False, with_travel=False):
        if not destination.name and destination.selection != "category":
            return PlaceResolution(status="missing_constraints", reason="missing_name", required_slot="name")
        started = monotonic()
        arguments = {
            "query": destination.query(),
            "nearest": destination.nearest,
            "category": destination.entity_type if destination.entity_type in CATEGORIES else None,
        }
        errors = []
        arguments["_errors"] = errors
        queries = []
        if destination.area and destination.area.constraint == "preferred":
            arguments["query"] = destination.model_copy(update={"area": None}).query()
        try:
            async with asyncio.timeout(self.budget_seconds):
                candidates = []
                for query, basis in self.query_plan(destination):
                    queries.append(query)
                    candidates = await self.lookup(
                        {**arguments, "_provider_query": query, "_basis": basis},
                        origin,
                        representative=representative,
                        with_travel=with_travel or destination.nearest,
                    )
                    if candidates:
                        break
                    if errors:
                        return PlaceResolution(
                            status="provider_unavailable",
                            reason="map_metadata_unavailable",
                            errors=errors,
                            queries=queries,
                        )
            if not candidates and self.nearby_request(arguments, origin, representative):
                # Asking for an area makes no sense when the car's position is known.
                return PlaceResolution(
                    status="no_verified_match",
                    reason="no_nearby_candidates",
                    required_slot="area",
                    queries=queries,
                    evidence=[{"radius_m": NEARBY_RINGS_M[-1]}],
                )
            if not candidates:
                return PlaceResolution(
                    status="missing_constraints" if not destination.area else "no_verified_match",
                    reason="no_verified_candidates",
                    required_slot="area" if not destination.area else "name",
                    queries=queries,
                )
            from src.vivi.navigation.selection import candidate_evidence, exact_destination

            evidence = candidate_evidence(arguments["query"], candidates)
            decision = exact_destination(arguments["query"], candidates)
            return PlaceResolution(
                status="resolved" if decision else "ambiguous",
                candidates=candidates,
                selected_handle=decision["handle"] if decision else None,
                evidence=evidence,
                reason="exact_destination" if decision else "insufficient_margin",
                queries=queries,
                errors=errors,
            )
        except (VietMapError, TimeoutError) as exc:
            return PlaceResolution(
                status="provider_unavailable",
                reason="map_read_failed",
                queries=queries,
                errors=[type(exc).__name__],
                evidence=[{"elapsed_ms": round((monotonic() - started) * 1000, 2)}],
            )

    @staticmethod
    def nearby_request(arguments, origin, representative=False):
        return bool(
            arguments.get("category") in CATEGORIES
            and arguments.get("nearest")
            and origin
            and origin.get("lat") is not None
            and not representative
        )

    async def nearby(self, arguments, origin, *, with_travel=True):
        """Category POIs around the car, widest ring only when inner ones are sparse."""
        item = CATEGORIES[arguments["category"]]
        near = coordinates(origin)
        code = (self.categories or {}).get(item.key) or item.codes[0]
        query = arguments.get("_provider_query") or arguments["query"]
        found = {}
        for radius in NEARBY_RINGS_M:
            kwargs = {"near": near, "radius_m": radius, "layers": "POI"}
            places = await self.provider_search(query, **kwargs, category=code)
            if not places and query != item.query:
                places = await self.provider_search(item.query, **kwargs, category=code)
            if not places:
                # An unknown code must not hide every result; identity is checked below.
                places = await self.provider_search(query, **kwargs)
            places = [p for p in places if p.ref_id not in found and matches_category(p.model_dump(), item.key)]
            missing = [p for p in places if p.lat is None or p.lng is None]
            details = await read_many(self.client, [("place", (p.ref_id,), {}) for p in missing])
            details = dict(zip((p.ref_id for p in missing), details))
            for place in places:
                if place.lat is None or place.lng is None:
                    detail = details.get(place.ref_id)
                    if detail is None or isinstance(detail, Exception):
                        if isinstance(detail, Exception):
                            arguments.get("_errors", []).append(type(detail).__name__)
                        continue
                    place = place.model_copy(
                        update={"lat": detail.lat, "lng": detail.lng, "address": place.address or detail.address}
                    )
                value = place.model_dump()
                value["_straight_m"] = straight_distance(near, (place.lat, place.lng))
                if value["_straight_m"] <= radius * 1.25:
                    found[place.ref_id] = value
            if len(found) >= NEARBY_ENOUGH:
                break
        destination = DestinationSpec.from_arguments(arguments)
        candidates = [
            {
                **value,
                "handle": "place:" + str(uuid4()),
                "category": item.key,
                "spoken_label": place_label(value),
                "short_address": short_address(value.get("address", "")),
            }
            for value in sorted(found.values(), key=lambda v: v["_straight_m"])
            if satisfies_area(value, destination.area)
        ]
        candidates = distinct_places(candidates)[:5]
        for place in candidates:
            place["straight_distance_m"] = round(place.pop("_straight_m"))
        if not with_travel:
            return candidates[:3]
        return await self.travel(candidates, origin, arguments, nearest=True)

    async def travel(self, candidates, origin, arguments, *, nearest):
        """One matrix call ranks the shortlist; only the shown places get a route."""
        distances = None
        if candidates and hasattr(self.client, "matrix"):
            try:
                distances = await self.client.matrix([coordinates(origin), *map(coordinates, candidates)])
            except Exception:
                pass
        indexed = [
            (index, place)
            for index, place in enumerate(candidates)
            if not (distances and index < len(distances) and (distances[index] is None or distances[index] < 0))
        ]
        if nearest and distances:
            indexed.sort(key=lambda pair: distances[pair[0]])
        indexed = indexed[:3] if nearest else indexed
        routes = await read_many(
            self.client, [("route", (coordinates(origin), coordinates(place)), {}) for _, place in indexed]
        )
        reachable = []
        for (index, place), route in zip(indexed, routes):
            if isinstance(route, Exception):
                arguments.get("_errors", []).append(type(route).__name__)
                continue
            place["distance_m"], place["duration_s"] = route.distance_m, route.duration_s
            place["_rank_distance"] = (
                distances[index]
                if distances and index < len(distances) and distances[index] is not None
                else route.distance_m
            )
            reachable.append(place)
        if nearest:
            reachable.sort(key=lambda p: p["_rank_distance"])
        for place in reachable:
            place.pop("_rank_distance")
        return reachable[:3]

    async def lookup(self, arguments, origin, *, representative=False, with_travel=True):
        if self.nearby_request(arguments, origin, representative):
            return await self.nearby(arguments, origin, with_travel=with_travel)
        category = arguments.get("category")
        categories = self.categories or {}
        # Without a verified category code use a bounded alias query plus a
        # local POI identity check, rather than fabricating a vendor code.
        kwargs = {"near": coordinates(origin)}
        query = landmark_query(arguments["query"]) if not category and not representative else arguments["query"]
        landmark = landmark_for(query)
        layer = administrative_layer(query) if not category else None
        admin_only = representative or layer is not None
        if admin_only:
            kwargs["layers"] = layer or "CITY"
        if category and categories.get(category):
            kwargs["category"] = categories[category]
        places = await self.provider_search(arguments.get("_provider_query", query), **kwargs)
        if not admin_only and not category:
            places.sort(key=lambda p: destination_rank(query, p.model_dump()))
        if admin_only:
            places = prefer_spoken_accents([p for p in places if administrative_match(p.model_dump(), query)], query)
            # The alias catalogue includes former cities/provinces. In v4 new
            # boundaries these names may identify wards, rather than CITY results.
            # Retry only when no verified result remains, never accept a business
            # or a renamed province merely because its address contains the name.
            if not places and representative and layer is None:
                places = await self.provider_search(query, **{**kwargs, "layers": "WARD"})
                places = prefer_spoken_accents(
                    [p for p in places if administrative_match(p.model_dump(), query)], query
                )
        missing = [place for place in places[:10] if place.lat is None or place.lng is None]
        details = await read_many(self.client, [("place", (place.ref_id,), {}) for place in missing])
        details = dict(zip((place.ref_id for place in missing), details))
        candidates = []
        for place in places[:10]:
            if category and not categories.get(category) and not matches_category(place.model_dump(), category):
                continue
            if place.lat is None or place.lng is None:
                detail = details.get(place.ref_id)
                if detail is None or isinstance(detail, Exception):
                    if isinstance(detail, Exception):
                        arguments.get("_errors", []).append(type(detail).__name__)
                    continue
                place = place.model_copy(
                    update={"lat": detail.lat, "lng": detail.lng, "address": place.address or detail.address}
                )
                if category and not categories.get(category) and not matches_category(place.model_dump(), category):
                    continue
            if place.lat is None or place.lng is None:
                continue
            value = place.model_dump()
            destination = DestinationSpec.from_arguments(arguments)
            if not satisfies_area(value, destination.area):
                continue
            if not admin_only and not category:
                from src.vivi.navigation.poi import place_query_matches

                match_query = (
                    arguments.get("_provider_query", query)
                    if arguments.get("_basis") in {"hypothesis", "context"}
                    else query
                )
                if not place_query_matches(match_query, value):
                    continue
                if landmark and normalize_text(short_name(value["name"])) not in {
                    normalize_text(name) for name in [landmark["name"], *landmark["aliases"]]
                }:
                    continue
            if all(p["ref_id"] != value["ref_id"] for p in candidates):
                if arguments.get("_basis") == "hypothesis":
                    value["match_basis"] = "hypothesis"
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
        if not admin_only and not category:
            commune = await self.named_commune(query, arguments, candidates)
            if commune:
                candidates = [commune, *candidates][:5]
        candidates = distinct_places(candidates)
        if not with_travel:
            return candidates[:3]
        return await self.travel(candidates, origin, arguments, nearest=bool(arguments.get("nearest")))

    async def named_commune(self, query, arguments, candidates):
        """ "Tà Xùa" is a commune, not the Hà Nội café that shares its name.

        Only consulted when no candidate carries exactly the spoken name; the
        alias catalogue covers provinces, not every commune and ward.
        """
        from src.vivi.navigation.contracts import entity_type

        # A spoken area ("Tà Xùa, Sơn La") already scopes the search, and a
        # typed venue ("hồ", "đường") is never a commune.
        # Folded names equate "Phủ Lý" with "Phú Lý"; the spoken accents decide.
        if "," in query or entity_type(query) or any(accented_name_match(c, query) for c in candidates):
            return None
        try:
            wards = await self.provider_search(query, near=None, layers="WARD")
        except VietMapError:
            return None
        ward = next((w for w in wards if accented_name_match(w.model_dump(), query)), None)
        if ward is None:
            return None
        if ward.lat is None or ward.lng is None:
            detail = (await read_many(self.client, [("place", (ward.ref_id,), {})]))[0]
            if isinstance(detail, Exception):
                arguments.get("_errors", []).append(type(detail).__name__)
                return None
            ward = ward.model_copy(update={"lat": detail.lat, "lng": detail.lng})
        value = ward.model_dump()
        destination = DestinationSpec.from_arguments(arguments)
        if not satisfies_area(value, destination.area):
            return None
        # "Xã Tà Xùa, Tỉnh Sơn La": the province is what tells it apart.
        parts = [part.strip() for part in value.get("address", "").split(",") if part.strip()]
        province = parts[-1] if len(parts) > 1 else ""
        return {
            **value,
            "handle": "place:" + str(uuid4()),
            "category": None,
            "spoken_label": ", ".join(p for p in [value["name"], province] if p),
            "short_address": province or short_address(value.get("address", "")),
        }
