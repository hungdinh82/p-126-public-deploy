"""Measure controls with SQLite and pending synthetic dependencies; no external I/O."""

import argparse
import asyncio
import hashlib
import json
import platform
import subprocess
import tempfile
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from eval.benchmark import local_network_only
from eval.workflow_benchmark import ROOT, Runtime, latency
from src.vivi.domain.models import TurnRequest


async def measure(load, samples=100, warmup=5):
    with tempfile.TemporaryDirectory(prefix="vivi-control-benchmark-") as directory:
        runtime = Runtime(Path(directory), "fixture" if load == "model_pending" else "rules")
        background = None
        blocker = None
        try:
            # Use an accepted journey, not only free-drive controls.
            cases = [json.loads(line) for line in (ROOT / "eval/workflow_cases.jsonl").read_text().splitlines()]
            steps = next(case for case in cases if case["id"] == "trip-accept-pause-resume-end")["operations"][:3]
            outputs = {}
            for step in steps:
                outputs[step["id"]] = await runtime.execute(step, "driver", outputs)
            assert outputs["A-start"]["navigation_workspace"]["journey"]["status"] == "running"
            if load != "normal":
                blocker = runtime.model if load == "model_pending" else runtime.maps
                blocker.entered.clear()
                blocker.release = asyncio.Event()
                if load == "model_pending":
                    runtime.graph.navigation.workflow.resolver.providers["fixture"].budget_seconds = 600
                background = asyncio.create_task(runtime.pipeline.run(TurnRequest(
                    session_id="driver", turn_id="pending", transcript="tôi nóng" if load == "model_pending" else "lập lộ trình tới Sầm Sơn",
                    position={"lat": 21, "lng": 105.8, "simulated": True}), runtime.provider))
                await asyncio.wait_for(blocker.entered.wait(), 3)
            measurements = []
            for index in range(samples + warmup):
                started = time.perf_counter()
                gear = "P" if index % 2 == 0 else "D"
                response = await asyncio.wait_for(runtime.pipeline.run(TurnRequest(
                    session_id="driver", turn_id=f"gear-{index}", transcript="Đổi số",
                    navigation_event={"kind": "gear", "gear": gear, "id": f"gear-{index}", "revision": 0}), runtime.provider), 2)
                wall = (time.perf_counter() - started) * 1000
                assert response.receipt["status"] == "completed", response.receipt
                assert runtime.vehicle.state_for("driver").driving == (gear == "D")
                if index >= warmup:
                    measurements.append({"gear": gear, **response.latency_ms, "wall": round(wall, 2),
                                         "orchestration": round(wall - response.latency_ms["total"], 2)})
            if background:
                assert not background.done(), "Dependency must remain pending throughout measurement"
            summary = latency([row["wall"] for row in measurements])
            budget = 300 if load == "normal" else 500
            return {"load": load, "samples": samples, "warmup": warmup, "backend": summary,
                    "queue": latency([row["queue"] for row in measurements]),
                    "control": latency([row["control"] for row in measurements]),
                    "per_gear": {gear: latency([row["wall"] for row in measurements if row["gear"] == gear]) for gear in ("P", "D")},
                    "budget_ms": budget, "passed": summary["p95_ms"] <= budget,
                    "includes": "orchestration, domain, durable receipts and turn ledger; excludes HTTP/browser",
                    "measurements": measurements}
        finally:
            if background:
                background.cancel()
                await asyncio.gather(background, return_exceptions=True)
            if blocker:
                blocker.release.set()
                blocker.release = None
            await runtime.close()


async def benchmark(samples=100):
    return [await measure(load, samples) for load in ("normal", "model_pending", "vietmap_pending")]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--samples", type=int, default=100)
    parser.add_argument("--output", type=Path, default=ROOT / "eval/results/navigation-controls.json")
    args = parser.parse_args()
    if args.samples < 100:
        parser.error("At least 100 samples per load are required for this benchmark")
    with local_network_only():
        report = asyncio.run(benchmark(args.samples))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    document = {
        "created_at": datetime.now(ZoneInfo("Asia/Ho_Chi_Minh")).isoformat(),
        "revision": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
        "working_tree_dirty": bool(subprocess.check_output(["git", "status", "--porcelain"], cwd=ROOT, text=True)),
        "environment": {"platform": platform.platform(), "python": platform.python_version()},
        "fixture_sha256": hashlib.sha256((ROOT / "eval/workflow_fixtures.py").read_bytes()).hexdigest(),
        "runs": report,
    }
    args.output.write_text(json.dumps(document, indent=2) + "\n")
    print(json.dumps([{key: value for key, value in row.items() if key != "measurements"} for row in report], indent=2))
    raise SystemExit(0 if all(row["passed"] for row in report) else 1)


if __name__ == "__main__":
    main()
