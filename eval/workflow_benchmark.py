"""Core v2 behavior evaluation with authored semantic replies, never parser stubs."""

import argparse
import asyncio
import hashlib
import json
import math
import platform
import statistics
import subprocess
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path

from eval.benchmark import local_network_only
from fixtures.core import Harness, Maps, ScriptedModel
from src.vivi.config import Settings
from src.vivi.domain.models import TurnRequest
from src.vivi.navigation.vietmap import Place
from src.vivi.orchestration import LangGraphOrchestrator
from src.vivi.persistence.event_store import EventStore

ROOT = Path(__file__).resolve().parents[1]
DATASET = ROOT / "eval/core_cases.jsonl"
OPS = {"turn", "stream", "action", "answer", "event", "snapshot", "restart", "replay", "collision"}


def nested(value, path):
    for key in path.split("."):
        if isinstance(value, dict):
            value = value.get(key)
        elif isinstance(value, list) and key.isdigit() and int(key) < len(value):
            value = value[int(key)]
        else:
            return None
    return value


def references(value, outputs):
    if isinstance(value, dict):
        if set(value) == {"$ref"}:
            result = nested(outputs, value["$ref"])
            if result is None:
                raise ValueError(f"Missing reference: {value['$ref']}")
            return result
        return {key: references(item, outputs) for key, item in value.items()}
    if isinstance(value, list):
        return [references(item, outputs) for item in value]
    return value


def checks(expected, actual):
    results = {}
    for path, rule in expected.items():
        value = nested(actual, path)
        if isinstance(rule, dict):
            for operator, wanted in rule.items():
                if operator == "contains":
                    passed = value is not None and wanted in value
                elif operator == "not_contains":
                    passed = value is not None and wanted not in value
                elif operator == "length":
                    passed = value is not None and len(value) == wanted
                elif operator == "min":
                    passed = value is not None and value >= wanted
                elif operator == "exists":
                    passed = (value is not None) == wanted
                elif operator == "one_of":
                    passed = value in wanted
                elif operator == "same_members":
                    passed = isinstance(value, list) and sorted(value) == sorted(wanted)
                else:
                    raise ValueError(f"Unknown assertion operator {operator}")
                # Keep route-length assertions small; full geometry stays in the runtime/snapshot.
                actual_value = len(value) if operator == "length" and value is not None else value
                results[f"{path}:{operator}"] = {"passed": passed, "expected": wanted, "actual": actual_value}
        else:
            results[path] = {"passed": value == rule, "expected": rule, "actual": value}
    return results


def load_cases(path):
    cases = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    ids = set()
    for case in cases:
        if case["id"] in ids or not case.get("group") or not case.get("operations"):
            raise ValueError("Cases need unique IDs, group and operations")
        ids.add(case["id"])
        step_ids = set()
        for operation in case["operations"]:
            if operation["id"] in step_ids or operation["type"] not in OPS:
                raise ValueError(f"Invalid operation: {case['id']}")
            step_ids.add(operation["id"])
            if operation["type"] != "setup" and not operation.get("expect"):
                raise ValueError(f"Missing independent assertions: {case['id']}/{operation['id']}")
            for rule in operation.get("expect", {}).values():
                if isinstance(rule, dict) and set(rule) != {"$ref"}:
                    if not rule or not set(rule) <= {
                        "contains",
                        "not_contains",
                        "length",
                        "min",
                        "exists",
                        "one_of",
                        "same_members",
                    }:
                        raise ValueError(f"Invalid assertion: {case['id']}/{operation['id']}")
    if not cases:
        raise ValueError("Empty dataset")
    return cases


def latency(values):
    return (
        {
            "samples": len(values),
            "p50_ms": round(statistics.median(values), 2),
            "p95_ms": round(sorted(values)[math.ceil(0.95 * len(values)) - 1], 2),
        }
        if values
        else {"samples": 0}
    )


class FixtureMaps(Maps):
    def __init__(self, places=None):
        super().__init__()
        self.places = places

    async def search(self, text, **kwargs):
        if self.places is None:
            return await super().search(text, **kwargs)
        self.queries.append(text)
        return [Place.model_validate(p) for p in self.places]


class Runtime:
    def __init__(self, root, case=None):
        self.root, self.case = root, case or {}
        replies = [
            RuntimeError("synthetic unavailable") if op.get("model_error") else op["semantic"]
            for op in self.case.get("operations", [])
            if op["type"] in {"turn", "stream"}
        ]
        self.model = ScriptedModel(replies, destination=self.case.get("destination_choice"))
        self.maps = FixtureMaps(self.case.get("places"))
        self.vehicle = None
        self.requests = {}
        self.build()

    def build(self):
        self.harness = Harness(self.root, model=self.model, maps=self.maps, vehicle=self.vehicle)
        self.vehicle = self.harness.vehicle
        config = Settings(
            _env_file=None, data_dir=self.root, llm_provider="google", stt_provider="off", tts_provider="off"
        )
        self.pipeline = LangGraphOrchestrator(
            self.harness.graph,
            self.harness.gateway,
            EventStore(config),
            default_provider="google",
            ledger=self.harness.ledger,
        )
        self.pipeline.semantic_providers = {"google"}

    async def close(self):
        await self.harness.close()

    async def observe(self, result):
        value = result.model_dump(mode="json") if hasattr(result, "model_dump") else dict(result)
        value["observed"] = {
            "vehicle": self.vehicle.state_for("driver").model_dump(mode="json"),
            "model_calls": len(self.model.calls),
        }
        return value

    async def execute(self, operation, outputs):
        op = references(operation, outputs)
        kind = op["type"]
        if kind == "restart":
            await self.close()
            self.build()
        if kind in {"snapshot", "restart"}:
            return await self.observe(await self.harness.graph.snapshot("driver"))
        if kind in {"replay", "collision"}:
            request = self.requests[op["target"]]
            if kind == "collision":
                request = request.model_copy(update={"transcript": "changed payload"})
                try:
                    await self.pipeline.run(request, "google")
                except ValueError:
                    return await self.observe({"collision": True})
                raise AssertionError("Reused ID with another payload was accepted")
        else:
            request = TurnRequest(
                session_id="driver",
                turn_id=op["id"],
                transcript=op.get("text", "Typed event"),
                position={"lat": 21, "lng": 105.85},
                action=op.get("action"),
                question_reply=op.get("answer"),
                navigation_event=op.get("event"),
            )
            self.requests[op["id"]] = request
        if kind == "stream":
            events = [event async for event in self.pipeline.run_stream(request, "google")]
            result = next(event["response"] for event in events if event["type"] == "final")
        else:
            result = await self.pipeline.run(request, "google")
        return await self.observe(result)


def metadata():
    return {
        "created_at": datetime.now(UTC).isoformat(),
        "revision": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
        "working_tree_dirty": bool(subprocess.check_output(["git", "status", "--porcelain"], cwd=ROOT, text=True)),
        "environment": {"platform": platform.platform(), "python": platform.python_version()},
    }


async def benchmark(args):
    results = []
    for case in load_cases(args.dataset):
        for repeat in range(args.repeats):
            with tempfile.TemporaryDirectory(prefix="vivi-core-eval-") as directory:
                runtime = Runtime(Path(directory), case)
                outputs, steps = {}, []
                try:
                    for op in case["operations"]:
                        started = time.perf_counter()
                        output = await runtime.execute(op, outputs)
                        outputs[op["id"]] = output
                        assertions = checks(references(op["expect"], outputs), output)
                        steps.append(
                            {
                                "id": op["id"],
                                "checks": assertions,
                                "wall_ms": round((time.perf_counter() - started) * 1000, 2),
                                "passed": all(c["passed"] for c in assertions.values()),
                            }
                        )
                except Exception as exc:
                    steps.append({"id": op["id"], "passed": False, "error": f"{type(exc).__name__}: {exc}"})
                finally:
                    await runtime.close()
                results.append(
                    {
                        "id": case["id"],
                        "group": case["group"],
                        "repeat": repeat,
                        "passed": all(s["passed"] for s in steps),
                        "steps": steps,
                    }
                )
    return {
        **metadata(),
        "profile": "authored_semantic_contract",
        "dataset_sha256": hashlib.sha256(args.dataset.read_bytes()).hexdigest(),
        "limitations": [
            "Fake models, maps and vehicle; does not measure semantic accuracy or live adapters.",
            "Authored cases used during development; not a held-out language evaluation.",
        ],
        "episodes": len(results),
        "passed": sum(r["passed"] for r in results),
        "results": results,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=DATASET)
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--output", type=Path, default=ROOT / "eval/results/core-v2.json")
    args = parser.parse_args()
    if args.repeats < 1:
        parser.error("repeats must be positive")
    with local_network_only():
        report = asyncio.run(benchmark(args))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({k: v for k, v in report.items() if k != "results"}, ensure_ascii=False, indent=2))
    raise SystemExit(0 if report["passed"] == report["episodes"] else 1)


if __name__ == "__main__":
    main()
