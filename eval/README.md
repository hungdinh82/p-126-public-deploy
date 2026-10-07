# VF8 local RAG evaluation

This directory separates **intent**, **retrieval**, and **answer generation**.
All inference is local. No cloud embedding, generation, or judge is used.

## Artifacts

- `golden_dataset.jsonl`: 74 initial cases, with no conversational follow-up.
- `golden_dataset.meta.json`: source hash and dataset provenance.
- `build_golden.py`: authored seed questions, literal source/fact validation.
- `benchmark.py`: runner and per-case JSON/Markdown reports.
- `results/local-dev.*` and `results/local-test.*`: measured rules/extractive baselines.

Every label is **draft**, awaiting user review. The dataset is deliberately small:
it establishes a repeatable baseline, not a claim of production accuracy. Source
content is copied into reference evidence for review; it is never included in the
retrieval query. Two questions about the same source stay in the same split.
The current split contains 55 development and 19 test cases.

## Prepare the local model and index

See [offline RAG setup](../docs/rag_local.md). The downloader accesses the public
model repository only during provisioning. The index builder and runtime do not
download models or send handbook content to an external API. Default input paths
are `data/handbooks/handbook-source.sqlite3` for original evidence and
`data/handbooks/handbook.sqlite3` for the runtime vector index. The initial reports
record the former `handbook-local.sqlite3` filename; that exact artifact was
promoted to `handbook.sqlite3` without changing its contents or hash.

## Run the three measurements

```bash
# Tune on development, then run the held-out split without tuning on its errors.
.venv/bin/python -m eval.benchmark --split dev --output eval/results/local-dev.json
.venv/bin/python -m eval.benchmark --split test --output eval/results/local-test.json

# Evaluate a running self-hosted SLM. Supply the model ID exposed by /v1/models.
.venv/bin/python -m eval.benchmark --provider local \
  --base-url http://127.0.0.1:1234/v1 --model YOUR_LOCAL_MODEL \
  --split dev --output eval/results/slm-dev.json
```

The runner guards TCP connections to allow only loopback, so it can contact a
local SLM but cannot contact a cloud LLM. It never executes vehicle actions.
Missing models, unavailable endpoints and other execution errors are written to
the report and cause a nonzero exit. Low metric values are reported, not hidden.

### Intent

Route accuracy, exact intent accuracy, route macro F1 and latency are measured on
all cases. Include both knowledge questions and neighboring action, clarification,
conversation and unsupported intents to expose accidental command routing.

### Retrieval

Compare legacy SQLite FTS5 (`fts`), FTS5 on the same token-budget chunks as the
new index (`fts_local`), local vector-only, and independent FTS5/vector RRF.
The same-chunk baseline separates chunking effects from retrieval changes.
Recall@k counts recovered relevant original source chunks. MRR@k measures the first
relevant rank. Token-budget subchunks map back to their original source ID, so the
same references work across chunking configurations. This is original-chunk
relevance, not an exact-span retrieval metric.

Scope-rejected questions return no evidence. Answerable questions that were
incorrectly scope-rejected still count as retrieval misses. Router output does not
control this measurement: the reference route determines the retrieval test set.

### Answer generation

`answer_with_oracle_evidence` gives the generator the reference sources to isolate
generation errors. `answer_with_retrieved_evidence` measures the retrieval/generation
combination independently of routing.

Automatic checks report required-fact keyword coverage, citation agreement with
reference sources, citation ID validity, and abstention on unanswerable questions.
**Keyword coverage and valid citations do not prove semantic entailment or
preservation of negation.** Inspect per-case answers and correct the draft labels.
The extractive baseline currently fails unanswerable cases; that failure is kept
in the report. An oracle score of 1.0 for extractive answers is expected and does
not establish SLM quality.

## Edit and maintain the golden dataset

Edit JSONL directly. For each answerable case preserve:

- `relevant_source_ids`: original source IDs containing the answer.
- `reference_evidence`: literal quote, source URL, section, content checksum.
- `required_facts`: groups of acceptable aliases; every group must match.
- `reference_answer`: reviewer-readable reference, currently the source excerpt.
- `review_status`: change from `draft` to `reviewed` after checking the case.

For multi-source questions list all required sources; Recall@k then tests all of
them. For unanswerable questions set `answerable=false` and no reference sources.
Do not put paraphrases or the same reference source in both dev and test. Add
new cases and dataset versions rather than rewriting past results.

The loader rejects duplicate IDs, stale sources/checksums, unsupported expected
facts, and source leakage across splits. Regeneration refuses to overwrite an
existing dataset unless `--overwrite` is explicitly passed.

## Measurement limits

Reports record dataset/index hashes, model revision and file checksums, software
versions, thread limit, hardware, peak process RSS and first-query cold loading.
Warm latency uses one pass and nearest-rank p95. Current timing is from the dev
host, not AGX Xavier. Repeat on Xavier with STT/TTS/SLM loaded, including cold start,
concurrent load, and the memory/power mode actually used in deployment.

## Conversation regressions

`voice_cases.jsonl` contains recent short utterances and authored tool-boundary
cases. Run `python -m eval.voice_benchmark` for local full-graph routing and reply
length checks against an in-memory car. See [voice behavior](../docs/voice_assistant.md).
`results/voice-dev.json` and `results/voice-rag-dev.*` record the updated behavior.
The original reports are historical; their extractive oracle coverage is not the
current selector's score. No held-out labels were edited for this change.
