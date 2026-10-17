"""Replay Oct 11 Sầm Sơn follow-up using isolated state and a semantic fixture.

Default: recorded map geometry. --live-vietmap: read-only live map requests.
Never sends commands to the running backend or migrates its databases.
"""

import argparse
import asyncio
import json
import sqlite3
import tempfile
import time
from dataclasses import replace
from pathlib import Path

from src.vivi.config import Settings, get_settings
from src.vivi.domain.models import TurnRequest
from src.vivi.navigation.charging import VietMapChargingDiscovery
from src.vivi.navigation.energy import EnergyPlanner, EnergyProfile, load_assumptions
from src.vivi.navigation.vietmap import Place, Route, VietMapClient
from src.vivi.orchestration import LangGraphOrchestrator
from src.vivi.persistence.event_store import EventStore
from tests.support.dialogue_v2 import build
from tests.support.long_term_memory import services


class SemanticFixture:
    async def agenerate_json(self, **kwargs):
        prompt = json.loads(kwargs["user"])
        if kwargs["schema_name"] == "destination_choice":
            matching = [p for p in prompt["candidates"] if "bãi biển" in p["name"].lower()]
            return json.dumps(dict(mode="propose", handle=matching[0]["handle"], reason="exact_destination")) if len(matching) == 1 else json.dumps(
                dict(mode="clarify", handle=None, reason="ambiguous")
            )
        return json.dumps(dict(act="request", actions=[{
            "capability": "trip.plan", "arguments": {
                "query": "bãi biển sầm sơn", "trip_kind": "round_trip" if "quay về" in prompt["utterance"] else "one_way",
            },
        }]))


class RecordedMaps:
    def __init__(self, plan, returning):
        self.plan, self.returning = plan, returning
        self.routes = []

    async def search(self, *args, **kwargs):
        return [Place.model_validate(self.plan["selected"])]

    async def route(self, a, b):
        self.routes.append((a, b))
        outbound = (self.plan["origin"]["lat"], self.plan["origin"]["lng"])
        return Route.model_validate(self.plan["waypoint_routes"][0] if a == outbound else self.returning)


async def replay(live=False, live_model=False):
    records = [json.loads(line) for line in Path("data/2026-10-11/turns.jsonl").read_text().splitlines()]
    original = next(item for item in records if item.get("transcript") == "bãi biển sầm sơn")
    plan = original["trip_plan"]
    with sqlite3.connect("file:data/runtime/state.sqlite3?mode=ro", uri=True) as db:
        plans = [json.loads(row[0]).get("trip_plan") for row in db.execute("SELECT payload FROM navigation_sessions")]
    round_trip = next(item for item in plans if item and item.get("trip_kind") == "round_trip" and item.get("selected", {}).get("ref_id") == plan["selected"]["ref_id"])
    settings = get_settings()
    maps = VietMapClient(settings) if live else RecordedMaps(plan, round_trip["waypoint_routes"][1])
    energy = EnergyPlanner(EnergyProfile.model_validate_json(settings.energy_profile_path.read_text()))
    discovery = VietMapChargingDiscovery(maps, assumptions=load_assumptions(settings.charging_assumptions_path)) if live else None
    with tempfile.TemporaryDirectory(prefix="vivi-sam-son-replay-") as directory:
        root = Path(directory)
        runtime = replace(services.__wrapped__(root), navigation=maps, energy_planner=energy, charging_discovery=discovery)
        model = SemanticFixture()
        if live_model:
            from src.vivi.rag.generator import GoogleHandbookGenerator

            if not settings.google_api_key:
                raise RuntimeError("Google API key is not configured; do not count a rules fallback as live-model acceptance")
            model = GoogleHandbookGenerator(settings).client
            model.max_tokens, model.max_attempts = settings.planner_max_tokens, 1
        graph, ledger, gateway = build(runtime, root, model)
        if live_model:
            graph.navigation.workflow.resolver.providers["google"].budget_seconds = settings.cloud_budget_seconds
        gateway.vehicle.set_energy("driver", plan["initial_soc"], 328)
        pipeline = LangGraphOrchestrator({"google": graph}, gateway, EventStore(Settings(data_dir=root)), ledger=ledger)
        report = []
        try:
            for index, text in enumerate(["lập lộ trình đi và sạc tới bãi biển sầm sơn", "tôi sẽ đến đấy chơi một chút xong quay về. lúc về cần sạc không"]):
                started = time.perf_counter()
                result = await pipeline.run(TurnRequest(session_id="driver", turn_id=f"replay-{index}", transcript=text, position=plan["origin"]), "google")
                with sqlite3.connect(graph.saver.connection.execute("PRAGMA database_list").fetchone()[2]) as db:
                    checkpoint = db.execute("SELECT length(checkpoint) FROM checkpoints ORDER BY checkpoint_id DESC LIMIT 1").fetchone()[0]
                current = result.trip_plan
                report.append({"text": text, "wall_ms": round((time.perf_counter() - started) * 1000, 2),
                               "status": result.status, "calculation_status": current.get("calculation_status"),
                               "trip_kind": current.get("trip_kind"), "message": result.message,
                               "latency_ms": result.latency_ms, "checkpoint_bytes": checkpoint,
                               "decision_source": result.diagnostics.get("effective_provider"),
                               "semantic": result.diagnostics.get("semantic"),
                               "provider_attempts": result.diagnostics.get("provider_attempts"),
                               "driving": gateway.vehicle.state_for("driver").driving})
                assert not gateway.vehicle.state_for("driver").driving
            return {"maps": "live_vietmap" if live else "recorded_fixture", "model": "live_google" if live_model else "semantic_fixture",
                    "accepted": report[-1]["trip_kind"] == "round_trip", "turns": report}
        finally:
            await graph.aclose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--live-vietmap", action="store_true")
    parser.add_argument("--live-model", action="store_true")
    parser.add_argument("--output", type=Path)
    options = parser.parse_args()
    report = asyncio.run(replay(options.live_vietmap, options.live_model))
    text = json.dumps(report, ensure_ascii=False, indent=2)
    print(text)
    if options.output:
        options.output.parent.mkdir(parents=True, exist_ok=True)
        options.output.write_text(text + "\n")
