"""Replay conversation development cases against local RAG and an in-memory car."""

from __future__ import annotations

import argparse
import json
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
    args = parser.parse_args()
    cases = [json.loads(line) for line in args.dataset.read_text().splitlines() if line]
    results = []
    with tempfile.TemporaryDirectory(prefix="vivi-voice-eval-") as directory, local_network_only():
        config = Settings(rag_local_only=True, rag_retrieval_mode="sqlite_local",
                          data_dir=Path(directory), memory_enabled=False,
                          rag_history_db=Path(directory) / "history.sqlite3")
        graph = build_graph(create_services(config, provider=args.provider))
        for case in cases:
            started = time.perf_counter()
            output = graph.invoke({"session_id": case["id"], "turn_id": "eval", "input_text": case["query"]})["output"]
            results.append({**case, "route": output["route"], "intent": output["intent"],
                            "status": output["status"], "answer": output["response_text"],
                            "characters": len(output["response_text"]), "citations": output["citations"],
                            "errors": output["errors"], "latency_ms": round((time.perf_counter() - started) * 1000, 2)})
    correct = sum(case["route"] == case["expected_route"] and case["intent"] == case["expected_intent"] for case in results)
    report = {"provider": args.provider, "cases": len(cases), "route_and_intent_accuracy": correct / len(cases),
              "over_360_characters": sum(case["characters"] > 360 for case in results),
              "limitations": ["Development cases include recent user utterances; not a held-out accuracy estimate.",
                              "Length and routing checks do not measure factual correctness or SLM voice quality.",
                              "Actions run only against an in-memory simulator; external TCP is blocked."],
              "results": results}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({key: value for key, value in report.items() if key != "results"}, ensure_ascii=False, indent=2))
    if correct != len(cases) or any(case["errors"] for case in results):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
