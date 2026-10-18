"""python -m benchmarks.agent.run --mode fixture --suite all"""

import argparse
import asyncio
import fnmatch
import hashlib
import json
import random
import tempfile
import time
from collections import Counter
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from benchmarks.agent.oracles import actual_decisions, decisions_match
from benchmarks.agent.runtime import AgentRuntime, live_resolver, proposal_calls
from benchmarks.agent.schema import load
from eval.benchmark import local_network_only
from eval.semantic_benchmark import inference_breakdown
from eval.workflow_benchmark import Runtime as WorkflowRuntime
from eval.workflow_benchmark import checks, latency, load_cases, metadata, nested, references
from src.vivi.agents.tools import TOOL_REGISTRY
from src.vivi.capabilities.registry import catalog
from src.vivi.config import Settings
from src.vivi.dialogue.contracts import SessionState
from src.vivi.dialogue.local_operations import CONTRACT_VERSION

ROOT = Path(__file__).resolve().parent
FEATURES = ("doors", "windows", "music", "climate", "body_panels", "vehicle_status",
            "navigation", "information", "memory", "dialogue", "hard_cases", "multi_tool", "adversarial")
EXCLUDED_CAPABILITIES = {"seat.set_heat_level": "Không có trong app thực tế; người dùng yêu cầu không benchmark."}
EXCLUDED_MODEL_TOOLS = {"play_requested_music": "App chỉ bật/tắt một đoạn nhạc cố định."}


def fingerprints():
    source = hashlib.sha256()
    for base in (ROOT, ROOT.parents[1] / "src/vivi", ROOT.parents[1] / "fixtures", ROOT.parents[1] / "eval"):
        for path in sorted(base.rglob("*.py")):
            source.update(str(path.relative_to(ROOT.parents[1])).encode())
            source.update(path.read_bytes())
    return {"source_sha256": source.hexdigest(), "dataset_sha256": {
        str(p.relative_to(ROOT / "cases")): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted((ROOT / "cases").rglob("*.jsonl"))
    }}


def datasets():
    data = {suite: [] for suite in ("tools", "language", "workflows")}
    for feature in FEATURES:
        for suite in data:
            path = ROOT / "cases" / feature / f"{suite}.jsonl"
            if not path.exists():
                continue
            cases = load_cases(path) if suite == "workflows" else load(path, suite)
            for case in cases:
                actual_feature = case.get("feature") if isinstance(case, dict) else case.feature
                if actual_feature != feature:
                    raise ValueError(f"Feature does not match folder: {path}")
            data[suite].extend(cases)
    for suite, cases in data.items():
        ids = [identity(c)[0] for c in cases]
        if not cases or len(ids) != len(set(ids)):
            raise ValueError(f"Empty suite or duplicate IDs across feature folders: {suite}")
        for case in cases:
            names = case.get("tool_names", []) if isinstance(case, dict) else case.tool_names
            if set(covers(case)) & EXCLUDED_CAPABILITIES.keys() or set(names) & EXCLUDED_MODEL_TOOLS.keys():
                raise ValueError(f"Out-of-scope app feature: {identity(case)[0]}")
    return data


def feature_of(case):
    return case["feature"] if isinstance(case, dict) else case.feature


def identity(case):
    return (case["id"], case["group"]) if isinstance(case, dict) else (case.id, case.group)


def covers(case):
    if not isinstance(case, dict):
        return case.covers
    return case.get("covers", [])


def score(expected, actual):
    ordinary, special = {}, {}
    for path, rule in expected.items():
        if isinstance(rule, dict) and set(rule) & {"same_calls", "decisions", "ordered_decisions"}:
            special[path] = {key: value for key, value in rule.items() if key in {"same_calls", "decisions", "ordered_decisions"}}
            rule = {key: value for key, value in rule.items() if key not in {"same_calls", "decisions", "ordered_decisions"}}
            if not rule:
                continue
        ordinary[path] = rule
    result = checks(ordinary, actual)
    for path, rules in special.items():
        value = nested(actual, path)
        for operator, wanted in rules.items():
            def encode(items):
                return Counter(json.dumps(item, sort_keys=True, ensure_ascii=False) for item in items)
            passed = (isinstance(value, list) and encode(value) == encode(wanted)) if operator == "same_calls" else decisions_match(value, wanted, ordered=operator == "ordered_decisions")
            result[f"{path}:{operator}"] = {"passed": passed, "expected": wanted, "actual": value}
    return result, all(c["passed"] for c in result.values())


async def tools_case(case, client, config, provider):
    turns = []
    with tempfile.TemporaryDirectory(prefix="vivi-agent-bench-") as directory:
        runtime = AgentRuntime(Path(directory), case, client=client, config=config, provider=provider)
        try:
            for number, turn in enumerate(case.turns, 1):
                started = time.perf_counter()
                try:
                    actual = await runtime.turn(turn)
                    expected = dict(turn.expect)
                    raw_checks = {}
                    if client:
                        expected.pop("model_calls", None)
                        raw_expected = {k: v for k, v in expected.items() if k.startswith(("tool_calls", "first_calls"))}
                        if provider == "local":
                            raw_checks = score(raw_expected, actual)[0]
                        expected = {k: v for k, v in expected.items() if k not in raw_expected}
                    expected.update({"fixture_errors": {"length": 0}, "unused_fixture_attempts": 0})
                    # A model outage cannot pass an action/read case through rules.
                    if client and not turn.fixture_error and turn.fixture_attempts:
                        expected["fallback"] = False
                        expected["output.diagnostics.effective_provider"] = provider
                    assertions, passed = score(expected, actual)
                    turns.append({"turn": number, "text": turn.text, "passed": passed,
                                  "checks": assertions, "local_tool_diagnostics": raw_checks, "actual": actual,
                                  "wall_ms": round((time.perf_counter() - started) * 1000, 2)})
                except Exception as exc:
                    turns.append({"turn": number, "text": turn.text, "passed": False,
                                  "error": f"{type(exc).__name__}: {exc}"})
                    break
        finally:
            await runtime.close()
    return turns


async def language_case(case, resolver, provider):
    started = time.perf_counter()
    async def observation():
        return case.observation
    semantic, served, attempts = await resolver.resolve(
        case.text, SessionState.model_validate(case.session), requested=provider,
        history=case.history, catalog=catalog(navigation=True),
        observation_loader=observation if case.observation else None,
    )
    actual = semantic.model_dump(mode="json")
    actual["decisions"] = actual_decisions(actual)
    actual.update(
        blocked=semantic.act in {"clarify", "unsupported"} or any(
            (semantic.negated, semantic.conditional, semantic.scheduled, semantic.conflicting, semantic.unresolved)
        ),
        **proposal_calls(attempts),
    )
    assertions, passed = score(case.expect, actual)
    raw_checks = score(case.local_tool_expect, actual)[0] if provider == "local" and case.local_tool_expect else {}
    literal = any(a.get("source") == "literal_question_reply" for a in attempts)
    actual.update(fallback=served != provider and not literal,
                  model_calls=sum(a.get("calls", 0) for a in attempts))
    assertions["provider_served"] = {"passed": served == provider or literal, "expected": provider, "actual": served}
    return [{"turn": 1, "text": case.text, "passed": passed and (served == provider or literal),
             "checks": assertions, "local_tool_diagnostics": raw_checks, "actual": {**actual, "attempts": attempts},
             "wall_ms": round((time.perf_counter() - started) * 1000, 2)}]


async def workflow_case(case):
    turns, outputs = [], {}
    with tempfile.TemporaryDirectory(prefix="vivi-workflow-bench-") as directory:
        runtime = WorkflowRuntime(Path(directory), case)
        try:
            for operation in case["operations"]:
                started = time.perf_counter()
                actual = await runtime.execute(operation, outputs)
                attempts = actual.get("diagnostics", {}).get("provider_attempts", [])
                actual.update(attempts=attempts,
                              fallback=actual.get("diagnostics", {}).get("semantic_source") == "fallback",
                              repairs=sum(a.get("repairs", 0) for a in attempts))
                outputs[operation["id"]] = actual
                assertions, passed = score(references(operation["expect"], outputs), actual)
                turns.append({"turn": operation["id"], "passed": passed, "checks": assertions,
                              "actual": actual, "wall_ms": round((time.perf_counter() - started) * 1000, 2)})
        finally:
            await runtime.close()
    return turns


def summarize(rows):
    summary = {}
    for key in ("suite", "group", "feature", "input_style"):
        summary[key] = {
            name: {"cases": len(items), "passed": sum(r["passed"] for r in items)}
            for name in sorted({r[key] for r in rows})
            for items in [[r for r in rows if r[key] == name]]
        }
    attempts = [a for r in rows for t in r["turns"] for a in t.get("actual", {}).get("attempts", [])]
    failed_checks = Counter(path for r in rows for t in r["turns"]
                            for path, check in t.get("checks", {}).items() if not check["passed"])
    exercised = set()
    for row in rows:
        for turn in row["turns"]:
            actual = turn.get("actual", {})
            semantic = actual.get("semantic") or actual.get("diagnostics", {}).get("semantic") or (actual if "act" in actual else {})
            exercised.update(a["capability"] for a in semantic.get("actions", []))
            exercised.update(s["capability"] for s in actual.get("steps", []) if s.get("capability"))
    return {
        "cases": len(rows), "passed": sum(r["passed"] for r in rows), "summary": summary,
        "service_errors": dict(Counter(r["service_error"] for r in rows if r.get("service_error"))),
        "latency": latency([t["wall_ms"] for r in rows for t in r["turns"] if "wall_ms" in t]),
        "model_latency": latency([a["latency_ms"] for a in attempts if "latency_ms" in a]),
        "capabilities_exercised": sorted(exercised),
        "capabilities_not_exercised": sorted(TOOL_REGISTRY.keys() - exercised - EXCLUDED_CAPABILITIES.keys()),
        "passed_without_repair_or_fallback": sum(r["passed"] and all(
            not t.get("actual", {}).get("fallback") and not t.get("actual", {}).get("repairs")
            for t in r["turns"]
        ) for r in rows),
        "repairs": sum(a.get("repairs", 0) for a in attempts),
        "inference_breakdown": inference_breakdown([{"attempts": attempts}]),
        "fallbacks": sum(t.get("actual", {}).get("fallback", False) for r in rows for t in r["turns"]),
        "failed_checks": dict(failed_checks), "results": rows,
    }


def rate_limited(turns):
    return any(a.get("http_status") == 429 for t in turns
               for a in t.get("actual", {}).get("attempts", []))


async def run(args, selected):
    config, client, resolver = None, None, None
    if args.mode == "live":
        config = Settings().model_copy(update={"local_semantic_contract": "operations_v1",
                                               "local_semantic_transport": args.transport})
        client, resolver = live_resolver(args.provider, config)
    rows = []
    args.output.mkdir(parents=True, exist_ok=True)
    progress = args.output / "progress.jsonl"
    checkpoint = args.output / "checkpoint.json"
    signature = {**args.fingerprints, "mode": args.mode, "provider": args.provider,
                 "model": getattr(client, "model", None), "transport": args.transport,
                 "seed": args.seed, "repeats": args.repeats,
                 "cases": [[suite, identity(case)[0]] for suite, case in selected]}
    completed = set()
    if getattr(args, "resume", False) and checkpoint.exists():
        if json.loads(checkpoint.read_text()) != signature:
            raise ValueError("Resume requires identical source, dataset, model and case selection")
        prior = {}
        for line in progress.read_text().splitlines():
            row = json.loads(line)
            prior[(row["suite"], row["id"], row["repeat"])] = row
        rows = [row for row in prior.values() if not rate_limited(row["turns"])]
        completed = {(row["suite"], row["id"], row["repeat"]) for row in rows}
    elif progress.exists():
        raise ValueError("Output already contains results; use --resume or a new output directory")
    else:
        checkpoint.write_text(json.dumps(signature, ensure_ascii=False, indent=2) + "\n")
        progress.touch()
    async def evaluate_case(suite, case, repeat):
        case_id, group = identity(case)
        if not args.quiet:
            print(f"[{args.provider}/{feature_of(case)}/{suite}] {case_id} ({repeat}/{args.repeats})", flush=True)
        retries = []
        for attempt in range(getattr(args, "rate_limit_retries", 0) + 1):
            try:
                if suite == "tools":
                    turns = await tools_case(case, client, config, args.provider)
                elif suite == "language":
                    turns = await language_case(case, resolver, args.provider)
                else:
                    turns = await workflow_case(case)
            except Exception as exc:
                turns = [{"passed": False, "error": f"{type(exc).__name__}: {exc}"}]
            if not rate_limited(turns) or attempt == getattr(args, "rate_limit_retries", 0):
                break
            retries.append(turns)
            await asyncio.sleep(30)
        rows.append({"id": case_id, "group": group, "feature": feature_of(case),
                     "tool_names": case.get("tool_names", []) if isinstance(case, dict) else case.tool_names,
                     "input_style": case.get("input_style", "workflow") if isinstance(case, dict) else case.input_style,
                     "suite": suite, "repeat": repeat,
                     "covers": covers(case), "passed": bool(turns) and all(t["passed"] for t in turns),
                     "turns": turns, "rate_limit_retries": retries,
                     "service_error": "http_429" if rate_limited(turns) else None})
        encoded = json.dumps(rows[-1], ensure_ascii=False) + "\n"
        with progress.open("a", encoding="utf-8") as handle:
            handle.write(encoded)
        feature_dir = args.output / feature_of(case) / suite
        feature_dir.mkdir(parents=True, exist_ok=True)
        with (feature_dir / "results.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(encoded)

    semaphore = asyncio.Semaphore(args.concurrency)

    async def limited(suite, case, repeat):
        if (suite, identity(case)[0], repeat) in completed:
            return
        async with semaphore:
            await evaluate_case(suite, case, repeat)
            if args.mode == "live" and args.provider == "google":
                await asyncio.sleep(getattr(args, "gemini_delay", 0))

    try:
        for repeat in range(1, args.repeats + 1):
            for feature in FEATURES:
                batch = [(suite, case) for suite, case in selected if feature_of(case) == feature]
                await asyncio.gather(*(limited(suite, case, repeat) for suite, case in batch))
    finally:
        if client and hasattr(client, "aclose"):
            await client.aclose()
    return {
        **metadata(), "mode": args.mode, "provider": args.provider if client else "authored_tool_fixture",
        "model": getattr(client, "model", None), "contract": CONTRACT_VERSION,
        "budgets": {"local": config.local_budget_seconds, "cloud": config.cloud_budget_seconds,
                    "semantic": config.semantic_budget_seconds, "turn": config.turn_timeout_seconds} if config else None,
        "seed": args.seed, "repeats": args.repeats, "concurrency": args.concurrency,
        **args.fingerprints,
        "inputs_changed_during_run": fingerprints() != args.fingerprints,
        **summarize(rows),
        "limitations": [
            "Development corpus, not an independently reviewed holdout.",
            "Fixture scores measure host contracts with authored model output, not model language accuracy.",
            "Live mode calls only the selected model; vehicle, maps and databases remain synthetic/temporary.",
            "Information/RAG/conversation cases use synthetic evidence or unavailable services, not live source/answer-quality evaluation.",
            "STT/TTS, browser, MQTT and physical hardware are outside this text-agent benchmark.",
        ],
    }


def write_report(report, directory):
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    lines = ["# Agent behavior benchmark", "", f"Mode: `{report['mode']}`; provider: `{report['provider']}`; contract: `{report['contract']}`.", "",
             f"Cases passed: **{report['passed']}/{report['cases']}**. Development corpus.", "",
             f"Service errors (included as failed runs, not semantic accuracy): `{report.get('service_errors', {})}`.", "",
             f"Generation source: `{report.get('source_sha256', 'unavailable')}`; scoring source: `{report.get('scoring_source_sha256', report.get('source_sha256', 'unavailable'))}`.", "",
             f"Inputs changed during run: **{report.get('inputs_changed_during_run', False)}**.", "",
             "| Suite | Passed | Cases |", "|---|---:|---:|"]
    for name, counts in report["summary"]["suite"].items():
        lines.append(f"| {name} | {counts['passed']} | {counts['cases']} |")
    lines += ["", "## Failures", ""]
    for row in report["results"]:
        if row["passed"]:
            continue
        lines.append(f"- `{row['suite']}/{row['id']}` (repeat {row['repeat']})")
        for turn in row["turns"]:
            if turn.get("error"):
                lines.append(f"  - {turn['error']}")
            for path, check in turn.get("checks", {}).items():
                if not check["passed"]:
                    lines.append(f"  - Turn {turn.get('turn')}, `{path}`: expected `{check['expected']}`, actual `{check['actual']}`")
    lines += ["", "## Limits", "", *[f"- {item}" for item in report["limitations"]], ""]
    (directory / "report.md").write_text("\n".join(lines), encoding="utf-8")


def write_feature_reports(report, directory):
    write_report(report, directory)
    index = ["# Kết quả theo tính năng", "", "| Tính năng | Bộ | Pass / Tổng | Report |", "|---|---|---:|---|"]
    for feature in FEATURES:
        rows = [row for row in report["results"] if row["feature"] == feature]
        if not rows:
            continue
        for suite in sorted({row["suite"] for row in rows}):
            selected = [row for row in rows if row["suite"] == suite]
            part = {**report, **summarize(selected)}
            (directory / feature / suite).mkdir(parents=True, exist_ok=True)
            (directory / feature / suite / "results.jsonl").write_text(
                "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in selected), encoding="utf-8")
            write_report(part, directory / feature / suite)
            index.append(f"| {feature} | {suite} | {part['passed']}/{part['cases']} | [Report]({feature}/{suite}/report.md) |")
    (directory / "index.md").write_text("\n".join(index) + "\n", encoding="utf-8")


async def run_both(args, selected):
    variants = [argparse.Namespace(**(vars(args) | {"provider": provider, "output": args.output / name}))
                for provider, name in (("local", "jetson"), ("google", "gemini"))]
    return await asyncio.gather(*(run(variant, selected) for variant in variants))


def write_comparison(reports, directory):
    directory.mkdir(parents=True, exist_ok=True)
    names = ("jetson", "gemini")
    lookup = {}
    for name, report in zip(names, reports, strict=True):
        write_feature_reports(report, directory / name)
        lookup[name] = {(r["suite"], r["id"], r["repeat"]): r for r in report["results"]}
    if lookup["jetson"].keys() != lookup["gemini"].keys():
        raise ValueError("Comparison requires exactly the same cases and repeats")
    rows = []
    for key, left in lookup["jetson"].items():
        right = lookup["gemini"][key]
        rows.append({"suite": key[0], "id": key[1], "repeat": key[2], "feature": left["feature"],
                     "jetson_passed": left["passed"], "gemini_passed": right["passed"]})
    comparison = {"cases_per_agent": len(rows), "agents": {
        name: {k: report[k] for k in ("provider", "model", "passed", "cases", "model_latency", "repairs", "fallbacks", "service_errors")}
        for name, report in zip(names, reports, strict=True)}, "results": rows}
    (directory / "comparison.json").write_text(json.dumps(comparison, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    lines = ["# Jetson và Gemini", "", "Cùng input và oracle nghiệp vụ; raw output nằm trong thư mục mỗi agent.", "",
             "| Tính năng | Ca mỗi agent | Jetson pass | Gemini pass |", "|---|---:|---:|---:|"]
    for feature in FEATURES:
        selected = [r for r in rows if r["feature"] == feature]
        if selected:
            lines.append(f"| {feature} | {len(selected)} | {sum(r['jetson_passed'] for r in selected)} | {sum(r['gemini_passed'] for r in selected)} |")
    lines += ["", "[Chi tiết Jetson](jetson/index.md) · [Chi tiết Gemini](gemini/index.md)", ""]
    (directory / "comparison.md").write_text("\n".join(lines), encoding="utf-8")
    return comparison


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("fixture", "live"), default="fixture")
    parser.add_argument("--suite", choices=("all", "tools", "workflows", "language"), default="all")
    parser.add_argument("--provider", choices=("local", "google", "openai", "openrouter", "both"), default="local")
    parser.add_argument("--transport", choices=("json_schema", "native"), default="json_schema")
    parser.add_argument("--case", default="*", help="Case ID glob")
    parser.add_argument("--group", help="Exact group name")
    parser.add_argument("--feature", choices=FEATURES, help="Run one feature/tool family")
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--concurrency", type=int, default=1, choices=range(1, 5), help="Isolated cases per agent, within one feature")
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--samples-per-feature", type=int, help="Limit each feature separately; keeps hard groups last")
    parser.add_argument("--validate-only", action="store_true")
    parser.add_argument("--quiet", action="store_true", help="Print only the final summary")
    parser.add_argument("--resume", action="store_true", help="Reuse completed cases with identical inputs; retry HTTP 429 cases")
    parser.add_argument("--gemini-delay", type=float, default=3, help="Seconds between Gemini cases; use concurrency 1 for pacing")
    parser.add_argument("--rate-limit-retries", type=int, default=2, choices=range(4), help="Fresh isolated retries after HTTP 429, 30 seconds apart")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.output is None:
        stamp = datetime.now(ZoneInfo("Asia/Ho_Chi_Minh")).strftime("%Y%m%d-%H%M%S-%f")
        args.output = ROOT / "results" / f"{stamp}-{args.mode}-{args.provider}"
    args.fingerprints = fingerprints()
    if args.repeats < 1 or (args.limit is not None and args.limit < 1) or (args.samples_per_feature is not None and args.samples_per_feature < 1):
        parser.error("repeats/limit must be positive")
    if args.mode == "fixture" and args.provider != "local":
        parser.error("Fixture tools use the local operations contract; --provider selects a live model")
    data = datasets()
    counts = {name: len(cases) for name, cases in data.items()}
    intended = {cap for cases in data.values() for case in cases for cap in covers(case)}
    inventory = {"suites": counts, "capabilities_with_authored_cases": sorted(intended),
                 "capabilities_without_authored_cases": sorted(TOOL_REGISTRY.keys() - intended - EXCLUDED_CAPABILITIES.keys()),
                 "excluded_capabilities": EXCLUDED_CAPABILITIES,
                 "excluded_model_tools": EXCLUDED_MODEL_TOOLS,
                 "features": {feature: {suite: sum(feature_of(c) == feature for c in cases)
                                         for suite, cases in data.items()} for feature in FEATURES}}
    if args.validate_only:
        print(json.dumps(inventory, ensure_ascii=False, indent=2))
        return
    suites = ["tools", "workflows"] if args.mode == "fixture" else ["tools", "language"]
    if args.suite != "all":
        if args.suite not in suites:
            parser.error("language requires live; tools require fixture or live/local; workflows require fixture")
        suites = [args.suite]
    selected = [(suite, case) for suite in suites for case in data[suite]
                if fnmatch.fnmatchcase(identity(case)[0], args.case) and (not args.group or identity(case)[1] == args.group)
                and (not args.feature or feature_of(case) == args.feature)]
    excluded = []
    if args.mode == "live":
        eligible = []
        for suite, case in selected:
            if suite == "tools" and case.group in {"tool_validation", "provider_failure"}:
                excluded.append({"id": case.id, "reason": "Authored invalid outputs/outages are fixture-only"})
            elif suite == "language" and case.local_only and args.provider != "local":
                excluded.append({"id": case.id, "reason": "Raw local tool-call oracle; cloud uses SemanticTurn"})
            else:
                eligible.append((suite, case))
        selected = eligible
    # Features run in a fixed curriculum; hard/compound/adversarial cases last.
    rng = random.Random(args.seed)
    ordered = []
    for feature in FEATURES:
        bucket = [(suite, case) for suite, case in selected if feature_of(case) == feature]
        rng.shuffle(bucket)
        if args.samples_per_feature:
            bucket = bucket[:args.samples_per_feature]
        ordered.extend(bucket)
    selected = ordered
    if args.limit:
        selected = selected[:args.limit]
    if not selected:
        parser.error("No cases matched")
    if args.provider == "both":
        reports = asyncio.run(run_both(args, selected))
        comparison = write_comparison(reports, args.output)
        print(json.dumps({k: comparison[k] for k in ("cases_per_agent", "agents")}, ensure_ascii=False, indent=2))
        raise SystemExit(0 if all(r["passed"] == r["cases"] and not r["inputs_changed_during_run"] for r in reports) else 1)
    if args.mode == "fixture":
        with local_network_only():
            report = asyncio.run(run(args, selected))
    else:
        report = asyncio.run(run(args, selected))
    report["coverage_inventory"] = inventory
    report["excluded"] = excluded
    report["not_run_suites"] = sorted(set(data) - set(suites))
    write_feature_reports(report, args.output)
    print(json.dumps({k: report[k] for k in ("mode", "cases", "passed", "summary", "failed_checks")}, ensure_ascii=False, indent=2))
    raise SystemExit(0 if report["passed"] == report["cases"] and not report["inputs_changed_during_run"] else 1)


if __name__ == "__main__":
    main()
