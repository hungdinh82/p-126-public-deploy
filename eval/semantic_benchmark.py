"""Opt-in evaluation of a real interpreter, with no vehicle or map effects."""

import argparse
import asyncio
import hashlib
import json
from pathlib import Path

from eval.workflow_benchmark import ROOT, checks, metadata
from src.vivi.capabilities.registry import catalog
from src.vivi.config import Settings
from src.vivi.dialogue.contracts import SessionState
from src.vivi.dialogue.planner import SEMANTIC_INSTRUCTION, SemanticResolver
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
    gateway = ModelGateway(
        {args.provider: ModelProvider(args.provider, client)},
        primary=args.provider,
        fallback_chains={args.provider: [args.provider]},
        breaker_seconds=0,
    )
    resolver = SemanticResolver(gateway=gateway)
    rows = []
    # This dataset is deliberately separate from authored workflow replies.
    for line in args.dataset.read_text().splitlines():
        if not line.strip():
            continue
        case = json.loads(line)
        semantic, provider, attempts = await resolver.resolve(
            case["text"], SessionState(), requested=args.provider, catalog=catalog(navigation=True)
        )
        assertions = checks(case["expect"], semantic.model_dump(mode="json"))
        rows.append(
            {
                "id": case["id"],
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
        "semantic_instruction_sha256": hashlib.sha256(SEMANTIC_INSTRUCTION.encode()).hexdigest(),
        "provider": args.provider,
        "model": getattr(client, "model", args.provider),
        "dataset_sha256": hashlib.sha256(args.dataset.read_bytes()).hexdigest(),
        "cases": len(rows),
        "passed": sum(r["passed"] for r in rows),
        "fallbacks": sum(not r["model_served"] for r in rows),
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
    args = parser.parse_args()
    report = asyncio.run(evaluate(args))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({k: v for k, v in report.items() if k != "results"}, ensure_ascii=False, indent=2))
    raise SystemExit(0 if report["passed"] == report["cases"] else 1)


if __name__ == "__main__":
    main()
