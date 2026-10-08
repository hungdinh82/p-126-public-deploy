# Local VF8 RAG benchmark

Split: `test`; cases: 19; provider: `rules`.

Labels are a draft. Fact coverage is an automatic keyword proxy; semantic correctness needs review.
Timing is measured on the execution host, **not AGX Xavier**.

Intent route accuracy: 1; macro F1: 1.0.
Oracle-evidence fact coverage: 0.6429.

| Retriever | Recall@k | MRR@k | p50 ms | p95 ms | Fact coverage | Negative abstention |
|---|---:|---:|---:|---:|---:|---:|
| hybrid | 0.9286 | 0.7679 | 14.74 | 129.05 | 0.5 | 1 |

## Intent failures


## Retrieval misses

- hybrid: vf8-starting_key-2

## Limitations

- Draft labels await human review
- Keyword fact coverage is a proxy, not semantic entailment
- Timing is this host, not AGX Xavier
- Single pass; no confidence intervals
- Source relevance is at original chunk granularity
- No action is executed by this benchmark
