"""The benchmark must reject bad datasets and report real behavioral failures."""

import json
from argparse import Namespace
from types import SimpleNamespace

import pytest

from benchmarks.agent.oracles import actual_decisions, decisions_match, expected_decisions
from benchmarks.agent.run import (
    EXCLUDED_CAPABILITIES,
    datasets,
    language_case,
    rate_limited,
    run,
    score,
    tools_case,
    write_feature_reports,
    write_report,
)
from benchmarks.agent.schema import AgentCase, LanguageCase, load
from src.vivi.agents.tools import TOOL_REGISTRY
from src.vivi.dialogue.contracts import SemanticTurn


def test_corpus_declares_every_public_capability_and_unique_ids():
    data = datasets()
    covered = set()
    for cases in data.values():
        ids = [case["id"] if isinstance(case, dict) else case.id for case in cases]
        assert len(ids) == len(set(ids))
        for case in cases:
            covered.update(case.get("covers", []) if isinstance(case, dict) else case.covers)
    assert covered == TOOL_REGISTRY.keys() - EXCLUDED_CAPABILITIES.keys()


@pytest.mark.parametrize(
    "change",
    [
        {"expect": {}},
        {"expect": {"output.status": {"typo_operator": "answered"}}},
    ],
)
def test_empty_or_invalid_oracles_cannot_produce_a_passing_score(tmp_path, change):
    case = {
        "id": "bad",
        "group": "vehicle",
        "feature": "music",
        "covers": ["media.play"],
        "turns": [{"text": "bật nhạc", **change}],
    }
    path = tmp_path / "bad.jsonl"
    path.write_text(json.dumps(case))
    with pytest.raises(ValueError):
        load(path, "tools")


@pytest.mark.asyncio
async def test_runner_catches_wrong_vehicle_oracle_instead_of_copying_fixture_output():
    case = AgentCase.model_validate(
        {
            "id": "independent-oracle",
            "group": "temperature",
            "feature": "climate",
            "covers": ["climate.set_temperature"],
            "turns": [
                {
                    "text": "đặt nhiệt độ 25 độ",
                    "fixture_attempts": [[{"name": "set_temperature", "arguments": {"celsius": 25}}]],
                    "expect": {"vehicle.temperature_celsius": 26},
                }
            ],
        }
    )
    turns = await tools_case(case, None, None, "local")
    assert not turns[0]["passed"]
    assert turns[0]["checks"]["vehicle.temperature_celsius"] == {"passed": False, "expected": 26, "actual": 25}


@pytest.mark.asyncio
async def test_live_language_fallback_is_not_model_accuracy():
    class Unavailable:
        async def resolve(self, *_args, **_kwargs):
            return SemanticTurn(act="clarify", text="unavailable"), None, []

    case = LanguageCase(
        id="blocked",
        group="negative",
        feature="doors",
        covers=["door.set_open"],
        text="đừng mở cửa",
        expect={"blocked": True},
    )
    turns = await language_case(case, Unavailable(), "local")
    assert turns[0]["checks"]["blocked"]["passed"]
    assert not turns[0]["checks"]["provider_served"]["passed"]
    assert not turns[0]["passed"]


def test_exact_tool_arguments_are_scored_and_failure_report_is_reviewable(tmp_path):
    assertions, passed = score(
        {"tool_calls.0.name": "close_door", "tool_calls.0.arguments.targets": ["driver"]},
        {"tool_calls": [{"name": "open_door", "arguments": {"targets": ["rear_right"]}}]},
    )
    assert not passed and all(not c["passed"] for c in assertions.values())
    report = {
        "mode": "fixture",
        "provider": "fixture",
        "contract": "test",
        "passed": 0,
        "cases": 1,
        "summary": {"suite": {"tools": {"passed": 0, "cases": 1}}},
        "limitations": ["synthetic"],
        "results": [
            {
                "suite": "tools",
                "id": "polarity",
                "passed": False,
                "repeat": 1,
                "turns": [{"turn": 1, "checks": assertions}],
            }
        ],
    }
    write_report(report, tmp_path)
    assert json.loads((tmp_path / "report.json").read_text())["passed"] == 0
    assert "close_door" in (tmp_path / "report.md").read_text()


def test_parallel_calls_allow_reordering_but_not_missing_extra_or_wrong_arguments():
    calls = [{"name": "get_battery_level", "arguments": {}}, {"name": "get_tire_pressure", "arguments": {}}]
    expected = {"tool_calls": {"same_calls": calls}}
    assert score(expected, {"tool_calls": list(reversed(calls))})[1]
    assert not score(expected, {"tool_calls": calls[:1]})[1]
    assert not score(expected, {"tool_calls": calls + calls[:1]})[1]
    assert not score(
        expected, {"tool_calls": [calls[0], {"name": "get_tire_pressure", "arguments": {"query": "wrong"}}]}
    )[1]


def test_each_feature_report_has_only_its_own_results_and_metrics(tmp_path):
    rows = [
        {
            "id": f"case-{feature}",
            "feature": feature,
            "group": feature,
            "input_style": "direct",
            "suite": "language",
            "repeat": 1,
            "passed": passed,
            "turns": [
                {
                    "passed": passed,
                    "checks": {"act": {"passed": passed, "expected": "request", "actual": "reply"}},
                    "actual": {"fallback": not passed},
                }
            ],
        }
        for feature, passed in [("doors", True), ("music", False)]
    ]
    report = {
        "mode": "fixture",
        "provider": "fixture",
        "contract": "test",
        "passed": 1,
        "cases": 2,
        "summary": {"suite": {"language": {"passed": 1, "cases": 2}}},
        "limitations": [],
        "results": rows,
    }
    write_feature_reports(report, tmp_path)
    doors = json.loads((tmp_path / "doors/language/report.json").read_text())
    music = json.loads((tmp_path / "music/language/report.json").read_text())
    assert doors["cases"] == doors["passed"] == 1 and doors["fallbacks"] == 0
    assert music["cases"] == 1 and music["passed"] == 0 and music["fallbacks"] == 1
    assert doors["failed_checks"] == {} and music["failed_checks"] == {"act": 1}


def test_same_oracle_scores_local_and_gemini_business_decisions():
    expected = expected_decisions([{"name": "open_door", "arguments": {"targets": ["driver"]}}], ["door.set_open"])
    semantic = {
        "act": "request",
        "actions": [{"capability": "door.set_open", "arguments": {"open": True, "zone": "driver"}}],
    }
    assert score({"decisions": {"decisions": expected}}, {"decisions": actual_decisions(semantic)})[1]
    semantic["actions"][0]["arguments"]["zone"] = "rear_right"
    assert not score({"decisions": {"decisions": expected}}, {"decisions": actual_decisions(semantic)})[1]


def test_equivalent_target_coordinates_and_entity_spelling_are_not_model_errors():
    wanted = [{"name": "door.set_open", "arguments": {"zone": "driver", "open": True}}]
    actual = [{"name": "door.set_open", "arguments": {"target_side": "left", "target_row": "front", "open": True}}]
    assert score({"decisions": {"decisions": wanted}}, {"decisions": actual})[1]
    actual[0]["arguments"]["target_row"] = "rear"
    assert not score({"decisions": {"decisions": wanted}}, {"decisions": actual})[1]


@pytest.mark.asyncio
async def test_resume_reuses_completed_failures_but_reruns_rate_limits(tmp_path, monkeypatch):
    from benchmarks.agent import run as runner

    cases = datasets()["tools"][:2]
    calls = []

    async def evaluate(case, *_):
        calls.append(case.id)
        limited = case == cases[1] and len(calls) == 2
        return [{"passed": False, "actual": {"attempts": [{"http_status": 429}]} if limited else {}}]

    monkeypatch.setattr(runner, "tools_case", evaluate)
    args = Namespace(
        mode="fixture",
        provider="local",
        output=tmp_path,
        transport="json_schema",
        fingerprints=runner.fingerprints(),
        seed=0,
        repeats=1,
        quiet=True,
        concurrency=1,
        resume=False,
        rate_limit_retries=0,
    )
    selected = [("tools", case) for case in cases]
    first = await run(args, selected)
    assert first["service_errors"] == {"http_429": 1}
    args.resume = True
    second = await run(args, selected)
    assert calls == [cases[0].id, cases[1].id, cases[1].id]
    assert second["cases"] == 2 and second["passed"] == 0
    assert second["service_errors"] == {}
    args.seed = 1
    with pytest.raises(ValueError, match="identical"):
        await run(args, selected)


def test_http_429_is_a_service_failure_regardless_of_semantic_fallback():
    assert rate_limited([{"actual": {"attempts": [{"http_status": 429}], "fallback": True}}])
    assert not rate_limited([{"actual": {"attempts": [{"outcome": "invalid"}]}}])


def test_nearest_place_query_paraphrases_keep_place_and_location_constraints():
    wanted = [
        {
            "name": "navigation.search",
            "arguments": {"query": "nhà thuốc gần đây", "nearest": True, "category": "pharmacy"},
        }
    ]
    actual = [
        {"name": "navigation.search", "arguments": {"query": "nhà thuốc", "nearest": True, "category": "pharmacy"}}
    ]
    assert score({"decisions": {"decisions": wanted}}, {"decisions": actual})[1]
    actual[0]["arguments"]["query"] = "nhà thuốc ở Huế"
    assert not score({"decisions": {"decisions": wanted}}, {"decisions": actual})[1]
    actual[0]["arguments"].update(query="nhà thuốc", nearest=False)
    assert not score({"decisions": {"decisions": wanted}}, {"decisions": actual})[1]


def test_structured_destination_and_text_query_score_the_same_place():
    wanted = [
        {"name": "navigation.plan", "arguments": {"query": "Tràng An", "purpose": "itinerary", "trip_kind": "one_way"}}
    ]
    semantic = {
        "actions": [
            {
                "capability": "navigation.plan",
                "arguments": {
                    "destination": {"raw_text": "Tràng An", "selection": "named_place"},
                    "purpose": "itinerary",
                },
            }
        ]
    }
    assert decisions_match(actual_decisions(semantic), wanted)
    semantic["actions"][0]["arguments"]["destination"]["raw_text"] = "Hạ Long"
    assert not decisions_match(actual_decisions(semantic), wanted)


def test_rescore_preserves_generation_source_original_output_and_provider_failures(monkeypatch):
    from benchmarks.agent import rescore as scorer

    expected = [
        {"name": "navigation.plan", "arguments": {"query": "Tràng An", "purpose": "itinerary", "trip_kind": "one_way"}}
    ]
    monkeypatch.setattr(
        scorer, "fingerprints", lambda: {"source_sha256": "new-scorer", "dataset_sha256": {"cases": "frozen"}}
    )
    monkeypatch.setattr(
        scorer,
        "datasets",
        lambda: {"language": [SimpleNamespace(id="case", expect={"decisions": {"decisions": expected}})]},
    )
    report = {
        "mode": "live",
        "provider": "google",
        "source_sha256": "original-generation",
        "dataset_sha256": {"cases": "frozen"},
        "results": [
            {
                "id": "case",
                "suite": "language",
                "group": "navigation",
                "feature": "navigation",
                "input_style": "direct",
                "passed": False,
                "turns": [
                    {
                        "passed": False,
                        "checks": {"provider_served": {"passed": True, "expected": "google", "actual": "google"}},
                        "actual": {
                            "decisions": [],
                            "actions": [
                                {
                                    "capability": "navigation.plan",
                                    "arguments": {"destination": {"raw_text": "Tràng An"}, "purpose": "itinerary"},
                                }
                            ],
                        },
                    }
                ],
            }
        ],
    }
    result = scorer.rescore(report)
    assert result["passed"] == 1 and not report["results"][0]["passed"]
    assert result["source_sha256"] == "original-generation" and result["scoring_source_sha256"] == "new-scorer"
    report["results"][0]["turns"][0]["checks"]["provider_served"]["passed"] = False
    assert scorer.rescore(report)["passed"] == 0
    report["dataset_sha256"] = {"cases": "changed"}
    with pytest.raises(ValueError, match="Dataset changed"):
        scorer.rescore(report)
