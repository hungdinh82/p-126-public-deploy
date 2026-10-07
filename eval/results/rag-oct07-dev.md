# Local VF8 RAG benchmark

Split: `dev`; cases: 55; provider: `rules`.

Labels are a draft. Fact coverage is an automatic keyword proxy; semantic correctness needs review.
Timing is measured on the execution host, **not AGX Xavier**.

Intent route accuracy: 1; macro F1: 1.0.
Oracle-evidence fact coverage: 0.8492.

| Retriever | Recall@k | MRR@k | p50 ms | p95 ms | Fact coverage | Negative abstention |
|---|---:|---:|---:|---:|---:|---:|
| fts | 0.9048 | 0.6583 | 3.4 | 4.91 | 0.5873 | 1 |
| fts_local | 0.9048 | 0.6571 | 3.72 | 5.54 | 0.5873 | 1 |
| vector | 0.8333 | 0.6972 | 47.05 | 65.06 | 0.623 | 1 |
| hybrid | 0.9524 | 0.723 | 61.83 | 148.28 | 0.5992 | 1 |

## Intent failures


## Retrieval misses

- fts: vf8-window_pinch-2, vf8-charge_equipment-2, vf8-battery_low-1, vf8-child_rear-2
- fts_local: vf8-window_pinch-2, vf8-charge_equipment-2, vf8-battery_low-1, vf8-child_rear-2
- vector: vf8-battery_low-1, vf8-child_rear-1, vf8-length-1, vf8-length-2, vf8-wheelbase-1, vf8-wheelbase-2, vf8-spare_tire-1
- hybrid: vf8-battery_low-1, vf8-child_rear-1

## Limitations

- Draft labels await human review
- Keyword fact coverage is a proxy, not semantic entailment
- Timing is this host, not AGX Xavier
- Single pass; no confidence intervals
- Source relevance is at original chunk granularity
- No action is executed by this benchmark
