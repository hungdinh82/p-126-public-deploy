from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import math
import platform
import re
import resource
import socket
import statistics
import time
import unicodedata
from contextlib import contextmanager
from importlib.metadata import version
from pathlib import Path

from src.vivi.agents.classifier import LocalIntentClassifier, RulesIntentClassifier
from src.vivi.config import Settings
from src.vivi.rag.embeddings.local import LocalE5EmbeddingProvider
from src.vivi.rag.generator import ExtractiveHandbookGenerator, LocalHandbookGenerator, validate_grounding
from src.vivi.rag.scope import scope_rejection_reason
from src.vivi.rag.sqlite_store import SQLiteHandbookRetriever, SQLiteHandbookStore
from src.vivi.rag.sqlite_vector import SQLiteVectorRetriever


def root_id(source_id: str) -> str:
    return re.sub(r"-p\d+$", "", source_id)


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFD", text.casefold())
    text = "".join(char for char in text if unicodedata.category(char) != "Mn").replace("đ", "d")
    return " ".join(re.findall(r"\w+", text))


def fact_coverage(text: str, facts: list[list[str]]) -> float | None:
    if not facts:
        return None
    value = normalize(text)
    return sum(any(normalize(alias) in value for alias in aliases) for aliases in facts) / len(facts)


def load_dataset(path: Path, source: Path) -> list[dict]:
    cases = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    identifiers = set()
    source_splits = {}
    store = SQLiteHandbookStore(source, read_only=True)
    with store._connect() as connection:
        originals = {row["source_id"]: row for row in connection.execute("SELECT * FROM handbook_chunks")}
    for case in cases:
        if case["id"] in identifiers:
            raise ValueError(f"Duplicate dataset id: {case['id']}")
        identifiers.add(case["id"])
        if case["split"] not in {"dev", "test"}:
            raise ValueError("Each case needs a dev or test split")
        if case["answerable"] and (not case["relevant_source_ids"] or not case["required_facts"]):
            raise ValueError(f"Answerable case lacks source/facts: {case['id']}")
        references = {item["source_id"]: item for item in case["reference_evidence"]}
        if set(references) != set(case["relevant_source_ids"]):
            raise ValueError(f"Reference source lists disagree: {case['id']}")
        for source_id, reference in references.items():
            row = originals.get(source_id)
            if row is None or reference["checksum"] != row["checksum"] or reference["quote"] not in row["content"]:
                raise ValueError(f"Stale/invalid reference evidence: {case['id']}/{source_id}")
            previous = source_splits.setdefault(source_id, case["split"])
            if previous != case["split"]:
                raise ValueError(f"Source leaked across dev/test: {source_id}")
        evidence = " ".join(item["quote"] for item in references.values())
        if case["answerable"] and fact_coverage(evidence, case["required_facts"]) != 1:
            raise ValueError(f"Expected facts not supported by reference: {case['id']}")
    if not cases:
        raise ValueError("Dataset is empty")
    return cases


@contextmanager
def local_network_only():
    """Fail if any component attempts a non-loopback TCP connection."""
    original = socket.socket.connect

    def connect(sock, address):
        if isinstance(address, tuple):
            host = address[0]
            allowed = host == "localhost"
            if not allowed:
                try:
                    allowed = ipaddress.ip_address(host).is_loopback
                except ValueError:
                    allowed = False
            if not allowed:
                raise RuntimeError(f"Offline benchmark blocked non-local connection: {host}")
        return original(sock, address)

    socket.socket.connect = connect
    try:
        yield
    finally:
        socket.socket.connect = original


def percentiles(values: list[float]) -> dict:
    if not values:
        return {"n": 0, "p50_ms": None, "p95_ms": None}
    ordered = sorted(values)
    return {"n": len(values), "p50_ms": round(statistics.median(values), 2),
            "p95_ms": round(ordered[max(0, math.ceil(len(values) * 0.95) - 1)], 2)}


def mean(values) -> float | None:
    values = [value for value in values if value is not None]
    return round(statistics.mean(values), 4) if values else None


def answer_result(generator, query: str, chunks: list, case: dict) -> dict:
    started = time.perf_counter()
    if not chunks:
        return {"abstained": True, "answer": "Không đủ bằng chứng.", "citation_valid": True,
                "reference_fact_coverage": 0.0 if case["answerable"] else None,
                "citation_reference_precision": None, "latency_ms": 0.0, "error": None}
    try:
        answer = generator.generate(query, chunks, [])
        valid, reason = validate_grounding(answer, chunks)
        cited = [root_id(citation.source_id) for citation in answer.citations]
        precision = (sum(source_id in case["relevant_source_ids"] for source_id in cited) / len(cited)
                     if cited and case["answerable"] else None)
        return {
            "abstained": not valid, "answer": answer.answer if valid else reason,
            "citation_valid": valid, "citations": [citation.model_dump() for citation in answer.citations],
            "reference_fact_coverage": fact_coverage(answer.answer if valid else "", case["required_facts"]),
            "citation_reference_precision": precision, "error": None,
            "latency_ms": round((time.perf_counter() - started) * 1000, 2),
        }
    except Exception as exc:
        return {"abstained": True, "answer": "", "citation_valid": False,
                "reference_fact_coverage": 0.0 if case["answerable"] else None,
                "citation_reference_precision": None,
                "latency_ms": round((time.perf_counter() - started) * 1000, 2), "error": str(exc)}


def route_macro_f1(rows: list[dict]) -> float:
    labels = {row["expected_route"] for row in rows}
    values = []
    for label in labels:
        true_positive = sum(row["expected_route"] == label and row["route"] == label for row in rows)
        false_positive = sum(row["expected_route"] != label and row["route"] == label for row in rows)
        false_negative = sum(row["expected_route"] == label and row["route"] != label for row in rows)
        denominator = 2 * true_positive + false_positive + false_negative
        values.append(2 * true_positive / denominator if denominator else 0)
    return round(statistics.mean(values), 4)


def run(args) -> dict:
    cases = load_dataset(args.dataset, args.source)
    cases = [case for case in cases if args.split == "all" or case["split"] == args.split]
    if not cases:
        raise ValueError("Selected split is empty")
    config = Settings(llm_provider=args.provider, rag_local_only=True,
                      local_llm_model=args.model or "", local_llm_base_url=args.base_url,
                      llm_timeout_seconds=args.timeout)
    classifier = LocalIntentClassifier(config) if args.provider == "local" else RulesIntentClassifier()
    generator = LocalHandbookGenerator(config) if args.provider == "local" else ExtractiveHandbookGenerator()
    embeddings = None
    model_load_ms = None
    if args.retriever not in {"fts", "fts_local"}:
        started = time.perf_counter()
        embeddings = LocalE5EmbeddingProvider(args.model_dir, threads=args.threads)
        embeddings.embed_query("Khởi động embedding")
        model_load_ms = round((time.perf_counter() - started) * 1000, 2)
    modes = ["fts", "fts_local", "vector", "hybrid"] if args.retriever == "all" else [args.retriever]
    source_store = SQLiteHandbookStore(args.source, read_only=True)
    with source_store._connect() as connection:
        oracle = {row["source_id"]: source_store._to_retrieved(row) for row in
                  connection.execute("SELECT *, 0 AS rank FROM handbook_chunks")}
    intent_rows = []
    for case in cases:
        started = time.perf_counter()
        try:
            decision = classifier.classify_with_context(case["query"], [], None)
            route, intent, error = decision.route, decision.intent, None
        except Exception as exc:
            route, intent, error = "error", "error", str(exc)
        intent_rows.append({"id": case["id"], "query": case["query"], "expected_route": case["expected_route"],
                            "route": route, "expected_intent": case["expected_intent"], "intent": intent,
                            "latency_ms": round((time.perf_counter() - started) * 1000, 2), "error": error})
    # Oracle evidence isolates answer generation from retrieval/router mistakes.
    oracle_rows = []
    for case in cases:
        if case["answerable"]:
            result = answer_result(generator, case["query"], [oracle[source] for source in case["relevant_source_ids"]], case)
            oracle_rows.append({"id": case["id"], **result})
    retrieval_reports = {}
    for mode in modes:
        retriever = (SQLiteHandbookRetriever(args.source if mode == "fts" else args.database, final_k=args.k)
                     if mode in {"fts", "fts_local"} else
                     SQLiteVectorRetriever(args.database, embeddings, mode=mode, final_k=args.k,
                                           retrieval_k=max(12, args.k), min_similarity=args.min_similarity))
        rows = []
        for case in cases:
            if case["expected_route"] != "handbook":
                continue
            started = time.perf_counter()
            error = None
            try:
                chunks = ([] if scope_rejection_reason(case["query"]) else
                          retriever.retrieve(case["query"], case["vehicle_model"], case["model_year"], case["locale"]))
            except Exception as exc:
                chunks, error = [], str(exc)
            latency = round((time.perf_counter() - started) * 1000, 2)
            sources = [root_id(chunk.source_id) for chunk in chunks]
            relevant = set(case["relevant_source_ids"])
            hits = set(sources) & relevant
            rank = next((i for i, source in enumerate(sources, 1) if source in relevant), None)
            result = answer_result(generator, case["query"], chunks, case)
            rows.append({
                "id": case["id"], "query": case["query"], "answerable": case["answerable"],
                "retrieved_source_ids": [chunk.source_id for chunk in chunks],
                "semantic_similarities": [round(1 - chunk.semantic_distance, 4) for chunk in chunks],
                "recall_at_k": len(hits) / len(relevant) if relevant else None,
                "reciprocal_rank": 1 / rank if rank else (0.0 if relevant else None),
                "latency_ms": latency, "error": error, "answer": result,
            })
        negatives = [row for row in rows if not row["answerable"]]
        retrieval_reports[mode] = {
            "recall_at_k": mean(row["recall_at_k"] for row in rows),
            "mrr_at_k": mean(row["reciprocal_rank"] for row in rows),
            "latency": percentiles([row["latency_ms"] for row in rows]),
            "answer_with_retrieved_evidence": {
                "reference_fact_coverage": mean(row["answer"]["reference_fact_coverage"] for row in rows),
                "citation_reference_precision": mean(row["answer"]["citation_reference_precision"] for row in rows),
                "unanswerable_abstention_rate": mean(row["answer"]["abstained"] for row in negatives),
                "latency": percentiles([row["answer"]["latency_ms"] for row in rows]),
            },
            "errors": sum(bool(row["error"] or row["answer"]["error"]) for row in rows), "cases": rows,
        }
    return {
        "schema_version": 1, "dataset_sha256": hashlib.sha256(args.dataset.read_bytes()).hexdigest(),
        "source_sha256": hashlib.sha256(args.source.read_bytes()).hexdigest(),
        "split": args.split, "k": args.k, "cases": len(cases), "provider": args.provider,
        "generation_model": args.model if args.provider == "local" else "extractive_baseline",
        "network_policy": "tcp_loopback_only", "hardware": {"platform": platform.platform(), "cpu": platform.processor()},
        "embedding": {"model": embeddings.manifest if embeddings else None, "threads": args.threads,
                      "cold_load_and_first_query_ms": model_load_ms},
        "runtime_versions": {
            "python": platform.python_version(),
            **{name: version(name)
               for name in ("numpy", "onnxruntime", "tokenizers") if embeddings},
            **({"onnxruntime_imported": __import__("onnxruntime").__version__,
                "embedding_execution_providers": embeddings._session.get_providers()} if embeddings else {}),
        },
        "min_similarity": args.min_similarity,
        "database_sha256": hashlib.sha256(args.database.read_bytes()).hexdigest() if args.retriever != "fts" else None,
        "peak_process_rss_bytes": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * (1 if platform.system() == "Darwin" else 1024),
        "intent": {"route_accuracy": mean(row["route"] == row["expected_route"] for row in intent_rows),
                   "intent_accuracy": mean(row["intent"] == row["expected_intent"] for row in intent_rows),
                   "route_macro_f1": route_macro_f1(intent_rows),
                   "latency": percentiles([row["latency_ms"] for row in intent_rows]), "cases": intent_rows},
        "answer_with_oracle_evidence": {
            "reference_fact_coverage": mean(row["reference_fact_coverage"] for row in oracle_rows),
            "citation_reference_precision": mean(row["citation_reference_precision"] for row in oracle_rows),
            "latency": percentiles([row["latency_ms"] for row in oracle_rows]), "cases": oracle_rows,
        },
        "retrieval": retrieval_reports,
        "limitations": ["Draft labels await human review", "Keyword fact coverage is a proxy, not semantic entailment",
                        "Timing is this host, not AGX Xavier", "Single pass; no confidence intervals",
                        "Source relevance is at original chunk granularity", "No action is executed by this benchmark"],
    }


def markdown(report: dict) -> str:
    rows = ["# Local VF8 RAG benchmark", "", f"Split: `{report['split']}`; cases: {report['cases']}; provider: `{report['provider']}`.",
            "", "Labels are a draft. Fact coverage is an automatic keyword proxy; semantic correctness needs review.",
            "Timing is measured on the execution host, **not AGX Xavier**.", "",
            f"Intent route accuracy: {report['intent']['route_accuracy']}; macro F1: {report['intent']['route_macro_f1']}.",
            f"Oracle-evidence fact coverage: {report['answer_with_oracle_evidence']['reference_fact_coverage']}.", "",
            "| Retriever | Recall@k | MRR@k | p50 ms | p95 ms | Fact coverage | Negative abstention |",
            "|---|---:|---:|---:|---:|---:|---:|"]
    for mode, values in report["retrieval"].items():
        answer = values["answer_with_retrieved_evidence"]
        rows.append(f"| {mode} | {values['recall_at_k']} | {values['mrr_at_k']} | {values['latency']['p50_ms']} | "
                    f"{values['latency']['p95_ms']} | {answer['reference_fact_coverage']} | {answer['unanswerable_abstention_rate']} |")
    rows += ["", "## Intent failures", ""]
    for case in report["intent"]["cases"]:
        if case["route"] != case["expected_route"] or case["intent"] != case["expected_intent"]:
            rows.append(f"- `{case['id']}`: {case['query']} → `{case['route']}/{case['intent']}`; expected `{case['expected_route']}/{case['expected_intent']}`")
    rows += ["", "## Retrieval misses", ""]
    for mode, values in report["retrieval"].items():
        misses = [case["id"] for case in values["cases"] if case["recall_at_k"] == 0]
        rows.append(f"- {mode}: {', '.join(misses) or 'none'}")
    rows += ["", "## Limitations", "", *[f"- {item}" for item in report["limitations"]], ""]
    return "\n".join(rows)


def main() -> None:
    parser = argparse.ArgumentParser(description="Local-only intent, retrieval and oracle-answer evaluation")
    parser.add_argument("--dataset", type=Path, default=Path("eval/golden_dataset.jsonl"))
    parser.add_argument("--source", type=Path, default=Path("data/handbooks/handbook-source.sqlite3"))
    parser.add_argument("--database", type=Path, default=Path("data/handbooks/handbook.sqlite3"))
    parser.add_argument("--model-dir", type=Path, default=Path("models/multilingual-e5-small-int8"))
    parser.add_argument("--retriever", choices=("fts", "fts_local", "vector", "hybrid", "all"), default="all")
    parser.add_argument("--provider", choices=("rules", "local"), default="rules")
    parser.add_argument("--model", help="Required for --provider local; never calls cloud providers")
    parser.add_argument("--base-url", default="http://127.0.0.1:1234/v1")
    parser.add_argument("--timeout", type=float, default=15)
    parser.add_argument("--threads", type=int, default=2)
    parser.add_argument("--min-similarity", type=float, default=0)
    parser.add_argument("--k", type=int, default=5)
    parser.add_argument("--split", choices=("dev", "test", "all"), default="dev")
    parser.add_argument("--output", type=Path, default=Path("eval/results/local-dev.json"))
    args = parser.parse_args()
    if args.provider == "local" and not args.model:
        parser.error("--provider local requires --model")
    if not 1 <= args.k <= 50 or not 1 <= args.threads <= 8:
        parser.error("k must be 1..50 and threads must be 1..8")
    with local_network_only():
        report = run(args)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    args.output.with_suffix(".md").write_text(markdown(report), encoding="utf-8")
    print(markdown(report))
    errors = sum(bool(case["error"]) for case in report["intent"]["cases"])
    errors += sum(bool(case["error"]) for case in report["answer_with_oracle_evidence"]["cases"])
    errors += sum(value["errors"] for value in report["retrieval"].values())
    if errors:
        raise SystemExit(f"Benchmark recorded {errors} infrastructure/model errors; inspect the saved report")


if __name__ == "__main__":
    main()
