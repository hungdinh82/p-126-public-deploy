"""A fixture evaluation must report bad behavior without learning its own oracle."""

import hashlib
import json
from types import SimpleNamespace

import pytest

from eval.workflow_benchmark import benchmark


@pytest.mark.asyncio
async def test_evaluator_records_failures_without_changing_independent_oracle(tmp_path):
    case = {
        "id": "intentional-failure",
        "group": "evaluator",
        "operations": [
            {
                "id": "play",
                "type": "turn",
                "text": "bật nhạc",
                "semantic": {"act": "request", "actions": [{"capability": "media.play"}]},
                "expect": {"observed.vehicle.media_playing": False},
            }
        ],
    }
    broken = {
        "id": "missing-reference",
        "group": "evaluator",
        "operations": [
            {
                "id": "reply",
                "type": "answer",
                "answer": {"question_id": {"$ref": "unknown.id"}, "option_id": "yes"},
                "expect": {"status": "clarify"},
            }
        ],
    }
    dataset = tmp_path / "cases.jsonl"
    dataset.write_text(json.dumps(case) + "\n" + json.dumps(broken) + "\n")
    before = hashlib.sha256(dataset.read_bytes()).hexdigest()
    report = await benchmark(SimpleNamespace(dataset=dataset, repeats=1))
    assert report["episodes"] == 2 and report["passed"] == 0
    check = report["results"][0]["steps"][0]["checks"]["observed.vehicle.media_playing"]
    assert check == {"passed": False, "expected": False, "actual": True}
    assert "Missing reference" in report["results"][1]["steps"][0]["error"]
    assert hashlib.sha256(dataset.read_bytes()).hexdigest() == before


@pytest.mark.asyncio
async def test_semantic_evaluator_uses_production_budgets_context_and_local_prompt_hash(monkeypatch, tmp_path):
    from eval import semantic_benchmark
    from src.vivi.dialogue.planner import semantic_system_prompt

    class Model:
        model = "synthetic-model"
        tools_as_json = True

        async def agenerate_tool(self, **kwargs):
            assert "plan:fixture" in kwargs["user"]
            assert "prior fixture turn" in kwargs["user"]
            return "interpret_turn", {
                "act": "request",
                "actions": [
                    {"capability": "climate.set_temperature", "arguments": {"value_celsius": 24}},
                ],
            }

    monkeypatch.setitem(semantic_benchmark.GENERATORS, "local", lambda config: SimpleNamespace(client=Model()))
    dataset = tmp_path / "semantic.jsonl"
    dataset.write_text(
        json.dumps(
            {
                "id": "wrong-temperature",
                "text": "đặt 25 độ",
                "session": {"navigation_workspace": {"draft": {"id": "plan:fixture"}}},
                "history": [{"query": "prior fixture turn", "answer": "old answer"}],
                "expect": {"actions.0.arguments.value_celsius": 25},
            }
        )
    )
    before = dataset.read_bytes()
    report = await semantic_benchmark.evaluate(SimpleNamespace(provider="local", dataset=dataset, repeats=2))
    assert report["cases"] == 2 and report["passed"] == 0 and report["fallbacks"] == 0
    assert report["results"][0]["attempts"][0]["budget_seconds"] > 10
    assert (
        report["semantic_instruction_sha256"] == hashlib.sha256(semantic_system_prompt(local=True).encode()).hexdigest()
    )
    assert report["transport"] == "json_schema" and report["semantic_schema_sha256"]
    assert dataset.read_bytes() == before
