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
