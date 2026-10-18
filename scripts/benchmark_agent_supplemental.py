"""Run independent short-answer/form cases without changing the core corpus.

PYTHONPATH=. .venv/bin/python scripts/benchmark_agent_supplemental.py --mode live
"""

import argparse
import asyncio
import hashlib
import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from benchmarks.agent.html_report import esc, page
from benchmarks.agent.run import fingerprints, run, write_feature_reports
from benchmarks.agent.schema import load


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("fixture", "live"), default="fixture")
    parser.add_argument("--provider", choices=("local", "google"), default="local")
    parser.add_argument("--cases", type=Path, default=Path("benchmarks/agent/supplemental/short_turns.jsonl"))
    parser.add_argument("--output", type=Path)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--concurrency", type=int, choices=range(1, 5), default=2)
    args = parser.parse_args()
    if args.output is None:
        stamp = datetime.now(ZoneInfo("Asia/Ho_Chi_Minh")).strftime("%Y%m%d-%H%M%S-%f")
        args.output = Path("benchmarks/agent/results") / f"{stamp}-supplemental-{args.mode}-{args.provider}"
    args.output.mkdir(parents=True, exist_ok=True)
    frozen = args.output / "supplemental-input.json"
    digest = hashlib.sha256(args.cases.read_bytes()).hexdigest()
    if args.resume and frozen.exists():
        if json.loads(frozen.read_text())["sha256"] != digest:
            parser.error("Supplemental dataset changed; choose a new output directory")
    elif (args.output / "progress.jsonl").exists():
        parser.error("Output exists; use --resume or a new directory")
    else:
        frozen.write_text(json.dumps({"path": str(args.cases), "sha256": digest}, indent=2) + "\n")
    cases = load(args.cases, "tools")
    args.transport, args.fingerprints = "json_schema", fingerprints()
    args.seed, args.repeats, args.quiet, args.rate_limit_retries = 0, 1, True, 0
    args.gemini_delay = 3
    report = asyncio.run(run(args, [("tools", case) for case in cases]))
    report["supplemental_dataset_sha256"] = digest
    report["supplemental_inputs_changed"] = hashlib.sha256(args.cases.read_bytes()).hexdigest() != digest
    write_feature_reports(report, args.output)
    body = f"<h1>Câu ngắn và form nhiều lượt</h1><p>{report['passed']}/{report['cases']} ca đạt. Bộ riêng, không gộp vào baseline 834 ca.</p>"
    body += "<table><tr><th>Ca / input</th><th>Lỗi</th></tr>"
    for row in report["results"]:
        if row["passed"]:
            continue
        errors = []
        for turn in row["turns"]:
            if turn["passed"]:
                continue
            paths = [key for key, check in turn.get("checks", {}).items() if not check["passed"]]
            message = ", ".join(paths) or turn.get("error", "Không có trace chi tiết")
            errors.append(f"Lượt {turn.get('turn', '?')}: không đạt {message}")
        text = " → ".join(turn.get("text", "") for turn in row["turns"])
        body += f"<tr><td>{esc(row['id'])}<p>{esc(text)}</p></td><td>{esc('; '.join(errors))}</td></tr>"
    body += "</table><p>Output/expected được giữ trong JSON; HTML chỉ ghi lỗi.</p>"
    (args.output / "analysis.html").write_text(page("Câu ngắn và form nhiều lượt", body))
    print(f"{report['passed']}/{report['cases']} passed; {args.output / 'analysis.html'}")
    raise SystemExit(
        0
        if report["passed"] == report["cases"]
        and not report["supplemental_inputs_changed"]
        and not report["inputs_changed_during_run"]
        else 1
    )


if __name__ == "__main__":
    main()
