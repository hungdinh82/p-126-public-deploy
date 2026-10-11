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
from tests.test_navigation_refinement import DestinationMaps


class SlowProvider:
    def __init__(self):
        self.started = asyncio.Event()

    async def agenerate_json(self, **kwargs):
        self.started.set()
        await asyncio.Event().wait()


class SlowMaps(DestinationMaps):
    def __init__(self):
        super().__init__()
        self.started = asyncio.Event()

    async def search(self, *args, **kwargs):
        self.started.set()
        await asyncio.Event().wait()


def percentile(values):
    return sorted(values)[math.ceil(len(values) * 0.95) - 1]


async def measure(load):
    with tempfile.TemporaryDirectory(prefix="vivi-control-benchmark-") as directory:
        root = Path(directory)
        model = SlowProvider() if load == "model_pending" else None
        graph, ledger, gateway = runtime(services.__wrapped__(root), root, model=model)
        graph.default_provider = "rules"
        pipeline = LangGraphOrchestrator({"rules": graph}, gateway, EventStore(Settings(data_dir=root)), ledger=ledger)
        try:
            await start(graph)
            background = None
            if model:
                graph.navigation.workflow.resolver.providers["google"].budget_seconds = 600
                background = asyncio.create_task(
                    graph.ainvoke(
                        {
                            "session_id": "driver",
                            "turn_id": "pending-model",
                            "provider": "google",
                            "input_text": "tôi nóng",
                        }
                    )
                )
                await asyncio.wait_for(model.started.wait(), 2)
            elif load == "vietmap_pending":
                maps = SlowMaps()
                graph.navigation.workflow.services.navigation.client = maps
                background = asyncio.create_task(
                    graph.ainvoke({"session_id": "driver", "turn_id": "pending-map", "input_text": "đi Hà Nội"})
                )
                await asyncio.wait_for(maps.started.wait(), 2)
            measurements = []
            for index in range(105):
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
                "load": load,
                "samples": len(measurements),
                "backend_p95_ms": percentile([m["wall"] for m in measurements]),
                "queue_p95_ms": percentile([m["queue"] for m in measurements]),
                "control_p95_ms": percentile([m["control"] for m in measurements]),
                "ledger_p95_ms": percentile([m["ledger"] for m in measurements]),
                "includes": "orchestration, navigation state/receipt and turn ledger; excludes HTTP and browser",
            }
            if background:
                assert not background.done(), "Slow dependency must remain pending throughout measurement"
            return report
        finally:
            await graph.aclose()


async def benchmark():
    report = [await measure(load) for load in ("normal", "model_pending", "vietmap_pending")]
    print(json.dumps(report, indent=2))
    Path("/tmp/vivi-ui-check").mkdir(exist_ok=True)
    Path("/tmp/vivi-ui-check/navigation-backend-performance.json").write_text(json.dumps(report, indent=2))


if __name__ == "__main__":
    asyncio.run(benchmark())
