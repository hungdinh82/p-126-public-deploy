# Local VF8 RAG benchmark

Split: `dev`; cases: 55; provider: `rules`.

Labels are a draft. Fact coverage is an automatic keyword proxy; semantic correctness needs review.
Timing is measured on the execution host, **not AGX Xavier**.

Intent route accuracy: 1; macro F1: 1.0.
Oracle-evidence fact coverage: 0.8492.

| Retriever | Recall@k | MRR@k | p50 ms | p95 ms | Fact coverage | Negative abstention |
|---|---:|---:|---:|---:|---:|---:|
| fts | 0.9048 | 0.6583 | 5.08 | 7.42 | 0.5873 | 0 |
| fts_local | 0.9048 | 0.6571 | 3.53 | 5.29 | 0.5873 | 0 |
| vector | 0.8333 | 0.6972 | 7.9 | 21.05 | 0.623 | 0 |
| hybrid | 0.9524 | 0.723 | 11.84 | 27.75 | 0.5992 | 0 |

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
