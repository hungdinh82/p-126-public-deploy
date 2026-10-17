"""Opt-in evaluation of a real interpreter, with no vehicle or map effects."""

import argparse
import asyncio
import hashlib
import json
from pathlib import Path

from eval.workflow_benchmark import ROOT, checks, latency, metadata
from src.vivi.capabilities.registry import catalog
from src.vivi.config import Settings
from src.vivi.dialogue.contracts import SessionState
from src.vivi.dialogue.planner import PROMPT_VERSION, SemanticResolver, semantic_system_prompt, semantic_tools
from src.vivi.model_gateway import ModelGateway, ModelProvider
from src.vivi.rag.generator import (
    GoogleHandbookGenerator,
    LocalHandbookGenerator,
    OpenAIHandbookGenerator,
    OpenRouterHandbookGenerator,
)

GENERATORS = {
    "google": GoogleHandbookGenerator,
    "local": LocalHandbookGenerator,
    "openai": OpenAIHandbookGenerator,
    "openrouter": OpenRouterHandbookGenerator,
}


async def evaluate(args):
    config = Settings()
    generator = GENERATORS[args.provider](config)
    client = generator.client
    client.max_attempts, client.max_tokens = 1, config.planner_max_tokens
    provider_budget = config.local_budget_seconds if args.provider == "local" else config.cloud_budget_seconds
    gateway = ModelGateway(
        {args.provider: ModelProvider(args.provider, client, provider_budget)},
        primary=args.provider,
        fallback_chains={args.provider: [args.provider]},
        breaker_seconds=0,
    )
    resolver = SemanticResolver(gateway=gateway, budget_seconds=config.semantic_budget_seconds)
    rows = []
    # This dataset is deliberately separate from authored workflow replies.
    cases = [json.loads(line) for line in args.dataset.read_text().splitlines() if line.strip()]
    for repeat, case in ((repeat, case) for repeat in range(getattr(args, "repeats", 1)) for case in cases):
        semantic, provider, attempts = await resolver.resolve(
            case["text"],
            SessionState.model_validate(case.get("session", {})),
            requested=args.provider,
            history=case.get("history", []),
            catalog=catalog(navigation=True),
        )
        actual = semantic.model_dump(mode="json")
        actual["blocked"] = semantic.act in {"clarify", "unsupported"} or bool(
            semantic.negated
            or semantic.conditional
            or semantic.scheduled
            or semantic.conflicting
            or semantic.unresolved
        )
        assertions = checks(case["expect"], actual)
        rows.append(
            {
                "id": case["id"],
                "repeat": repeat + 1,
                "passed": provider == args.provider and all(c["passed"] for c in assertions.values()),
                "semantic": semantic.model_dump(mode="json"),
                "model_served": provider == args.provider,
                "checks": assertions,
                "attempts": attempts,
            }
        )
    if not rows:
        raise ValueError("Semantic dataset is empty")
    return {
        **metadata(),
        "profile": "live_semantic",
        "prompt_version": PROMPT_VERSION,
        "semantic_instruction_sha256": hashlib.sha256(
            semantic_system_prompt(local=args.provider == "local").encode()
        ).hexdigest(),
        "semantic_schema_sha256": hashlib.sha256(json.dumps(semantic_tools(), sort_keys=True).encode()).hexdigest()
        if args.provider == "local"
        else None,
        "transport": "json_schema"
        if getattr(client, "tools_as_json", False)
        else "native"
        if args.provider == "local"
        else "json",
        "provider_budget_seconds": provider_budget,
        "semantic_budget_seconds": config.semantic_budget_seconds,
        "provider": args.provider,
        "model": getattr(client, "model", args.provider),
        "dataset_sha256": hashlib.sha256(args.dataset.read_bytes()).hexdigest(),
        "cases": len(rows),
        "passed": sum(r["passed"] for r in rows),
        "fallbacks": sum(not r["model_served"] for r in rows),
        "model_latency": latency([sum(a.get("latency_ms", 0) for a in r["attempts"]) for r in rows]),
        "limitations": [
            "Authored language cases; a separately reviewed held-out corpus is still required.",
            "Measures interpreter only; no map, vehicle, speech or response-generation evaluation.",
        ],
        "results": rows,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider", choices=tuple(GENERATORS), required=True)
    parser.add_argument("--dataset", type=Path, default=ROOT / "eval/semantic_cases.jsonl")
    parser.add_argument("--output", type=Path, default=ROOT / "eval/results/semantic-v2.json")
    parser.add_argument("--repeats", type=int, default=1)
    args = parser.parse_args()
    if args.repeats < 1:
        parser.error("--repeats must be positive")
    report = asyncio.run(evaluate(args))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({k: v for k, v in report.items() if k != "results"}, ensure_ascii=False, indent=2))
    raise SystemExit(0 if report["passed"] == report["cases"] else 1)


if __name__ == "__main__":
    main()
