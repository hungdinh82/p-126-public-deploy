"""Measure current control path with real SQLite, ledger and vehicle adapter."""

import asyncio
import json
import math
import tempfile
import time
from pathlib import Path

import tests.conftest  # noqa: F401
from src.vivi.config import Settings
from src.vivi.domain.models import TurnRequest
from src.vivi.orchestration import LangGraphOrchestrator
from src.vivi.persistence.event_store import EventStore
from tests.test_long_term_memory import services
from tests.test_navigation_lifecycle import runtime, start


def percentile(values):
    return sorted(values)[math.ceil(len(values) * 0.95) - 1]


async def benchmark():
    with tempfile.TemporaryDirectory(prefix="vivi-control-benchmark-") as directory:
        root = Path(directory)
        graph, ledger, gateway = runtime(services.__wrapped__(root), root)
        pipeline = LangGraphOrchestrator({"rules": graph}, gateway, EventStore(Settings(data_dir=root)), ledger=ledger)
        try:
            await start(graph)
            measurements = []
            for index in range(35):
                started = time.perf_counter()
                response = await pipeline.run(
                    TurnRequest(
                        session_id="driver",
                        turn_id=f"gear-{index}",
                        transcript="Đổi số",
                        navigation_event={
                            "kind": "gear",
                            "gear": "P" if index % 2 == 0 else "D",
                            "id": f"gear-{index}",
                            "revision": 0,
                        },
                    )
                )
                if index >= 5:
                    measurements.append(
                        {
                            **response.latency_ms,
                            "wall": round((time.perf_counter() - started) * 1000, 2),
                            "ledger": round((time.perf_counter() - started) * 1000 - response.latency_ms["total"], 2),
                        }
                    )
            report = {
                "samples": len(measurements),
                "backend_p95_ms": percentile([m["wall"] for m in measurements]),
                "queue_p95_ms": percentile([m["queue"] for m in measurements]),
                "control_p95_ms": percentile([m["control"] for m in measurements]),
                "ledger_p95_ms": percentile([m["ledger"] for m in measurements]),
                "includes": "orchestration, checkpoint and turn ledger; excludes HTTP and browser",
            }
            print(json.dumps(report, indent=2))
            Path("/tmp/vivi-ui-check").mkdir(exist_ok=True)
            Path("/tmp/vivi-ui-check/navigation-backend-performance.json").write_text(json.dumps(report, indent=2))
        finally:
            graph.close()


if __name__ == "__main__":
    asyncio.run(benchmark())
