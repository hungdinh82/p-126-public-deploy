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

The October 7 audit adds 17 development cases from conversational failures and
temperature semantics, for 57 voice cases in total. `voice_benchmark.py` checks
optional `required_answer_terms`, `forbidden_answer_terms` and
`expected_value_celsius` in addition to route/intent. Reports are saved separately
as `results/voice-oct07.json`, `results/rag-oct07-dev.*` and
`results/rag-oct07-test.*`; historical results are preserved. The new test-split
report still has failures, detailed in [the conversation audit](../docs/voice_assistant.md).

## Memory regressions

`memory_cases.jsonl` has 29 authored development turns in ordered episodes.
Run `python -m eval.memory_benchmark` to verify persistence across sessions/restart,
correction, profile isolation, selective forgetting/reset and saved commands going
through confirmation and driving policy. Labels are draft and can be edited.
The runner uses temporary SQLite databases, rules and a simulated car; TCP is
disabled, and user memory is never modified. Reports include per-turn checks and
full-graph p50/p95 latency, excluding graph construction. These measurements are
from the development host, not AGX Xavier or a real speech/SLM workload.
See [memory design](../docs/memory.md) and `results/memory-dev.json`.

## Agents/RAG refactor regressions

`conversation_cases.jsonl` adds 12 situations / 18 turns including the latest log
failures, resolved referents, topic changes, pending slots and fresh observations.
The voice runner accepts `turns` arrays and checks route/intent at every turn, plus
optional `expected_status`, answer terms and temperature arguments.

```bash
.venv/bin/python -m eval.voice_benchmark --dataset eval/conversation_cases.jsonl \
  --output eval/results/conversation-refactor-dev.json
```

Reports `voice-refactor-dev.json`, `conversation-refactor-dev.json` and
`rag-refactor-{dev,test}.*` are the current snapshot. Earlier reports remain
baselines. The test split was inspected while fixing implementation regressions;
it has not been relabelled, but current results are **not an unseen evaluation**.
Create a new independent cohort before making a generalization claim.
See [architecture, diagnosis and remaining limits](../docs/agent_rag_architecture.md).
# Dialogue tasks and native local tools

`task_cases.jsonl` adds six development episodes (14 turns): offers and consent,
slot completion, vehicle confirmation, personal memory and corrections. The voice
runner checks actual execution, exact arguments, memory counts and per-turn p50/p95.
Each run uses temporary history/memory and a simulated vehicle; external networking
is blocked. These are regression cases, not an unseen accuracy estimate.

```bash
.venv/bin/python -m eval.voice_benchmark --dataset eval/task_cases.jsonl \
  --output eval/results/tasks-rules-dev.json
# Requires an already running localhost inference server:
LOCAL_LLM_MODEL=vivi-qwen3-1.7b LLM_TIMEOUT_SECONDS=90 \
  .venv/bin/python -m eval.voice_benchmark --provider local \
  --dataset eval/task_cases.jsonl --output eval/results/tasks-local-native-dev.json
```

See `docs/dialogue_tasks.md` for architecture, pinned artifacts, measured limitations
and why the small experimental SLM is not the default runtime.
