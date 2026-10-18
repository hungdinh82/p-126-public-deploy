"""Rerun a frozen failure cohort. Never expands to the full suite automatically."""

import argparse
import asyncio
import hashlib
import json
from collections import Counter
from pathlib import Path

from benchmarks.agent.html_report import esc, page
from benchmarks.agent.run import datasets, fingerprints, identity, run, summarize, tools_case, write_feature_reports


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, default=Path("benchmarks/agent/results/jetson-v13-complete/report.json"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--feature", action="append")
    parser.add_argument("--concurrency", type=int, default=4, choices=range(1, 5))
    args = parser.parse_args()
    baseline = json.loads(args.baseline.read_text())
    def in_scope(row):
        return row["feature"] != "navigation" and not any(
            capability.startswith("navigation.") for capability in row.get("covers", [])
        )

    all_failed = {(row["suite"], row["id"]) for row in baseline["results"] if not row["passed"] and in_scope(row)}
    failed = {(row["suite"], row["id"]) for row in baseline["results"] if not row["passed"] and in_scope(row)
              and (not args.feature or row["feature"] in args.feature)}
    selected = [(suite, case) for suite, cases in datasets().items() for case in cases
                if (suite, identity(case)[0]) in failed]
    assert len(selected) == len(failed) > 0
    args.output.mkdir(parents=True, exist_ok=True)
    if (args.output / "progress.jsonl").exists():
        parser.error("Choose a new output directory for a changed system")
    (args.output / "cohort.json").write_text(json.dumps({
        "baseline": str(args.baseline), "baseline_sha256": hashlib.sha256(args.baseline.read_bytes()).hexdigest(),
        "cases": sorted(failed), "excluded_scope": "navigation, including combined navigation cases",
        "acceptance": {"resolve_recurring_error_groups": True, "no_wrong_vehicle_effect": True},
    }, ensure_ascii=False, indent=2) + "\n")
    args.mode, args.provider, args.transport = "live", "local", "json_schema"
    args.seed, args.repeats, args.quiet, args.rate_limit_retries = 0, 1, True, 0
    args.gemini_delay, args.resume, args.fingerprints = 3, False, fingerprints()
    assert args.fingerprints["dataset_sha256"] == baseline["dataset_sha256"], "Expected corpus changed"
    async def evaluate():
        references = {}
        for suite, case in selected:
            if suite == "tools":
                references[case.id] = await tools_case(case, None, None, "local")
        report = await run(args, selected)
        for row in report["results"]:
            if row["suite"] != "tools":
                continue
            for turn, reference in zip(row["turns"], references[row["id"]], strict=False):
                actual_vehicle = turn.get("actual", {}).get("vehicle")
                expected_vehicle = reference.get("actual", {}).get("vehicle")
                assert expected_vehicle is not None, "Fixture could not produce a reference vehicle state"
                def physical(state):
                    return {k: v for k, v in (state or {}).items() if k not in {"state_version", "updated_at"}}
                matched = actual_vehicle is not None and physical(actual_vehicle) == physical(expected_vehicle)
                turn.setdefault("checks", {})["vehicle_effect_matches_reference"] = {
                    "passed": matched, "expected": True, "actual": matched,
                }
                turn["passed"] = turn["passed"] and matched
            row["passed"] = all(turn["passed"] for turn in row["turns"])
        report.update(summarize(report["results"]))
        return report

    report = asyncio.run(evaluate())
    write_feature_reports(report, args.output)
    wrong_effects = []
    body = f"<h1>Kiểm tra riêng các ca còn lỗi</h1><p>{report['passed']}/{report['cases']} đạt. Chưa chạy lại cả bộ.</p><table><tr><th>Ca / input</th><th>Lỗi</th></tr>"
    for row in report["results"]:
        if row["passed"]:
            continue
        errors = []
        for turn in row["turns"]:
            paths = [key for key, check in turn.get("checks", {}).items() if not check["passed"]]
            errors.append(f"Lượt {turn.get('turn', '?')}: " + (", ".join(paths) or turn.get("error", "")))
            if any(path.startswith(("vehicle.", "vehicle_effect")) for path in paths) or (
                "vehicle_unchanged" in paths and turn["checks"]["vehicle_unchanged"]["expected"] is True
            ):
                wrong_effects.append(row["id"])
        text = " → ".join(turn.get("text", "") for turn in row["turns"])
        body += f"<tr><td>{esc(row['id'])}<p>{esc(text)}</p></td><td>{esc('; '.join(errors))}</td></tr>"
    eligible = False  # Whole-suite execution requires a review of the recurring groups.
    (args.output / "gate.json").write_text(json.dumps({"eligible_for_full_suite": eligible, "passed": report["passed"],
        "cases": report["cases"], "full_failure_cohort": failed == all_failed, "requires_review": True,
        "remaining_by_feature": dict(Counter(row["feature"] for row in report["results"] if not row["passed"])),
        "wrong_vehicle_effect_cases": sorted(set(wrong_effects))}, indent=2) + "\n")
    (args.output / "analysis.html").write_text(page("Các ca lỗi", body + "</table>"))
    print(f"{report['passed']}/{report['cases']} passed; gate={eligible}; {args.output / 'analysis.html'}")


if __name__ == "__main__":
    main()
