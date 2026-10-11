"""Synthetic ports for the real LangGraph, policy, simulator and SQLite runtime."""

import asyncio
import json
import math

from src.vivi.dialogue.parser import OfflineParser
from src.vivi.dialogue.planner import SemanticResolver
from src.vivi.navigation.energy import ChargeBand, EnergyProfile
from src.vivi.navigation.vietmap import Place, Route, VietMapError
from src.vivi.text import normalize_text


def energy_profile():
    return EnergyProfile(id="eval-vf8", vehicle_model="VF8", model_year=2026, edition="FIXTURE",
                         usable_kwh=80, consumption_kwh_per_100km=20, max_charge_kw=100,
                         conservative_factor=1, charge_curve=[ChargeBand(until_soc=80, power_kw=100),
                                                              ChargeBand(until_soc=100, power_kw=40)],
                         source="Synthetic evaluation fixture, not a verified vehicle specification", updated_at="2026-10-11")


class FixtureMaps:
    def __init__(self):
        self.delay = 0
        self.fail = False
        self.points = 2
        self.calls = []
        self.entered = asyncio.Event()
        self.release = None

    async def wait(self, name):
        self.calls.append(name)
        self.entered.set()
        if self.release:
            await self.release.wait()
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.fail:
            raise VietMapError("Fixture map unavailable")

    async def search(self, query, **kwargs):
        await self.wait("search")
        value = normalize_text(query)
        if "khong ton tai" in value:
            return []
        if kwargs.get("layers"):
            return [Place(ref_id="center", name=query, lat=20.5, lng=105.9,
                          boundaries=[{"name": query, "full_name": query}])]
        if "aeon" in value:
            return [Place(ref_id=f"aeon-{i}", name=name, address="Phường Long Biên, Hà Nội",
                          lat=21.01 + i * .01, lng=105.9 + i * .01)
                    for i, name in enumerate(["AEON Mall Long Biên", "Aeon Mall Long Biên Đường Cổ Linh"])]
        return [Place(ref_id="destination", name=query, address="Địa điểm fixture", lat=20.5, lng=105.9)]

    async def route(self, a, b):
        await self.wait("route")
        distance = math.hypot((a[0] - b[0]) * 111000, (a[1] - b[1]) * 103000) * 1.2
        return Route(distance_m=distance, duration_s=distance / 20,
                     coordinates=[[a[1] + (b[1] - a[1]) * i / (self.points - 1),
                                   a[0] + (b[0] - a[0]) * i / (self.points - 1)] for i in range(self.points)], steps=[])


class FixtureModel:
    def __init__(self):
        self.session = None
        self.semantic = None
        self.fault = None
        self.calls = 0
        self.entered = asyncio.Event()
        self.release = None

    async def agenerate_json(self, **kwargs):
        self.calls += 1
        self.entered.set()
        if self.release:
            await self.release.wait()
        if self.fault == "timeout":
            await asyncio.Future()
        if self.fault == "invalid_json":
            return "not-json"
        prompt = json.loads(kwargs["user"])
        if kwargs["schema_name"] == "destination_choice":
            return json.dumps(dict(mode="clarify", handle=None, reason="ambiguous"))
        if self.semantic is not None:
            return json.dumps(self.semantic, ensure_ascii=False)
        # A grammar-backed semantic stub tests provider/graph contracts, not NLU.
        return OfflineParser().interpret(prompt["utterance"], self.session).model_dump_json()


class FixtureResolver(SemanticResolver):
    async def resolve(self, text, session, **kwargs):
        if "fixture" in self.providers:
            self.providers["fixture"].client.session = session
        return await super().resolve(text, session, **kwargs)
