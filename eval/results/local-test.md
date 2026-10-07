# Local VF8 RAG benchmark

Split: `test`; cases: 19; provider: `rules`.

Labels are a draft. Fact coverage is an automatic keyword proxy; semantic correctness needs review.
Timing is measured on the execution host, **not AGX Xavier**.

Intent route accuracy: 0.8947; macro F1: 0.8571.
Oracle-evidence fact coverage: 1.0.

| Retriever | Recall@k | MRR@k | p50 ms | p95 ms | Fact coverage | Negative abstention |
|---|---:|---:|---:|---:|---:|---:|
| fts | 0.7857 | 0.5643 | 2.94 | 5.31 | 0.5357 | 0 |
| fts_local | 0.7857 | 0.5524 | 3.96 | 5.54 | 0.5357 | 0 |
| vector | 0.7143 | 0.6429 | 6.98 | 37.4 | 0.75 | 0 |
| hybrid | 0.8571 | 0.6488 | 14.84 | 68.03 | 0.6429 | 0 |

## Intent failures

- `vf8-rear_window_lock-1`: Công tắc khóa cửa sổ điện dùng để làm gì? → `clarify/conversation.clarify`; expected `handbook/manual.search`
- `vf8-mirror_control-2`: Muốn chỉnh gương bên thì dùng màn hình và nút nào? → `clarify/conversation.clarify`; expected `handbook/manual.search`

## Retrieval misses

- fts: vf8-window_control-2, vf8-rear_window_lock-2, vf8-battery_12v-2
- fts_local: vf8-window_control-2, vf8-rear_window_lock-2, vf8-battery_12v-2
- vector: vf8-window_control-1, vf8-window_control-2, vf8-child_seat-2, vf8-battery_12v-2
- hybrid: vf8-window_control-2, vf8-battery_12v-2

## Limitations

- Draft labels await human review
- Keyword fact coverage is a proxy, not semantic entailment
- Timing is this host, not AGX Xavier
- Single pass; no confidence intervals
- Source relevance is at original chunk granularity
- No action is executed by this benchmark
