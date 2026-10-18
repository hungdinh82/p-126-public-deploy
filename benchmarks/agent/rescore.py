"""Re-score saved language decisions without calling models or changing oracles."""

import argparse
import copy
import json
from pathlib import Path

from benchmarks.agent.oracles import actual_decisions
from benchmarks.agent.run import datasets, fingerprints, score, summarize, write_comparison, write_feature_reports


def rescore(report):
    current = fingerprints()
    if report["dataset_sha256"] != current["dataset_sha256"]:
        raise ValueError("Dataset changed; saved outputs require their original authored oracles")
    result = copy.deepcopy(report)
    cases = {case.id: case for case in datasets()["language"]}
    for row in result["results"]:
        if row["suite"] == "tools" and result["mode"] == "live":
            for turn in row["turns"]:
                checks = turn.get("checks", {})
                raw = {k: v for k, v in checks.items() if k.startswith(("tool_calls", "first_calls"))}
                turn.setdefault("local_tool_diagnostics", {}).update(raw)
                turn["checks"] = {k: v for k, v in checks.items() if k not in raw}
                turn["passed"] = not turn.get("error") and bool(turn["checks"]) and all(v["passed"] for v in turn["checks"].values())
            row["passed"] = bool(row["turns"]) and all(t["passed"] for t in row["turns"])
        if row["suite"] != "language":
            continue
        case = cases[row["id"]]
        for turn in row["turns"]:
            if "actual" not in turn:
                continue
            actual = turn["actual"]
            actual["decisions"] = actual_decisions(actual)
            assertions, passed = score(case.expect, actual)
            # Preserve the recorded provider check; rule fallback is never model accuracy.
            provider = turn["checks"]["provider_served"]
            turn["checks"] = {**assertions, "provider_served": provider}
            turn["passed"] = passed and provider["passed"]
        row["passed"] = bool(row["turns"]) and all(t["passed"] for t in row["turns"])
    result.update(summarize(result["results"]))
    result["scoring_source_sha256"] = current["source_sha256"]
    result["scoring_note"] = "Language decisions re-scored from captured semantic output; generation source hash, provider checks and authored dataset retained. No model calls."
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("Choose a new output directory to preserve original reports")
    if (args.input / "comparison.json").exists():
        reports = [rescore(json.loads((args.input / name / "report.json").read_text()))
                   for name in ("jetson", "gemini")]
        write_comparison(reports, args.output)
    else:
        write_feature_reports(rescore(json.loads((args.input / "report.json").read_text())), args.output)


if __name__ == "__main__":
    main()
