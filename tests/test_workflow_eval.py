"""Verify the evaluator's independent oracles, contracts and report behavior."""

import asyncio
import json
from types import SimpleNamespace

import pytest

from eval.workflow_benchmark import DATASET, benchmark, checks, latency, load_cases, markdown, references


def test_checks_execution_and_exact_exclusions():
    oracle = {"execution.executed": True, "action.arguments.zones": {"same_members": ["rear_right", "rear_left"]},
              "observed.vehicle.window_positions.driver": 100}
    response = {"execution": {"executed": True}, "action": {"arguments": {"zones": ["rear_left", "rear_right"]}},
                "observed": {"vehicle": {"window_positions": {"driver": 100}}}}
    assert all(row["passed"] for row in checks(oracle, response).values())
    response["execution"]["executed"] = False
    response["action"]["arguments"]["zones"] = ["driver", "rear_left", "rear_right"]
    outcomes = checks(oracle, response)
    assert not outcomes["execution.executed"]["passed"]
    assert not outcomes["action.arguments.zones:same_members"]["passed"]


def test_references_bind_previous_target_and_reject_missing():
    assert references({"id": {"$ref": "choice.plan.id"}}, {"choice": {"plan": {"id": "A"}}}) == {"id": "A"}
    with pytest.raises(ValueError, match="Missing reference"):
        references({"$ref": "choice.plan.id"}, {})


@pytest.mark.parametrize("mutate", ["duplicate", "empty_assertion", "unknown_operator", "unknown_operation"])
def test_dataset_rejects_invalid_contract(tmp_path, mutate):
    case = {"id": "one", "group": "vehicle", "operations": [{"id": "turn", "type": "turn", "expect": {"status": "verified"}}]}
    cases = [case]
    if mutate == "duplicate":
        cases.append(case)
    elif mutate == "empty_assertion":
        case["operations"][0]["expect"] = {}
    elif mutate == "unknown_operator":
        case["operations"][0]["expect"] = {"status": {"equals_typo": "verified"}}
    else:
        case["operations"][0]["type"] = "typo"
    path = tmp_path / "invalid.jsonl"
    path.write_text("\n".join(json.dumps(row) for row in cases))
    with pytest.raises(ValueError):
        load_cases(path)


def test_dataset_covers_runtime_failures_and_place_selection():
    cases = load_cases(DATASET)
    groups = {case["group"] for case in cases}
    assert {"place_selection", "recovery", "concurrency", "policy", "provider_failure", "trip_planning", "journey_lifecycle"} <= groups
    assert any(case["id"] == "sam-son-deictic-followup" for case in cases)
    assert latency(list(range(1, 101)))["p95_ms"] == 95


def test_real_graph_eval_records_failures_without_changing_oracle(tmp_path):
    cases = [case for case in load_cases(DATASET) if case["id"] in {"front-right-slot", "ledger-replay", "model-timeout-fallback"}]
    assert len(cases) == 3
    # An intentionally false oracle must survive into JSON and Markdown.
    cases[0]["operations"][0]["expect"]["status"] = "intentionally_wrong"
    dataset = tmp_path / "cases.jsonl"
    dataset.write_text("\n".join(json.dumps(case, ensure_ascii=False) for case in cases))
    report = asyncio.run(benchmark(SimpleNamespace(dataset=dataset, repeats=1, providers=["rules", "fixture"])))
    assert report["episodes"] == 5
    assert report["passed"] == 3
    assert "intentionally_wrong" in markdown(report)
    assert report["workflow_versions"] == ["unified-6"]
    assert all(row.get("error_type") is None for case in report["results"] for row in case["operations"])
