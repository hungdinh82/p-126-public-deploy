# Local VF8 RAG benchmark

Split: `dev`; cases: 55; provider: `rules`.

Labels are a draft. Fact coverage is an automatic keyword proxy; semantic correctness needs review.
Timing is measured on the execution host, **not AGX Xavier**.

Intent route accuracy: 1; macro F1: 1.0.
Oracle-evidence fact coverage: 0.8254.

| Retriever | Recall@k | MRR@k | p50 ms | p95 ms | Fact coverage | Negative abstention |
|---|---:|---:|---:|---:|---:|---:|
| hybrid | 0.9762 | 0.7143 | 15.79 | 31.6 | 0.5992 | 1 |

## Intent failures


## Retrieval misses

- hybrid: vf8-battery_low-1

## Limitations

- Draft labels await human review
- Keyword fact coverage is a proxy, not semantic entailment
- Timing is this host, not AGX Xavier
- Single pass; no confidence intervals
- Source relevance is at original chunk granularity
- No action is executed by this benchmark
