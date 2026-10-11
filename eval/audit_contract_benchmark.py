"""Measure the exact Oct09 regression oracle; mocks do not measure model NLU."""

from __future__ import annotations

import asyncio
import json
import statistics
import tempfile
import time
from pathlib import Path

from src.vivi.history.sqlite import SQLiteConversationHistory
from src.vivi.memory.sqlite import SQLiteLongTermMemory
from src.vivi.rag.generator import ExtractiveHandbookGenerator
from src.vivi.rag.runtime import HandbookServices
from src.vivi.vehicle.gateway import VehicleActionGateway
from tests import test_dialogue_audit_oct09 as audit
from tests.test_long_term_memory import NoRetrieval


async def benchmark():
    original_build = audit.build
    report = {
        "workflow": "unified-4",
        "oracle": "tests/test_dialogue_audit_oct09.py",
        "by_tier": {},
        "limitations": [
            "Cloud/local mocks emit parser semantic frames; this measures contract/reducer consistency, not model language accuracy.",
            "Map/vehicle fixtures are synthetic; latency is this host, not Xavier.",
        ],
    }
    for tier in ["rules", "google", "local"]:
        turns = []

        def measured_build(*args, **kwargs):
            graph, store, gateway = original_build(*args, **kwargs)
            invoke = graph.ainvoke

            async def measured(event):
                started = time.perf_counter()
                result = await invoke(event)
                turns.append(
                    {
                        "row": int(event["turn_id"]),
                        "status": result["status"],
                        "latency_ms": (time.perf_counter() - started) * 1000,
                        "served": result["diagnostics"]["effective_provider"],
                    }
                )
                return result

            graph.ainvoke = measured
            return graph, store, gateway

        audit.build = measured_build
        try:
            with tempfile.TemporaryDirectory(prefix="vivi-audit-contract-") as directory:
                root = Path(directory)
                services = HandbookServices(
                    NoRetrieval(),
                    ExtractiveHandbookGenerator(),
                    SQLiteConversationHistory(root / "history.sqlite3"),
                    action_gateway=VehicleActionGateway(),
                    memory=SQLiteLongTermMemory(root / "memory.sqlite3"),
                )
                await audit.test_all_latest_logged_turns(services, root, tier)
        finally:
            audit.build = original_build
        values = sorted(t["latency_ms"] for t in turns)
        report["by_tier"][tier + "_mock" if tier != "rules" else tier] = {
            "turns": len(turns),
            "oracle_passed": True,
            "clarifications": sum(t["status"] == "clarify" for t in turns),
            "latency_p50_ms": round(statistics.median(values), 2),
            "latency_p95_ms": round(values[(95 * len(values) + 99) // 100 - 1], 2),
            "unexpected_effects": 0,
            "results": turns,
        }
    return report


if __name__ == "__main__":
    report = asyncio.run(benchmark())
    Path("eval/results/navigation-audit-contract.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    )
    print(json.dumps({k: {f: v for f, v in row.items() if f != "results"} for k, row in report["by_tier"].items()}))
