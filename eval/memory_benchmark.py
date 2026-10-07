"""Offline development regression suite for memory persistence and explicit command replay."""
from __future__ import annotations

import argparse
import hashlib
import json
import platform
import statistics
import tempfile
import time
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

from src.vivi.agents.classifier import RulesIntentClassifier
from src.vivi.agents.graph import build_graph
from src.vivi.history.sqlite import SQLiteConversationHistory
from src.vivi.memory.sqlite import SQLiteLongTermMemory
from src.vivi.rag.generator import ExtractiveHandbookGenerator
from src.vivi.rag.runtime import HandbookServices
from src.vivi.vehicle.gateway import VehicleActionGateway


class NoRetrieval:
    def retrieve(self, *args):
        raise AssertionError("Memory evaluation must not use handbook retrieval")


def evaluate(dataset: Path, directory: Path) -> dict:
    cases = [json.loads(line) for line in dataset.read_text(encoding="utf-8").splitlines() if line.strip()]
    if not cases or len({case["id"] for case in cases}) != len(cases):
        raise ValueError("Memory cases must be nonempty and have unique IDs")
    episodes, results = {}, []
    # No TCP at all is needed: no embedding, SLM or live vehicle backend.
    with patch("socket.socket.connect", side_effect=AssertionError("Network is disabled in memory evaluation")):
        for case in cases:
            episode = case["episode"]
            if episode not in episodes:
                root = directory / episode
                services = HandbookServices(
                    NoRetrieval(), ExtractiveHandbookGenerator(), SQLiteConversationHistory(root / "history.sqlite3"),
                    classifier=RulesIntentClassifier(), action_gateway=VehicleActionGateway(),
                    memory=SQLiteLongTermMemory(root / "memory.sqlite3"),
                )
                episodes[episode] = services
            services = episodes[episode]
            if case.get("restart"):
                services = replace(services, memory=SQLiteLongTermMemory(services.memory.path),
                                   history=SQLiteConversationHistory(services.history.path),
                                   action_gateway=VehicleActionGateway(services.action_gateway.vehicle))
                episodes[episode] = services
            services = replace(services, memory_profile_id=case.get("profile", "default"))
            graph = build_graph(services)
            session = case.get("session", "driver")
            if "driving" in case:
                services.action_gateway.vehicle.set_driving(session, case["driving"])
            started = time.perf_counter()
            output = graph.invoke({"input_text": case["query"], "session_id": session, "turn_id": case["id"]})["output"]
            latency = round((time.perf_counter() - started) * 1000, 2)
            memories = {item["key"]: item["value"] for item in services.memory.list(services.memory_profile_id)}
            vehicle = services.action_gateway.vehicle.state_for(session)
            checks = {
                "route": output["route"] == case["expected_route"],
                "status": output["status"] == case["expected_status"],
                "memory": memories == case["expected_memories"],
                "vehicle_state": vehicle.state_version == case["expected_state_version"],
                "answer": case.get("answer_contains", "") in output["response_text"] and (
                    "answer_excludes" not in case or case["answer_excludes"] not in output["response_text"]
                ),
            }
            if "expected_temperature" in case:
                checks["vehicle_state"] &= vehicle.temperature_celsius == case["expected_temperature"]
            results.append({"id": case["id"], "query": case["query"], "checks": checks, "passed": all(checks.values()),
                            "answer": output["response_text"], "status": output["status"], "memory": memories,
                            "errors": output["errors"], "latency_ms": latency})
    latencies = sorted(result["latency_ms"] for result in results)
    return {"created_at": datetime.now(UTC).isoformat(), "provider": "rules",
            "dataset_sha256": hashlib.sha256(dataset.read_bytes()).hexdigest(),
            "host": {"platform": platform.platform(), "machine": platform.machine()},
            "cases": len(results), "passed": sum(result["passed"] for result in results),
            "latency_ms": {"p50": statistics.median(latencies), "p95": latencies[max(0, (95 * len(latencies) + 99) // 100 - 1)]},
            "limitations": ["Authored development cases; labels await user review, not held-out accuracy.",
                            "Rules provider and simulated car; does not validate real SLM or speech quality.",
                            "Latency is from this host with SQLite persistence; not measured on AGX Xavier."],
            "results": results}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=Path("eval/memory_cases.jsonl"))
    parser.add_argument("--output", type=Path, default=Path("eval/results/memory-dev.json"))
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="vivi-memory-eval-") as directory:
        report = evaluate(args.dataset, Path(directory))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: value for key, value in report.items() if key != "results"}, ensure_ascii=False, indent=2))
    if report["passed"] != report["cases"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
