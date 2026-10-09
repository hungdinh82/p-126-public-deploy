from eval.dialogue_v2_benchmark import checks


def test_eval_checks_actual_execution_and_exact_exclusions():
    turn = {"expected_executed": True, "expected_arguments": {"zones": ["rear_right", "rear_left"], "position_percent": 0},
            "expected_state": {"window_positions.driver": 100}}
    response = {"status": "verified", "route": "vehicle", "message": "Đã đóng hai kính sau.",
                "execution": {"executed": True}, "action": {"intent": "window.set_position", "arguments": {"zones": ["rear_left", "rear_right"], "position_percent": 0}},
                "vehicle_state": {"window_positions": {"driver": 100}}, "diagnostics": {}}
    assertions, _ = checks(turn, response)
    assert all(assertions.values())
    response["execution"]["executed"] = False
    response["action"]["arguments"] = {"zone": "all", "position_percent": 0}
    assertions, _ = checks(turn, response)
    assert not assertions["executed"] and not assertions["arguments"]


async def _run_all_log_cases(monkeypatch):
    from pathlib import Path
    from types import SimpleNamespace

    # Intent/task regressions do not depend on a installed handbook artifact.
    # Retrieval/grounding are tested independently; no policy/effect is mocked.
    import src.vivi.rag.runtime as runtime
    from eval.dialogue_v2_benchmark import benchmark
    monkeypatch.setattr(runtime, "SQLiteHandbookRetriever", lambda *a, **kw: SimpleNamespace(retrieve=lambda *args: []))
    report = await benchmark(SimpleNamespace(provider="rules", dataset=Path(__file__).parents[1] / "eval/dialogue_v2_cases.jsonl", max_cases=9, max_tokens=2048))
    assert report["passed"] == report["cases"] == 9, [case for case in report["results"] if not case["passed"]]


def test_all_log_cases_use_the_unified_workflow(monkeypatch):
    import asyncio
    asyncio.run(_run_all_log_cases(monkeypatch))
