"""Opt-in model/parser evaluation: synthetic POIs, temporary DBs, simulated car."""

import argparse
import asyncio
import json
import statistics
import tempfile
import time
from pathlib import Path

from src.vivi.config import Settings
from src.vivi.dialogue.planner import ModelProvider, SemanticResolver
from src.vivi.graphs.assistant import build_assistant_graph
from src.vivi.history.sqlite import SQLiteConversationHistory
from src.vivi.memory.sqlite import SQLiteLongTermMemory
from src.vivi.navigation.energy import EnergyPlanner, EnergyProfile
from src.vivi.persistence.checkpoints import ThreadedSqliteSaver
from src.vivi.persistence.confirmations import DurableConfirmations
from src.vivi.persistence.runtime_store import RuntimeStore
from src.vivi.rag.generator import ExtractiveHandbookGenerator
from src.vivi.rag.runtime import HandbookServices
from src.vivi.structured_llm import google_client
from src.vivi.vehicle.gateway import VehicleActionGateway
from tests.test_dialogue_audit_oct10 import NearbyMaps
from tests.test_long_term_memory import NoRetrieval


async def benchmark(args):
    cases = [json.loads(line) for line in args.dataset.read_text().splitlines() if line.strip()]
    results, tiers = [], {}
    with tempfile.TemporaryDirectory(prefix="vivi-navigation-voice-") as directory:
        root = Path(directory)
        settings = Settings()
        providers = {}
        if args.provider == "google":
            client = google_client(settings)
            client.max_tokens, client.max_attempts = 2048, 1
            providers["google"] = ModelProvider("google", client, 10)
        ledger = RuntimeStore(root / "runtime.sqlite3")
        gateway = VehicleActionGateway(confirmations=DurableConfirmations(ledger), ledger=ledger)
        profile = EnergyProfile.model_validate_json(Path("data/energy/vf8-plus.json").read_text())
        services = HandbookServices(
            NoRetrieval(),
            ExtractiveHandbookGenerator(),
            SQLiteConversationHistory(root / "history.sqlite3"),
            action_gateway=gateway,
            memory=SQLiteLongTermMemory(root / "memory.sqlite3"),
            navigation=NearbyMaps(),
            energy_planner=EnergyPlanner(profile),
        )
        graph = build_assistant_graph(
            services, ThreadedSqliteSaver(root / "checkpoint.sqlite3"), ledger, resolver=SemanticResolver(providers)
        )
        try:
            for case in cases:
                turns = []
                for index, turn in enumerate(case["turns"]):
                    if args.interval:
                        await asyncio.sleep(args.interval)
                    started = time.perf_counter()
                    response = await graph.ainvoke(
                        {
                            "session_id": case["id"],
                            "turn_id": str(index),
                            "input_text": turn["query"],
                            "provider": args.provider,
                            "position": {"lat": 21, "lng": 105.8, "simulated": True},
                        }
                    )
                    latency = (time.perf_counter() - started) * 1000
                    state = gateway.vehicle.state_for(case["id"])
                    actual = {
                        "status": response["status"],
                        "navigation": response["navigation"]["status"],
                        "temperature": state.temperature_celsius,
                        "trunk": state.trunk_open,
                        "hood": state.hood_open,
                        "executed": bool((response.get("execution") or {}).get("executed")),
                        "capabilities": [s["capability"] for s in response["steps"]],
                        "step_statuses": [s["status"] for s in response["steps"]],
                    }
                    checks = {key: actual[key] == value for key, value in turn.items() if key != "query"}
                    served = response["diagnostics"]["effective_provider"]
                    row = tiers.setdefault(
                        served, {"latencies": [], "correct": 0, "clarifications": 0, "unexpected_executions": 0}
                    )
                    row["latencies"].append(latency)
                    row["correct"] += all(checks.values())
                    row["clarifications"] += response["status"] == "clarify"
                    row["unexpected_executions"] += turn.get("executed") is False and actual["executed"]
                    turns.append(
                        {
                            "query": turn["query"],
                            "passed": all(checks.values()),
                            "checks": checks,
                            "actual": actual,
                            "message": response["response_text"],
                            "diagnostics": response["diagnostics"],
                        }
                    )
                results.append({"id": case["id"], "passed": all(t["passed"] for t in turns), "turns": turns})
        finally:
            graph.close()
    return {
        "dataset": str(args.dataset),
        "provider": args.provider,
        "split": "development-regression",
        "cases": len(results),
        "passed": sum(c["passed"] for c in results),
        "task_completion": sum(c["passed"] for c in results) / len(results),
        "by_tier": {
            key: {
                "turns": len(row["latencies"]),
                "correct": row["correct"],
                "turn_accuracy": row["correct"] / len(row["latencies"]),
                "clarifications": row["clarifications"],
                "unexpected_executions": row["unexpected_executions"],
                "latency_p50_ms": round(statistics.median(row["latencies"]), 2),
                "latency_p95_ms": round(sorted(row["latencies"])[(95 * len(row["latencies"]) + 99) // 100 - 1], 2),
            }
            for key, row in tiers.items()
        },
        "limitations": [
            "Synthetic POIs/routes and VehicleSimulator; no real GPS, car or charging station.",
            "Log-derived development cases; accuracy is after semantic guard, not held-out model NLU.",
        ],
        "results": results,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider", choices=["rules", "google"], default="rules")
    parser.add_argument("--allow-api", action="store_true")
    parser.add_argument("--interval", type=float, default=0)
    parser.add_argument("--dataset", type=Path, default=Path("eval/navigation_voice_cases.jsonl"))
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.provider != "rules" and not args.allow_api:
        parser.error("Cloud requires --allow-api; only fixture text and synthetic context are sent")
    report = asyncio.run(benchmark(args))
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(
        json.dumps({key: report[key] for key in ("cases", "passed", "task_completion", "by_tier")}, ensure_ascii=False)
    )
