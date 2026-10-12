"""Deterministic EV planning. Model outputs never supply energy measurements."""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, Field, model_validator


class ChargeBand(BaseModel):
    until_soc: float = Field(gt=0, le=100)
    power_kw: float = Field(gt=0)


class EnergyProfile(BaseModel):
    id: str
    vehicle_model: str
    model_year: int = Field(ge=2000, le=2100)
    edition: str
    usable_kwh: float = Field(gt=0)
    consumption_kwh_per_100km: float = Field(gt=0)
    max_charge_kw: float = Field(gt=0)
    charge_curve: list[ChargeBand] = Field(min_length=1)
    connector: str = "CCS2"
    source: str = Field(min_length=1)
    updated_at: str = Field(min_length=1)
    simulated: bool = False
    conservative_factor: float = Field(default=1.15, ge=1, le=2)

    @model_validator(mode="after")
    def ordered_curve(self):
        limits = [band.until_soc for band in self.charge_curve]
        if sorted(set(limits)) != limits or limits[-1] != 100:
            raise ValueError("Charging curve must increase and cover 100%")
        return self


class ChargingStation(BaseModel):
    id: str
    name: str
    lat: float = Field(ge=-90, le=90)
    lng: float = Field(ge=-180, le=180)
    network: str
    connector: str
    power_kw: float | None = Field(default=None, gt=0)
    max_soc: float = Field(default=100, gt=15, le=100)
    accessible: bool = True
    source: str = Field(min_length=1)
    updated_at: str = Field(min_length=1)
    simulated: bool = False
    address: str = ""
    map_ref_id: str | None = None
    location_source: str | None = None
    road_context: str = ""
    route_progress_m: float | None = None
    detour_m: float | None = None
    rest_area_name: str = ""
    rest_area_ref_id: str | None = None
    equipment_basis: Literal["verified", "assumed", "missing"] = "verified"

    @model_validator(mode="after")
    def network_limit(self):
        if self.network.lower() in {"vinfast", "v-green", "vgreen"} and (self.power_kw or 0) >= 250:
            self.max_soc = min(80, self.max_soc)
        return self


class ChargingAssumptions(BaseModel):
    power_kw: float = Field(gt=0, le=500)
    connector: Literal["CCS2"] = "CCS2"
    max_soc: float = Field(gt=15, le=100)
    networks: list[str]
    source: str = Field(min_length=1)
    updated_at: str = Field(min_length=1)


def load_assumptions(path, enabled=True):
    if not enabled or not path or not path.is_file():
        return None
    return ChargingAssumptions.model_validate_json(path.read_text(encoding="utf-8"))


def load_stations(path: Path | None) -> list[ChargingStation]:
    if not path or not path.is_file():
        return []
    if path.suffix.lower() == ".csv":
        with path.open(encoding="utf-8") as source:
            records = list(csv.DictReader(source))
        records = [{k: v for k, v in row.items() if v != ""} for row in records]
    else:
        records = json.loads(path.read_text(encoding="utf-8"))
    return [ChargingStation.model_validate(row) for row in records]


class EnergyPlanner:
    def __init__(self, profile: EnergyProfile | None = None, stations=(), reserve=15):
        self.profile, self.stations, self.reserve = profile, list(stations), reserve

    def consumption(self, distance_m):
        profile = self.profile
        return distance_m / 1000 * profile.consumption_kwh_per_100km / 100 * profile.conservative_factor

    def soc_cost(self, distance_m):
        return self.consumption(distance_m) / self.profile.usable_kwh * 100

    def charging_minutes(self, station, arrival, departure):
        if not station.power_kw or departure > station.max_soc + 1e-6 or arrival > departure:
            raise ValueError("Insufficient charging data or unsupported target SOC")
        total, lower = 0.0, 0.0
        for band in self.profile.charge_curve:
            width = max(0, min(departure, band.until_soc) - max(arrival, lower))
            if width:
                power = min(band.power_kw, self.profile.max_charge_kw, station.power_kw)
                total += self.profile.usable_kwh * width / 100 / power * 60
            lower = band.until_soc
        return total + 5  # connection/payment allowance; queue time is unknown

    def compatible_stations(self):
        if not self.profile:
            return []
        return [
            s
            for s in self.stations
            if s.accessible
            and s.connector == self.profile.connector
            and s.network.lower() in {"vinfast", "v-green", "vgreen"}
            and s.power_kw
        ]

    def soc_after_charge(self, station, arrival, departure, elapsed_s):
        """Same charging curve as planning, including five-minute setup."""
        remaining = max(0, elapsed_s / 60 - 5)
        soc, lower = arrival, 0.0
        for band in self.profile.charge_curve:
            upper = min(departure, band.until_soc)
            start = max(soc, lower)
            if upper > start:
                power = min(band.power_kw, self.profile.max_charge_kw, station.power_kw)
                minutes = self.profile.usable_kwh * (upper - start) / 100 / power * 60
                if remaining < minutes:
                    return start + remaining * power / 60 / self.profile.usable_kwh * 100
                remaining -= minutes
                soc = upper
            lower = band.until_soc
        return min(soc, departure)

    @staticmethod
    def stop(station, arrival, departure, minutes, leg_index=None, phase=0):
        # Persist equipment as well as the map identity. A restart must not need
        # a repeat map lookup to advance an already confirmed charging plan.
        return {
            **station.model_dump(),
            "station_id": station.id,
            "visit_id": str(uuid4()),
            "leg_index": leg_index,
            "phase": phase,
            "arrival_soc": round(arrival, 2),
            "departure_soc": round(departure, 2),
            "charge_minutes": round(minutes, 1),
            "queue_status": "unknown",
        }

    def solve(
        self, origin, destination, soc, edges, stations, reserve=None, waypoints=None, allowed_stations_by_phase=None
    ):
        """Bounded label search with exact leg SOC, no charging/road shortcuts.

        edges[(a,b)] are verified road routes. Retain a Pareto frontier per
        (visited set, current node), so early arrival and higher SOC both survive.
        """
        self.last_search_incomplete = False
        reserve = reserve or self.reserve
        waypoints = waypoints or ["destination"]
        nodes = {s.id: s for s in stations}
        frontier = [("origin", float(soc), 0.0, [], [], frozenset(), 0)]
        solutions = []
        # Visiting a station does not necessarily mean charging there. The
        # previous five-visit bound could stop a valid long corridor before
        # its final leg even though all discovered road edges were feasible.
        for _ in range((min(len(stations), 12) + 1) * len(waypoints)):
            following, labels = [], {}
            for current, arrival, elapsed, stops, legs, visited, phase in frontier:
                available = allowed_stations_by_phase.get(phase, []) if allowed_stations_by_phase is not None else nodes
                for next_id in [waypoints[phase], *available]:
                    if next_id == current or next_id in visited:
                        continue
                    leg = edges.get((current, next_id))
                    if not leg:
                        continue
                    cost = self.soc_cost(leg["distance_m"])
                    required = cost + reserve
                    if required > 100:
                        continue
                    departing, wait, next_stops = arrival, 0.0, list(stops)
                    if current in nodes and required > arrival:
                        station = nodes[current]
                        departing = required
                        if departing > station.max_soc:
                            continue
                        wait = self.charging_minutes(station, arrival, departing) * 60
                        next_stops.append(self.stop(station, arrival, departing, wait / 60, len(legs) - 1, phase))
                    # Also keep a full-charge label at each station below. A
                    # minimal charge to the next stop alone can hide fewer stops.
                    if departing - cost < reserve - 1e-6:
                        continue
                    next_legs = legs + [
                        {
                            "from": current,
                            "to": next_id,
                            "phase": phase,
                            "departure_soc": round(departing, 2),
                            "arrival_soc": round(departing - cost, 2),
                            **leg,
                        }
                    ]
                    new_elapsed = elapsed + wait + leg["duration_s"]
                    if next_id == waypoints[phase] and phase == len(waypoints) - 1:
                        solutions.append(
                            {
                                "id": str(uuid4()),
                                "stops": next_stops,
                                "legs": next_legs,
                                "arrival_soc": round(departing - cost, 2),
                                "total_seconds": round(new_elapsed),
                                "distance_m": sum(leg["distance_m"] for leg in next_legs),
                                "reserve_soc": reserve,
                                "profile_id": self.profile.id,
                                "simulated": self.profile.simulated or any(s["simulated"] for s in next_stops),
                                "assumptions": {
                                    "conservative_factor": self.profile.conservative_factor,
                                    "stop_overhead_minutes": 5,
                                    "queue_minutes": None,
                                },
                            }
                        )
                    elif next_id == waypoints[phase]:
                        # A destination is a waypoint, never a free recharge.
                        following.append(
                            (next_id, departing - cost, new_elapsed, next_stops, next_legs, frozenset(), phase + 1)
                        )
                    else:
                        candidate = (
                            next_id,
                            departing - cost,
                            new_elapsed,
                            next_stops,
                            next_legs,
                            visited | {next_id},
                            phase,
                        )
                        following.append(candidate)
                        station = nodes[next_id]
                        # Charge-on-arrival label: count one stop and preserve its
                        # incoming leg and original arrival SOC for the preview.
                        if candidate[1] < station.max_soc:
                            minutes = self.charging_minutes(station, candidate[1], station.max_soc)
                            full_stops = next_stops + [
                                self.stop(station, candidate[1], station.max_soc, minutes, len(next_legs) - 1, phase)
                            ]
                            following.append(
                                (
                                    next_id,
                                    station.max_soc,
                                    new_elapsed + minutes * 60,
                                    full_stops,
                                    next_legs,
                                    candidate[5],
                                    phase,
                                )
                            )
            for item in following:
                key = (item[0], item[6], item[5], len(item[3]))
                existing = labels.setdefault(key, [])
                if any(old[1] >= item[1] and old[2] <= item[2] for old in existing):
                    continue
                existing[:] = [old for old in existing if not (item[1] >= old[1] and item[2] <= old[2])]
                existing.append(item)
            frontier = [item for values in labels.values() for item in values]
            if len(frontier) > 2000:
                self.last_search_incomplete = True
            frontier = sorted(frontier, key=lambda x: (len(x[3]), x[2]))[:2000]
        if frontier:
            self.last_search_incomplete = True
        if not solutions:
            return []
        fewest = min(solutions, key=lambda p: (len(p["stops"]), p["total_seconds"]))
        fastest = min(solutions, key=lambda p: p["total_seconds"])
        choices = [dict(fewest, strategy="fewest_stops")]
        if fastest["id"] != fewest["id"]:
            choices.append(
                dict(
                    fastest,
                    strategy="fastest",
                    worthwhile_alternative=fewest["total_seconds"] - fastest["total_seconds"] >= 1200,
                )
            )
        return choices

    def assess(self, route, soc, range_km=None):
        if not self.profile:
            return {
                "status": "configuration_required",
                "message": "Chưa chọn hồ sơ pin/phiên bản xe để tính thời gian sạc.",
                "reserve_soc": self.reserve,
            }
        arrival = soc - self.soc_cost(route["distance_m"])
        return {
            "status": "sufficient" if arrival >= self.reserve else "charging_required",
            "arrival_soc": round(arrival, 2),
            "reserve_soc": self.reserve,
            "profile_id": self.profile.id,
            "simulated": self.profile.simulated,
            "source": self.profile.source,
            "estimated": True,
        }
