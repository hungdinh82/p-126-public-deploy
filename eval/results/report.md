# VF8 RAG evaluation reports

The measured local baselines are available in:

- [Development report](local-dev.md), with [per-case JSON](local-dev.json).
- [Held-out test report](local-test.md), with [per-case JSON](local-test.json).

Both use the draft [golden dataset](../golden_dataset.jsonl). Model weights and
handbook embeddings are local; there are no cloud inference or judge calls.

Intent and answer generation currently use rules/extractive baselines. The actual
self-hosted SLM and AGX Xavier hardware have not yet been benchmarked. Required-fact
coverage is a lexical proxy and does not prove semantic correctness. Unanswerable
cases expose incorrect answering by the extractive baseline.

See [runner instructions and limits](../README.md) to review labels, reproduce
results, or evaluate the actual local SLM.
