"""Contract and latency evaluation of the current LangGraph in isolated runtimes."""

from __future__ import annotations

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
from collections import Counter
from datetime import datetime
from importlib.metadata import version
from pathlib import Path
from zoneinfo import ZoneInfo

from eval.benchmark import local_network_only
from eval.workflow_fixtures import FixtureMaps, FixtureModel, FixtureResolver, energy_profile
from src.vivi.config import Settings
from src.vivi.dialogue.planner import ModelProvider
from src.vivi.domain.models import TurnRequest
from src.vivi.graphs.assistant import build_assistant_graph
from src.vivi.history.sqlite import SQLiteConversationHistory
from src.vivi.memory.sqlite import SQLiteLongTermMemory
from src.vivi.navigation.energy import EnergyPlanner
from src.vivi.orchestration import LangGraphOrchestrator
from src.vivi.persistence.checkpoints import ThreadedSqliteSaver
from src.vivi.persistence.confirmations import DurableConfirmations
from src.vivi.persistence.event_store import EventStore
from src.vivi.persistence.runtime_store import RuntimeStore
from src.vivi.rag.generator import ExtractiveHandbookGenerator
from src.vivi.rag.runtime import HandbookServices
from src.vivi.vehicle.gateway import VehicleActionGateway
from src.vivi.vehicle.memory import VehicleSimulator

ROOT = Path(__file__).resolve().parents[1]
DATASET = ROOT / "eval/workflow_cases.jsonl"
OPS = {"turn", "stream", "action", "event", "snapshot", "restart", "parallel_park", "cancel", "replay", "collision", "lost_response", "setup"}


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
                    if not rule or not set(rule) <= {"contains", "not_contains", "length", "min", "exists", "one_of", "same_members"}:
                        raise ValueError(f"Invalid assertion: {case['id']}/{operation['id']}")
    if not cases:
        raise ValueError("Empty dataset")
    return cases


class NoHandbook:
    def retrieve(self, *args):
        return []  # Tests abstention/orchestration, never handbook answer accuracy.


class Runtime:
    def __init__(self, root, provider):
        self.root, self.provider = root, provider
        self.vehicle, self.maps, self.model = VehicleSimulator(), FixtureMaps(), FixtureModel()
        self.requests = {}
        self.build()

    def build(self):
        ledger = RuntimeStore(self.root / "state.sqlite3")
        gateway = VehicleActionGateway(self.vehicle, DurableConfirmations(ledger), ledger=ledger)
        config = Settings(data_dir=self.root, store_transcripts=False, store_audio=False,
                          google_api_key="", openai_api_key="", openrouter_api_key="", vietmap_api_key="",
                          stt_provider="off", tts_provider="off", vehicle_provider="memory")
        services = HandbookServices(NoHandbook(), ExtractiveHandbookGenerator(),
                                   SQLiteConversationHistory(self.root / "history.sqlite3"),
                                   action_gateway=gateway, memory=SQLiteLongTermMemory(self.root / "memory.sqlite3"),
                                   navigation=self.maps, energy_planner=EnergyPlanner(energy_profile()))
        providers = {"fixture": ModelProvider("fixture", self.model, .1)} if self.provider == "fixture" else {}
        resolver = FixtureResolver(providers, fallback_chains={"fixture": ["fixture"]}, breaker_seconds=.01)
        self.graph = build_assistant_graph(services, ThreadedSqliteSaver(self.root / "checkpoints.sqlite3"), ledger, resolver=resolver)
        self.pipeline = LangGraphOrchestrator({self.provider: self.graph}, gateway, EventStore(config),
                                              default_provider=self.provider, ledger=ledger)

    async def close(self):
        await self.graph.aclose()

    async def observe(self, value, session):
        memory = self.graph.navigation.workflow.services.memory
        value["observed"] = {"vehicle": self.vehicle.state_for(session).model_dump(mode="json"),
                             "memory": {item["key"]: item["value"] for item in memory.list("default")},
                             "model_calls": self.model.calls, "map_calls": Counter(self.maps.calls)}
        return value

    async def execute(self, operation, session, outputs):
        operation = references(operation, outputs)
        kind = operation["type"]
        if kind == "setup":
            if "battery" in operation:
                self.vehicle.set_energy(session, operation["battery"], operation["battery"] * 4)
            if "driving" in operation:
                self.vehicle.set_driving(session, operation["driving"])
            self.maps.fail = operation.get("map_error", False)
            self.maps.points = operation.get("route_points", 2)
            return {}
        if kind == "restart":
            await self.close()
            self.build()
            return await self.observe(await self.graph.snapshot(session), session)
        if kind == "snapshot":
            value = await self.graph.snapshot(session, operation.get("event_id"))
            config = self.graph.config(self.graph.event({"session_id": session, "turn_id": "inspect", "input_text": "inspect"}))
            checkpoint = (await self.graph.compiled.aget_state(config)).values
            encoded = json.dumps(checkpoint, ensure_ascii=False)
            value["checkpoint"] = {"bytes": len(encoded.encode()), "has_geometry": '"coordinates"' in encoded}
            return await self.observe(value, session)
        self.model.semantic = operation.get("semantic")
        self.model.fault = operation.get("model_fault")
        if kind in {"replay", "collision", "lost_response"}:
            request = self.requests[operation["request"]]
            if kind == "collision":
                request = request.model_copy(update={"transcript": operation["query"]})
        else:
            payload = {"session_id": session, "turn_id": operation["id"], "transcript": operation.get("query", "typed event"),
                       "position": operation.get("position")}
            if kind == "action":
                payload["action"] = operation["action"]
            if kind == "event":
                event = {"id": operation["id"], "revision": 0, **operation["event"]}
                event.setdefault("runtime_epoch", self.graph.runtime_epoch)
                payload["navigation_event"] = event
            request = TurnRequest(**payload)
            self.requests[operation["id"]] = request
        if kind == "parallel_park":
            blocker = self.maps if operation.get("block") == "map" else self.model
            blocker.entered.clear()
            blocker.release = asyncio.Event()
            if operation.get("action"):
                request = TurnRequest.model_validate({**request.model_dump(), "action": operation["action"]})
            pending = asyncio.create_task(self.pipeline.run(request, self.provider))
            try:
                await asyncio.wait_for(blocker.entered.wait(), 3)
                parked = await asyncio.wait_for(self.pipeline.run(TurnRequest(
                    session_id=session, turn_id=operation["id"] + "-P", transcript="P",
                    navigation_event={"id": operation["id"] + "-P", "kind": "gear", "gear": "P", "revision": 0}), self.provider), 2)
                blocker.release.set()
                result = await pending
                value = {"park": parked.model_dump(mode="json"), "pending": result.model_dump(mode="json")}
            finally:
                blocker.release.set()
                blocker.release = None
                if not pending.done():
                    pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
            return await self.observe(value, session)
        if kind == "cancel":
            self.model.entered.clear()
            self.model.release = asyncio.Event()
            task = asyncio.create_task(self.pipeline.run(request, self.provider))
            try:
                await asyncio.wait_for(self.model.entered.wait(), 3)
                value = self.pipeline.cancel(session, request.turn_id)
                try:
                    await task
                except asyncio.CancelledError:
                    value["runner_cancelled"] = True
            finally:
                self.model.release.set()
                self.model.release = None
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            return await self.observe(value, session)
        try:
            if kind == "stream":
                events = [event async for event in self.pipeline.run_stream(request, self.provider)]
                value = next(event["response"] for event in reversed(events) if event["type"] == "final")
                value["stream_types"] = [event["type"] for event in events]
            else:
                value = (await self.pipeline.run(request, self.provider)).model_dump(mode="json")
            if kind == "lost_response":
                value = await self.graph.snapshot(session, request.navigation_event["id"])
            return await self.observe(value, session)
        except (ValueError, TimeoutError) as exc:
            return await self.observe({"error_type": type(exc).__name__}, session)


def latency(values):
    return {"samples": len(values), "p50_ms": round(statistics.median(values), 2),
            "p95_ms": round(sorted(values)[math.ceil(.95 * len(values)) - 1], 2)} if values else {"samples": 0}


async def benchmark(args):
    cases = load_cases(args.dataset)
    results = []
    started = time.perf_counter()
    for provider in args.providers:
        for repeat in range(args.repeats):
            for case in cases:
                if provider not in case.get("providers", ["rules", "fixture"]):
                    continue
                with tempfile.TemporaryDirectory(prefix="vivi-workflow-eval-") as directory:
                    cold = time.perf_counter()
                    runtime = Runtime(Path(directory), provider)
                    cold_ms = round((time.perf_counter() - cold) * 1000, 2)
                    rows, outputs = [], {}
                    try:
                        for operation in case["operations"]:
                            tick = time.perf_counter()
                            try:
                                output = await asyncio.wait_for(runtime.execute(operation, case["id"], outputs), 60)
                                outcomes = checks(references(operation.get("expect", {}), outputs), output)
                                row = {"id": operation["id"], "type": operation["type"], "query": operation.get("query"),
                                       "passed": all(check["passed"] for check in outcomes.values()), "checks": outcomes,
                                       "message": output.get("message"), "status": output.get("status"),
                                       "served": output.get("diagnostics", {}).get("effective_provider"),
                                       "workflow_version": output.get("diagnostics", {}).get("workflow_version"),
                                       "prompt_version": output.get("diagnostics", {}).get("prompt_version"),
                                       "provider_attempts": output.get("diagnostics", {}).get("provider_attempts", []),
                                       "node_spans": output.get("diagnostics", {}).get("node_spans", []),
                                       "dependency_ms": output.get("latency_ms", {})}
                                outputs[operation["id"]] = output
                            except Exception as exc:
                                row = {"id": operation["id"], "type": operation["type"], "passed": False,
                                       "error_type": type(exc).__name__, "error": str(exc)[:300]}
                            row["wall_ms"] = round((time.perf_counter() - tick) * 1000, 2)
                            rows.append(row)
                    finally:
                        await runtime.close()
                    results.append({"id": case["id"], "group": case["group"], "provider": provider, "repeat": repeat,
                                    "cold_build_ms": cold_ms, "passed": all(row["passed"] for row in rows), "operations": rows})
    return summarize(args, results, (time.perf_counter() - started) * 1000)


def summarize(args, results, wall_ms):
    groups = {}
    tiers = {}
    for result in results:
        group = groups.setdefault(result["group"], {"episodes": 0, "passed": 0})
        group["episodes"] += 1
        group["passed"] += result["passed"]
        tier = tiers.setdefault(result["provider"], {"episodes": 0, "passed": 0, "latencies": [], "cold": [], "served": Counter(), "operations": 0, "correct": 0})
        tier["episodes"] += 1
        tier["passed"] += result["passed"]
        tier["cold"].append(result["cold_build_ms"])
        for row in result["operations"]:
            if row["type"] == "setup":
                continue
            tier["operations"] += 1
            tier["correct"] += row["passed"]
            if row["type"] in {"turn", "stream", "action", "event"}:
                tier["latencies"].append(row["wall_ms"])
            if row.get("served"):
                tier["served"][row["served"]] += 1
    for tier in tiers.values():
        tier["turn_latency"] = latency(tier.pop("latencies"))
        tier["cold_build"] = latency(tier.pop("cold"))
    return {"created_at": datetime.now(ZoneInfo("Asia/Ho_Chi_Minh")).isoformat(),
            "revision": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
            "working_tree_dirty": bool(subprocess.check_output(["git", "status", "--porcelain"], cwd=ROOT, text=True)),
            "dataset": str(args.dataset.relative_to(ROOT)) if args.dataset.is_relative_to(ROOT) else str(args.dataset),
            "dataset_sha256": hashlib.sha256(args.dataset.read_bytes()).hexdigest(),
            "workflow_versions": sorted({row["workflow_version"] for result in results for row in result["operations"] if row.get("workflow_version")}),
            "prompt_versions": sorted({row["prompt_version"] for result in results for row in result["operations"] if row.get("prompt_version")}),
            "dataset_kind": "authored development regression; not held-out", "repeats": args.repeats,
            "environment": {"platform": platform.platform(), "processor": platform.processor(), "python": platform.python_version(),
                            "langgraph": version("langgraph"), "langgraph_checkpoint_sqlite": version("langgraph-checkpoint-sqlite")},
            "wall_ms": round(wall_ms, 2), "episodes": len(results), "passed": sum(result["passed"] for result in results),
            "by_group": groups, "by_provider": tiers, "results": results,
            "limitations": ["Model fixture is a semantic contract stub, not model NLU accuracy.",
                            "Maps, car and energy are labelled synthetic fixtures; SQLite/checkpoints/policy are real.",
                            "No real STT/TTS, GPU/browser, VietMap, physical vehicle or cloud inference is measured.",
                            "Episode latency samples are functional regression, not a 100-sample P/D performance gate."]}


def markdown(report):
    lines = ["# LangGraph workflow benchmark", "", f"Thời điểm: {report['created_at']}.",
             f"Revision: `{report['revision'][:12]}`; working tree có thay đổi: {report['working_tree_dirty']}.",
             f"Dataset SHA-256: `{report['dataset_sha256']}`.", "",
             f"Kết quả: **{report['passed']}/{report['episodes']} episode qua**.", "",
             "| Provider | Episode qua | Operation qua | Turn p50 / p95 ms | Cold build p95 ms |",
             "|---|---:|---:|---:|---:|"]
    for provider, tier in report["by_provider"].items():
        timing = tier["turn_latency"]
        lines.append(f"| {provider} | {tier['passed']}/{tier['episodes']} | {tier['correct']}/{tier['operations']} | {timing.get('p50_ms')} / {timing.get('p95_ms')} | {tier['cold_build']['p95_ms']} |")
    lines += ["", "| Nhóm | Episode qua |", "|---|---:|"]
    lines += [f"| {group} | {value['passed']}/{value['episodes']} |" for group, value in report["by_group"].items()]
    lines += ["", "## Lỗi còn lại", ""]
    failures = 0
    for episode in report["results"]:
        for row in episode["operations"]:
            if not row["passed"]:
                failures += 1
                detail = {key: check for key, check in row.get("checks", {}).items() if not check["passed"]}
                lines.append(f"- `{episode['provider']}/{episode['id']}/{row['id']}`: `{json.dumps(detail or row.get('error'), ensure_ascii=False)}`")
    if not failures:
        lines.append("Không có lỗi trong phạm vi dataset này.")
    lines += ["", "## Giới hạn", "", *[f"- {value}" for value in report["limitations"]], "",
              "Kết quả ghi cả failure; exit code 1 nếu bất kỳ episode nào không qua. Không đổi oracle theo output để làm xanh báo cáo."]
    return "\n".join(lines) + "\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=DATASET)
    parser.add_argument("--providers", nargs="+", choices=["rules", "fixture"], default=["rules", "fixture"])
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--output", type=Path, default=ROOT / "eval/results/workflow.json")
    args = parser.parse_args()
    args.dataset = args.dataset.resolve()
    if args.repeats < 1:
        parser.error("repeats must be positive")
    with local_network_only():
        report = asyncio.run(benchmark(args))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    args.output.with_suffix(".md").write_text(markdown(report))
    print(json.dumps({key: report[key] for key in ["episodes", "passed", "by_provider", "by_group"]}, ensure_ascii=False))
    raise SystemExit(0 if report["passed"] == report["episodes"] else 1)


if __name__ == "__main__":
    main()
