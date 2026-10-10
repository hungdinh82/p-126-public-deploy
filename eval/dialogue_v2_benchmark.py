"""Opt-in API replay of sanitized dialogue episodes against a simulated car."""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import tempfile
import time
from contextlib import nullcontext
from pathlib import Path

from eval.benchmark import local_network_only
from src.vivi.config import Settings
from src.vivi.domain.models import TurnRequest
from src.vivi.orchestration import create_langgraph_orchestrator
from src.vivi.persistence.event_store import EventStore
from src.vivi.vehicle.memory import VehicleSimulator


def nested(value, path):
    for key in path.split("."):
        value = value.get(key) if isinstance(value, dict) else None
    return value


def checks(turn, response):
    semantic = response["diagnostics"].get("semantic", {})
    drafts = semantic.get("actions", [])
    decision = {"intent": drafts[-1]["capability"]} if drafts else {}
    action = response.get("action") or {}
    actual = {
        "status": response["status"],
        "route": response["route"],
        "intent": action.get("intent") or decision.get("intent"),
        "arguments": action.get("arguments"),
        "executed": bool((response.get("execution") or {}).get("executed")),
        "state": response["vehicle_state"],
    }
    outcomes = {
        key: actual[key] == turn["expected_" + key]
        for key in ("status", "route", "intent", "executed")
        if "expected_" + key in turn
    }
    if "expected_arguments" in turn:
        wanted, received = turn["expected_arguments"], actual["arguments"] or {}
        outcomes["arguments"] = set(wanted) == set(received) and all(
            sorted(received[key]) == sorted(value) if isinstance(value, list) else received[key] == value
            for key, value in wanted.items()
        )
    outcomes["state"] = all(
        nested(actual["state"], key) == value for key, value in turn.get("expected_state", {}).items()
    )
    outcomes["planner_available"] = response["diagnostics"].get("decision_source") != "degraded"
    outcomes["nonempty"] = bool(response["message"].strip())
    outcomes["answer"] = not any(
        term.casefold() in response["message"].casefold() for term in turn.get("forbidden_answer_terms", [])
    )
    return outcomes, actual


async def benchmark(args):
    cases = [json.loads(line) for line in args.dataset.read_text().splitlines() if line.strip()][: args.max_cases]
    results, latencies, tiers = [], [], {}
    with tempfile.TemporaryDirectory(prefix="vivi-v2-eval-") as directory:
        root = Path(directory)
        config = Settings(
            llm_provider=args.provider,
            data_dir=root,
            rag_history_db=root / "history.sqlite3",
            memory_db=root / "memory.sqlite3",
            memory_enabled=True,
            vehicle_provider="memory",
            rag_retrieval_mode="sqlite",
            rag_local_only=args.provider in {"rules", "local"},
            planner_max_tokens=args.max_tokens,
            store_transcripts=False,
            store_audio=False,
            vietmap_api_key="",
        )
        runtime = create_langgraph_orchestrator(VehicleSimulator(), EventStore(config), config)
        if args.provider not in runtime.graph_providers:
            raise RuntimeError(runtime.provider_errors.get(args.provider, "Provider unavailable"))
        try:
            for case in cases:
                turns = []
                for index, turn in enumerate(case["turns"]):
                    if delay := getattr(args, "interval_seconds", 0):
                        await asyncio.sleep(delay)
                    started = time.perf_counter()
                    try:
                        response = await runtime.run(
                            TurnRequest(session_id=case["id"], turn_id=f"eval-{index}", transcript=turn["query"]),
                            provider=args.provider,
                        )
                        value = response.model_dump(mode="json")
                        assertions, actual = checks(turn, value)
                        tier = value["diagnostics"].get("effective_provider", "unknown")
                        row = tiers.setdefault(
                            tier,
                            {
                                "turns": 0,
                                "correct": 0,
                                "clarifications": 0,
                                "unexpected_executions": 0,
                                "guarded_turns": 0,
                                "latencies_ms": [],
                            },
                        )
                        row["turns"] += 1
                        row["correct"] += all(assertions.values())
                        row["clarifications"] += value["status"] == "clarify"
                        row["unexpected_executions"] += bool(
                            actual["executed"] and turn.get("expected_executed") is False
                        )
                        row["guarded_turns"] += any(
                            attempt.get("semantic_guard")
                            for attempt in value["diagnostics"].get("provider_attempts", [])
                        )
                        row["latencies_ms"].append(value["latency_ms"]["total"])
                        turns.append(
                            {
                                "query": turn["query"],
                                "passed": all(assertions.values()),
                                "checks": assertions,
                                "actual": actual,
                                "message": value["message"],
                                "error": value.get("error"),
                                "diagnostics": value["diagnostics"],
                            }
                        )
                    except Exception as exc:
                        turns.append({"query": turn["query"], "passed": False, "error": str(exc)})
                    latencies.append(round((time.perf_counter() - started) * 1000, 2))
                results.append({"id": case["id"], "passed": all(turn["passed"] for turn in turns), "turns": turns})
        finally:
            for graph in set(runtime.graphs.values()):
                if hasattr(graph, "close"):
                    graph.close()
    return {
        "dataset": str(args.dataset),
        "split": "development-regression",
        "provider": args.provider,
        "workflow": "unified-4",
        "max_cases": args.max_cases,
        "max_tokens_per_planner_call": args.max_tokens,
        "cases": len(results),
        "passed": sum(result["passed"] for result in results),
        "turns": len(latencies),
        "by_tier": {
            tier: {
                **{k: v for k, v in row.items() if k != "latencies_ms"},
                "turn_accuracy": row["correct"] / row["turns"],
                "latency_p50_ms": statistics.median(row["latencies_ms"]),
                "latency_p95_ms": sorted(row["latencies_ms"])[max(0, (95 * row["turns"] + 99) // 100 - 1)],
            }
            for tier, row in tiers.items()
        },
        "task_completion": sum(result["passed"] for result in results) / len(results) if results else 0,
        "limitations": [
            "Log-derived development regression, not held-out accuracy.",
            "VehicleSimulator only; does not measure physical vehicle, microphone STT or speaker TTS.",
        ],
        "latency_p50_ms": statistics.median(latencies) if latencies else None,
        "latency_p95_ms": sorted(latencies)[min(len(latencies) - 1, int(len(latencies) * 0.95))] if latencies else None,
        "results": results,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider", choices=("rules", "local", "google", "openai", "openrouter"), default="rules")
    parser.add_argument(
        "--allow-api",
        action="store_true",
        help="Explicitly permit cloud calls and their cost; only fixture text is sent.",
    )
    parser.add_argument("--dataset", type=Path, default=Path("eval/dialogue_v2_cases.jsonl"))
    parser.add_argument("--max-cases", type=int, default=9)
    parser.add_argument(
        "--interval-seconds", type=float, default=0, help="Optional pacing between API turns to respect rate limits."
    )
    parser.add_argument("--max-tokens", type=int, default=2048)
    parser.add_argument("--output", type=Path, default=Path("eval/results/dialogue-v2.json"))
    args = parser.parse_args()
    if args.interval_seconds < 0 or args.interval_seconds > 60:
        parser.error("interval-seconds must be between 0 and 60")
    if args.max_cases < 1 or not 512 <= args.max_tokens <= 8192:
        parser.error("max-cases must be positive; max-tokens must be 512–8192")
    cloud = args.provider not in {"rules", "local"}
    if cloud and not args.allow_api:
        parser.error("Cloud evaluation requires --allow-api")
    with nullcontext() if cloud else local_network_only():
        report = asyncio.run(benchmark(args))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(
        json.dumps(
            {key: report[key] for key in ("provider", "cases", "passed", "turns", "latency_p50_ms", "latency_p95_ms")},
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
