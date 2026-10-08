# Local VF8 RAG benchmark

Split: `test`; cases: 19; provider: `rules`.

Labels are a draft. Fact coverage is an automatic keyword proxy; semantic correctness needs review.
Timing is measured on the execution host, **not AGX Xavier**.

Intent route accuracy: 0.9474; macro F1: 0.908.
Oracle-evidence fact coverage: 0.6786.

| Retriever | Recall@k | MRR@k | p50 ms | p95 ms | Fact coverage | Negative abstention |
|---|---:|---:|---:|---:|---:|---:|
| hybrid | 0.8571 | 0.6607 | 67.56 | 146.05 | 0.5 | 0 |

## Intent failures

- `vf8-mirror_control-2`: Muốn chỉnh gương bên thì dùng màn hình và nút nào? → `unsupported/unsupported.request`; expected `handbook/manual.search`

## Retrieval misses

- hybrid: vf8-window_control-2, vf8-battery_12v-2

## Limitations

- Draft labels await human review
- Keyword fact coverage is a proxy, not semantic entailment
- Timing is this host, not AGX Xavier
- Single pass; no confidence intervals
- Source relevance is at original chunk granularity
- No action is executed by this benchmark
