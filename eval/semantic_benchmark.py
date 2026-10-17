"""Opt-in evaluation of a real interpreter, with no vehicle or map effects."""

import argparse
import asyncio
import hashlib
import json
import statistics
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


def inference_breakdown(rows):
    """Use every response once; gateway completion repeats the final response."""
    responses = []
    for row in rows:
        for attempt in row["attempts"]:
            proposals = attempt.get("proposal_attempts")
            candidates = [p.get("completion") for p in proposals] if proposals else [attempt.get("completion")]
            responses.extend(c for c in candidates if c and c.get("prompt_tokens"))
    if not responses:
        return {"responses_with_metadata": 0}
    prompt_total = sum(c["prompt_tokens"] for c in responses)
    return {
        "responses_with_metadata": len(responses),
        "prompt_tokens_median": statistics.median(c["prompt_tokens"] for c in responses),
        "output_tokens_median": statistics.median(c.get("completion_tokens", 0) for c in responses),
        "output_tokens_total": sum(c.get("completion_tokens", 0) for c in responses),
        "cached_prompt_fraction": round(sum(c.get("cached_tokens") or 0 for c in responses) / prompt_total, 4),
        "prefill_ms_total": round(sum((c.get("timings") or {}).get("prompt_ms", 0) for c in responses), 2),
        "decode_ms_total": round(sum((c.get("timings") or {}).get("predicted_ms", 0) for c in responses), 2),
    }


async def evaluate(args):
    config = Settings()
    overrides = {}
    if getattr(args, "contract", None):
        overrides["local_semantic_contract"] = args.contract
    if getattr(args, "transport", None):
        overrides["local_semantic_transport"] = args.transport
    config = config.model_copy(update=overrides)
    generator = GENERATORS[args.provider](config)
    client = generator.client
    client.max_attempts = 1
    client.max_tokens = (
        min(config.planner_max_tokens, config.local_llm_max_tokens)
        if args.provider == "local"
        else config.planner_max_tokens
    )
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
        passed = provider == args.provider and all(c["passed"] for c in assertions.values())
        rows.append(
            {
                "id": case["id"],
                "repeat": repeat + 1,
                "passed": passed,
                "passed_first_call": passed
                and sum(a.get("calls", 0) for a in attempts) <= 1
                and all(a.get("semantic_validation", "accepted") == "accepted" for a in attempts),
                "semantic": semantic.model_dump(mode="json", exclude_none=True, exclude_defaults=True),
                "model_served": provider == args.provider,
                "checks": assertions,
                "attempts": attempts,
            }
        )
    if not rows:
        raise ValueError("Semantic dataset is empty")
    from src.vivi.dialogue.local_operations import CONTRACT_VERSION, INSTRUCTION

    operations = args.provider == "local" and getattr(client, "local_semantic_contract", "legacy") == "operations_v1"
    return {
        **metadata(),
        "profile": "live_semantic",
        "semantic_representation": "sparse_domain_projection; raw model calls are in attempts[].proposal_attempts[].calls",
        "prompt_version": CONTRACT_VERSION if operations else PROMPT_VERSION,
        "local_semantic_contract": "operations_v1" if operations else "legacy" if args.provider == "local" else None,
        "semantic_instruction_sha256": hashlib.sha256(
            (INSTRUCTION if operations else semantic_system_prompt(local=args.provider == "local")).encode()
        ).hexdigest(),
        "semantic_schema_sha256": hashlib.sha256(json.dumps(semantic_tools(), sort_keys=True).encode()).hexdigest()
        if args.provider == "local" and not operations
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
        "passed_first_call": sum(r["passed_first_call"] for r in rows),
        "repairs": sum(a.get("repairs", 0) for r in rows for a in r["attempts"]),
        "discoveries": sum(bool(a.get("discovery")) for r in rows for a in r["attempts"]),
        "model_latency": latency([sum(a.get("latency_ms", 0) for a in r["attempts"]) for r in rows]),
        "inference_breakdown": inference_breakdown(rows),
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
    parser.add_argument("--contract", choices=("legacy", "operations_v1"))
    parser.add_argument("--transport", choices=("json_schema", "native"))
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
