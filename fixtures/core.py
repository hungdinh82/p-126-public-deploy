"""Explicit model replies and isolated runtime dependencies for core contracts."""

import asyncio
import json
from pathlib import Path

from src.vivi.dialogue.planner import SemanticResolver
from src.vivi.graphs.assistant import build_assistant_graph
from src.vivi.history.sqlite import SQLiteConversationHistory
from src.vivi.memory.sqlite import SQLiteLongTermMemory
from src.vivi.model_gateway import ModelGateway, ModelProvider
from src.vivi.navigation.energy import EnergyPlanner, EnergyProfile
from src.vivi.navigation.vietmap import Place, Route
from src.vivi.persistence.checkpoints import ThreadedSqliteSaver
from src.vivi.persistence.confirmations import DurableConfirmations
from src.vivi.persistence.runtime_store import RuntimeStore
from src.vivi.rag.generator import ExtractiveHandbookGenerator
from src.vivi.rag.runtime import HandbookServices
from src.vivi.vehicle.gateway import VehicleActionGateway
from src.vivi.vehicle.memory import VehicleSimulator


class ScriptedModel:
    def __init__(self, replies=(), *, destination=None):
        self.replies = iter(replies)
        self.destination = destination or {"mode": "propose", "handle": "first", "reason": "exact_destination"}
        self.calls = []

    async def agenerate_json(self, **kwargs):
        self.calls.append(kwargs)
        if kwargs["schema_name"] == "destination_choice":
            result = dict(self.destination)
            if result.get("handle") == "first":
                result["handle"] = json.loads(kwargs["user"])["candidates"][0]["handle"]
            return json.dumps(result)
        reply = next(self.replies)
        if isinstance(reply, BaseException):
            raise reply
        return reply if isinstance(reply, str) else json.dumps(reply, ensure_ascii=False)


class EmptyRetriever:
    def retrieve(self, *_):
        return []


class Maps:
    def __init__(self):
        self.queries = []
        self.routes = []

    async def search(self, text, **kwargs):
        self.queries.append(text)
        return [Place(ref_id="destination", name=text, address="Ninh Bình", lat=20.25, lng=105.92)]

    async def route(self, origin, destination):
        self.routes.append((origin, destination))
        return Route(
            distance_m=10000,
            duration_s=900,
            coordinates=[[origin[1], origin[0]], [destination[1], destination[0]]],
            steps=[],
        )


class Harness:
    def __init__(self, root: Path, replies=(), *, model=None, maps=None, offline=False, vehicle=None, semantic_budget=20):
        self.model = model or ScriptedModel(replies)
        self.maps = maps or Maps()
        self.ledger = RuntimeStore(root / "runtime.sqlite3")
        self.vehicle = vehicle or VehicleSimulator()
        self.gateway = VehicleActionGateway(self.vehicle, DurableConfirmations(self.ledger), ledger=self.ledger)
        self.services = HandbookServices(
            EmptyRetriever(),
            ExtractiveHandbookGenerator(),
            SQLiteConversationHistory(root / "history.sqlite3"),
            action_gateway=self.gateway,
            navigation=self.maps,
            memory=SQLiteLongTermMemory(root / "memory.sqlite3"),
            energy_planner=EnergyPlanner(
                EnergyProfile(
                    id="test",
                    vehicle_model="VF8",
                    model_year=2026,
                    edition="test",
                    usable_kwh=80,
                    consumption_kwh_per_100km=20,
                    max_charge_kw=120,
                    charge_curve=[{"until_soc": 100, "power_kw": 100}],
                    source="synthetic",
                    updated_at="2026-10-15",
                    simulated=True,
                )
            ),
        )
        self.models = ModelGateway(
            {"google": ModelProvider("google", self.model, 0.5)}, offline=offline, breaker_seconds=0
        )
        self.saver = ThreadedSqliteSaver(root / "checkpoint.sqlite3")
        self.graph = build_assistant_graph(
            self.services, self.saver, self.ledger, resolver=SemanticResolver(gateway=self.models, budget_seconds=semantic_budget)
        )
        self.graph.default_provider = "google"
        self.number = 0

    async def turn(self, text="", **extra):
        self.number += 1
        return await asyncio.wait_for(
            self.graph.ainvoke(
                {
                    "session_id": "driver",
                    "turn_id": str(self.number),
                    "input_text": text,
                    "position": {"lat": 21, "lng": 105.85},
                    **extra,
                }
            ),
            5,
        )

    async def close(self):
        await self.graph.aclose()


def action(capability, arguments=None, **extra):
    return {"act": "request", "actions": [{"capability": capability, "arguments": arguments or {}}], **extra}
