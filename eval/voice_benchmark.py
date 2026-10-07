"""Replay conversation development cases against local RAG and an in-memory car."""

from __future__ import annotations

import argparse
import json
import statistics
import tempfile
import time
from pathlib import Path

from eval.benchmark import local_network_only
from src.vivi.agents.graph import build_graph
from src.vivi.config import Settings
from src.vivi.rag.runtime import create_services


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=Path("eval/voice_cases.jsonl"))
    parser.add_argument("--provider", choices=("rules", "local"), default="rules")
    parser.add_argument("--output", type=Path, default=Path("eval/results/voice-dev.json"))
    parser.add_argument("--max-cases", type=int, default=None, help="Optional smoke subset; report records the selected size.")
    args = parser.parse_args()
    cases = [json.loads(line) for line in args.dataset.read_text().splitlines() if line]
    if args.max_cases:
        cases = cases[:args.max_cases]
    results = []
    with tempfile.TemporaryDirectory(prefix="vivi-voice-eval-") as directory, local_network_only():
        config = Settings(
            rag_local_only=True,
            rag_retrieval_mode="sqlite_local",
            data_dir=Path(directory),
            memory_enabled=True,
            memory_db=Path(directory) / "memory.sqlite3",
            rag_history_db=Path(directory) / "history.sqlite3",
        )
        services = create_services(config, provider=args.provider)
        graph = build_graph(services)
        for case in cases:
            services.memory.reset(services.memory_profile_id)
            started = time.perf_counter()
            turns = case.get("turns") or [case]
            turn_results = []
            for index, turn in enumerate(turns):
                turn_started = time.perf_counter()
                output = graph.invoke(
                    {"session_id": case["id"], "turn_id": f"eval-{index}", "input_text": turn["query"]}
                )["output"]
                answer_checks = all(
                    term.casefold() in output["response_text"].casefold()
                    for term in turn.get("required_answer_terms", [])
                ) and all(
                    term.casefold() not in output["response_text"].casefold()
                    for term in turn.get("forbidden_answer_terms", [])
                )
                argument_checks = (
                    "expected_value_celsius" not in turn
                    or (output.get("action_proposal") or {}).get("arguments", {}).get("value_celsius")
                    == turn["expected_value_celsius"]
                )
                if "expected_arguments" in turn:
                    argument_checks = argument_checks and (output.get("action_proposal") or {}).get("arguments") == turn["expected_arguments"]
                execution_checks = ("expected_executed" not in turn or
                                    bool((output.get("execution") or {}).get("executed")) == turn["expected_executed"])
                memory_checks = ("expected_memory_count" not in turn or
                                 len(services.memory.list(services.memory_profile_id)) == turn["expected_memory_count"])
                status_checks = "expected_status" not in turn or output["status"] == turn["expected_status"]
                route_checks = output["route"] == turn["expected_route"] and output["intent"] == turn["expected_intent"]
                turn_results.append(
                    {
                        **turn,
                        "route": output["route"],
                        "intent": output["intent"],
                        "status": output["status"],
                        "action_proposal": output.get("action_proposal"),
                        "execution": output.get("execution"),
                        "timings": output.get("timings", {}),
                        "answer": output["response_text"],
                        "characters": len(output["response_text"]),
                        "citations": output["citations"],
                        "errors": output["errors"],
                        "answer_checks": answer_checks and execution_checks and memory_checks,
                        "execution_checks": execution_checks,
                        "memory_checks": memory_checks,
                        "latency_ms": round((time.perf_counter() - turn_started) * 1000, 2),
                        "argument_checks": argument_checks,
                        "status_checks": status_checks,
                        "route_checks": route_checks,
                    }
                )
            results.append(
                {
                    **case,
                    **turn_results[-1],
                    "turn_results": turn_results,
                    "answer_checks": all(row["answer_checks"] and row["status_checks"] for row in turn_results),
                    "argument_checks": all(row["argument_checks"] for row in turn_results),
                    "route_checks": all(row["route_checks"] for row in turn_results),
                    "errors": [error for row in turn_results for error in row["errors"]],
                    "latency_ms": round((time.perf_counter() - started) * 1000, 2),
                }
            )
    correct = sum(case["route_checks"] for case in results)
    latencies = sorted(row["latency_ms"] for case in results for row in case["turn_results"])
    report = {
        "provider": args.provider,
        "cases": len(cases),
        "route_and_intent_accuracy": correct / len(cases),
        "turns": sum(len(case["turn_results"]) for case in results),
        "latency_p50_ms": statistics.median(latencies),
        "latency_p95_ms": latencies[min(len(latencies) - 1, int(len(latencies) * .95))],
        "error_count": sum(len(case["errors"]) for case in results),
        "over_360_characters": sum(row["characters"] > 360 for case in results for row in case["turn_results"]),
        "answer_or_argument_failures": sum(
            not case["answer_checks"] or not case["argument_checks"] for case in results
        ),
        "limitations": [
            "Development cases include recent user utterances; not a held-out accuracy estimate.",
            "Length and routing checks do not measure factual correctness or SLM voice quality.",
            "Actions run only against an in-memory simulator; external TCP is blocked.",
        ],
        "results": results,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({key: value for key, value in report.items() if key != "results"}, ensure_ascii=False, indent=2))
    if correct != len(cases) or any(
        case["errors"] or not case["answer_checks"] or not case["argument_checks"] for case in results
    ):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
