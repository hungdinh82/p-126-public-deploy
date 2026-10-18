import json

import pytest

from benchmarks.agent import html_report


def test_failure_html_includes_test_output_explanation_and_escapes_untrusted_text(tmp_path, monkeypatch):
    authored = {"source": "cases/music/language.jsonl:1", "sha256": "frozen", "test": {
        "id": "music.pause/../../x", "text": "<script>alert('input')</script>", "expect": {"blocked": True}}}
    row = {"id": authored["test"]["id"], "suite": "language", "feature": "music", "repeat": 1,
           "input_style": "direct", "passed": False, "turns": [{"turn": 1, "text": authored["test"]["text"],
           "passed": False, "actual": {"text": "<img src=x onerror=alert(1)>", "attempts": [{"http_status": None}]},
           "checks": {"blocked": {"passed": False, "expected": True, "actual": False}}}]}
    monkeypatch.setattr(html_report, "catalog", lambda _: {("language", row["id"]): authored})
    html_report.write_html({"model": "test", "results": [row]}, tmp_path)
    index = (tmp_path / "analysis.html").read_text()
    assert "&lt;script&gt;" in index and "<script>alert" not in index
    failures = list((tmp_path / "failures/music").glob("*.html"))
    assert len(failures) == 1
    content = failures[0].read_text()
    assert '"expected": true' in content.replace('&quot;', '"')
    assert "ngoài phạm vi" in content and "Nội dung bài test gốc" in content
    assert "&lt;img" in content and "<img src=x" not in content
    bundle = json.loads(failures[0].with_suffix(".json").read_text())
    assert bundle["test"] == authored["test"] and bundle["result"] == row
    assert bundle["explanations"][0]["reasons"][0][0] == "guard"
    analysis = json.loads((tmp_path / "analysis.json").read_text())
    assert analysis["failed"] == 1 and analysis["passed"] == 0
    assert (tmp_path / analysis["failure_files"][0]).is_file()


def test_html_report_rejects_changed_dataset_instead_of_attaching_wrong_test():
    with pytest.raises(ValueError, match="Dataset differs"):
        html_report.catalog({"dataset_sha256": {}})


def test_service_and_runtime_failures_are_not_explained_as_model_semantic_errors():
    reasons = html_report.explain({"error": "TimeoutError", "actual": {"attempts": [{"http_status": 503}]}})
    assert {category for category, _ in reasons} == {"service", "runtime"}


def test_explanation_identifies_missing_tool_catalog_with_recorded_evidence():
    turn = {"actual": {"attempts": [{"proposal_attempts": [{"catalog": ["media.play"]}]}]},
            "checks": {"decisions:decisions": {"passed": False,
                "expected": [{"name": "media.pause", "arguments": {}}],
                "actual": [{"name": "media.play", "arguments": {}}]}}}
    reasons = html_report.explain(turn)
    assert {category for category, _ in reasons} == {"catalog", "capability"}
    assert "media.pause" in reasons[0][1] and "media.play" in reasons[0][1]
    turn["actual"]["attempts"][0]["proposal_attempts"].append({"catalog": ["media.pause"]})
    assert "catalog" not in {category for category, _ in html_report.explain(turn)}


def test_multiturn_failure_points_to_first_failure_instead_of_blaming_confirmation():
    turns = [{"turn": 1, "passed": False, "actual": {}}, {"turn": 2, "passed": False, "actual": {}}]
    row = {"id": "confirmation", "suite": "tools", "feature": "doors", "turns": turns}
    bundle = html_report.failure_bundle(row, {"test": {"turns": []}}, {})
    assert bundle["first_failed_turn"] == 1 and "lượt trước đã fail" in bundle["multi_turn_note"]


def test_model_output_preserves_first_call_and_repair_separately_from_agent_result():
    turn = {"actual": {"attempts": [{"proposal_attempts": [
        {"calls": [["play_music", {}]], "validation": "rejected"},
        {"calls": [["pause_music", {}]], "validation": "accepted"}]}],
        "output": {"response_text": "Đã tắt nhạc"}}, "checks": {
        "decisions:decisions": {"passed": False, "expected": [{"name": "media.pause"}], "actual": []}}}
    recorded = html_report.model_output(turn)
    assert recorded[0]["tool_calls"] == [["play_music", {}]]
    assert recorded[1]["tool_calls"] == [["pause_music", {}]]
    rendered = html_report.comparison(turn)
    assert "Expected" in rendered and "media.pause" in rendered
    assert "play_music" in rendered and "pause_music" in rendered
    assert "Kết quả agent dùng để chấm" in rendered and "Đã tắt nhạc" in rendered


def test_missing_model_output_is_explicit_instead_of_using_agent_response_as_model_output():
    turn = {"actual": {"output": {"response_text": "Host trả lời"}}}
    assert "Không có output model" in html_report.model_output(turn)
    assert "Host trả lời" not in html_report.model_output(turn)
