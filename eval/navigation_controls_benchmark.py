"""Measure P/D through orchestration and durable receipts with isolated dependencies."""

import argparse
import asyncio
import hashlib
import json
import tempfile
import time
from pathlib import Path

from eval.benchmark import local_network_only
from eval.workflow_benchmark import ROOT, Runtime, latency, metadata
from src.vivi.domain.models import TurnRequest


async def measure(load, samples=100, warmup=6):
    with tempfile.TemporaryDirectory(prefix="vivi-controls-") as directory:
        runtime = Runtime(Path(directory))
        entered, release = asyncio.Event(), asyncio.Event()
        background = None
        try:
            quote = await runtime.pipeline.run(
                TurnRequest(
                    session_id="driver",
                    turn_id="plan",
                    transcript="Plan",
                    action={"capability": "navigation.plan", "arguments": {"query": "Tràng An"}},
                    position={"lat": 21, "lng": 105.85},
                ),
                "google",
            )
            await runtime.pipeline.run(
                TurnRequest(
                    session_id="driver",
                    turn_id="start",
                    transcript="Yes",
                    question_reply={"question_id": quote.pending_question["id"], "option_id": "yes"},
                    position={"lat": 21, "lng": 105.85},
                ),
                "google",
            )
            assert runtime.vehicle.state_for("driver").driving
            if load != "normal":

                async def slow(**kwargs):
                    entered.set()
                    await release.wait()
                    return json.dumps({"act": "reply", "text": "Done"})

                async def slow_search(*args, **kwargs):
                    entered.set()
                    await release.wait()
                    return []

                if load == "model_pending":
                    runtime.model.agenerate_json = slow
                    runtime.harness.models.providers["google"].budget_seconds = 10
                    request = TurnRequest(session_id="driver", turn_id="pending", transcript="Xin chào")
                else:
                    runtime.maps.search = slow_search
                    request = TurnRequest(
                        session_id="driver",
                        turn_id="pending",
                        transcript="Plan",
                        action={"capability": "navigation.plan", "arguments": {"query": "Tam Cốc"}},
                        position={"lat": 21, "lng": 105.85},
                    )
                background = asyncio.create_task(runtime.pipeline.run(request, "google"))
                await asyncio.wait_for(entered.wait(), 2)
            rows = []
            for index in range(2 * samples + warmup):
                gear = "P" if index % 2 == 0 else "D"
                started = time.perf_counter()
                response = await asyncio.wait_for(
                    runtime.pipeline.run(
                        TurnRequest(
                            session_id="driver",
                            turn_id=f"gear-{index}",
                            transcript="Gear",
                            navigation_event={"id": f"gear-{index}", "kind": "gear", "gear": gear, "revision": 0},
                        ),
                        "google",
                    ),
                    2,
                )
                wall = (time.perf_counter() - started) * 1000
                assert response.receipt["status"] == "completed", response.receipt
                assert runtime.vehicle.state_for("driver").driving == (gear == "D")
                if index >= warmup:
                    rows.append({"gear": gear, "wall_ms": round(wall, 2)})
            if background:
                assert not background.done(), f"{load}: dependency deadline expired during batch"
            summary = latency([row["wall_ms"] for row in rows])
            budget = 300 if load == "normal" else 500
            return {
                "load": load,
                "backend": summary,
                "budget_ms": budget,
                "per_gear": {gear: latency([r["wall_ms"] for r in rows if r["gear"] == gear]) for gear in ("P", "D")},
                "passed": summary["p95_ms"] <= budget,
                "measurements": rows,
            }
        finally:
            if background:
                background.cancel()
                await asyncio.gather(background, return_exceptions=True)
            release.set()
            await runtime.close()


async def benchmark(samples):
    runs = []
    for load in ("normal", "model_pending", "map_pending"):
        rows = []
        remaining = samples
        # Renew the blocked request in isolated batches so every sample runs while
        # the dependency is actually pending within its real 10 second deadline.
        while remaining:
            count = min(10, remaining)
            batch = await measure(load, count)
            rows.extend(batch["measurements"])
            remaining -= count
        budget = 300 if load == "normal" else 500
        per_gear = {gear: latency([r["wall_ms"] for r in rows if r["gear"] == gear]) for gear in ("P", "D")}
        runs.append(
            {
                "load": load,
                "backend": latency([r["wall_ms"] for r in rows]),
                "per_gear": per_gear,
                "budget_ms": budget,
                "passed": all(value["p95_ms"] <= budget for value in per_gear.values()),
                "batch_samples_per_gear": 10,
                "measurements": rows,
            }
        )
    return runs


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--samples", type=int, default=100, help="Minimum samples per gear, for each load")
    parser.add_argument("--output", type=Path, default=ROOT / "eval/results/controls-v2.json")
    args = parser.parse_args()
    if args.samples < 100:
        parser.error("At least 100 samples per gear required")
    with local_network_only():
        runs = asyncio.run(benchmark(args.samples))
    report = {
        **metadata(),
        "scope": "Backend orchestration, domain, SQLite and receipts; excludes HTTP/browser and real hardware",
        "fixture_sha256": hashlib.sha256((ROOT / "fixtures/core.py").read_bytes()).hexdigest(),
        "runs": runs,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps([{k: v for k, v in run.items() if k != "measurements"} for run in runs], indent=2))
    raise SystemExit(0 if all(run["passed"] for run in runs) else 1)


if __name__ == "__main__":
    main()
