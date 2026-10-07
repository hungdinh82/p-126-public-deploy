# ViVi: spoken responses and intent boundaries

The current implementation is described in [agents/RAG architecture](agent_rag_architecture.md).
The October 7 audit below documents the earlier baseline; use the `*-refactor-*`
reports for current results.

The shared persona lives in `src/vivi/agents/prompts.py`. ViVi is the voice of the
VF8, speaks as “mình” to “bạn”, and gives the useful answer first. Normal replies
are one or two short sentences. Identity is not repeated after every greeting.
Vehicle identity must not imply autonomous driving, human feelings, unknown
optional equipment or invented sensor observations.

This follows the principles of [brief informational responses](https://developers.google.com/assistant/conversation-design/informational-statements)
and [clear action confirmations](https://developers.google.com/assistant/conversation-design/confirmations).
The existing vehicle confirmation policy still determines which actions need approval.

| Request | Behavior |
| --- | --- |
| “Điều hoà đang là bao nhiêu độ?” | Call `vehicle.get_status`, read the verified temperature only |
| “Tình trạng pin hiện tại” | Call the status tool; do not use handbook facts as observations |
| “Bạn có thể mở cửa sổ bên tài giúp tôi không?” | Propose the window action; preserve the confirmation gate |
| “Bạn có cruise control không?” | Retrieve the handbook; “bạn” can refer to the car |
| “Cách kiểm tra áp suất lốp” | Handbook instructions, not a sensor read |
| “Đừng mở cửa” | Acknowledge without an action |
| “Mở cửa sổ và phát nhạc” | Ask which action to do first; do not silently drop a command |
| “Hướng dẫn sử dụng xe” | Ask a focused question about charging, cabin or driver assistance |

Explicit negative commands and media stop requests bypass the local model in both
sync and async paths. Pending task replies resume the persisted workflow directly.
Local language understanding uses native function calls; ordinary dialogue and
handbook questions reach the model. The rules provider remains an independent
fallback with deterministic climate extrema and delta/target handling.
Other model decisions use the expanded intent prompt and the existing argument
validation, safety, confirmation, execution and verification pipeline.
Relative climate adjustments require a current observation and stay within
16–30°C; a supplied absolute target outside that range is still rejected.
Short climate follow-ups only use the immediately preceding relevant turn.

Verified replies are composed from tool results in `agents/voice.py`; the model
cannot decide that an action succeeded. Missing observations are not replaced
with remembered numbers. State reads do not change the simulator state version.

Handbook citations remain in the structured API evidence and UI. Spoken answers
do not include source IDs, URLs or section preambles. The rules profile selects
at most two complete literal evidence units with a 360-character budget. The
model prompt asks for the main fact or essential steps; responses over 360
characters or 70 space-delimited words use the short extractive fallback instead
of being cut mid-sentence. Table duplication and obvious headings are removed.
Explicit tire and battery variants are filtered before vector/FTS fusion.
Queries expand English feature names to handbook terminology, and lexical search
removes conversational filler.

These rules do not turn extraction into semantic summarization. Multi-step
procedures, definitions and conflicting equipment variants still require a capable
local SLM and review. Short answers and valid citation IDs do not prove the answer
is sufficient or semantically supported. Voice pitch/prosody and STT models are
unchanged; this change concerns wording and routing.

Recent user history was available in `data/vivi_rag.sqlite3`, including turns from
2026-10-07. The development cases copy only short utterances, without session IDs
or the complete personal conversation. Original history is not changed.

```bash
.venv/bin/python -m eval.voice_benchmark
# If a local SLM is configured:
.venv/bin/python -m eval.voice_benchmark --provider local \
  --output eval/results/voice-slm-dev.json
.venv/bin/python -m eval.benchmark --split dev \
  --output eval/results/voice-rag-dev.json
```

`eval/voice_cases.jsonl` is a draft development set, separate from the handbook
held-out split. `voice-dev.json` measures routing and response length against an
in-memory car with external TCP blocked. It is not a production accuracy claim.
`voice-rag-dev.*` records the updated retrieval/extractive behavior; prior reports
remain historical baselines. Run again on the actual local SLM and Xavier before
using these results to judge deployment quality.

## October 7 conversation audit

The running backend reports `rules`, with no configured local SLM. Changing a
system prompt alone therefore cannot improve the running rules implementation.
The most recent conversation exposed the following failures:

| User request | Observed failure | Updated behavior |
| --- | --- | --- |
| “bạn giúp gì được cho mình” | Repeated generic clarification | Give a short list of supported capabilities |
| A personal introduction | Generic clarification | Acknowledge the name naturally; do not silently save it to long-term memory |
| “tôi muốn hỏi về chiếc xe này” | Random lock-feedback instructions | Ask which area the user wants to discuss |
| A red warning light, without its symbol | Guessed an interior-light warning | Ask for the symbol/message before selecting a handbook explanation |
| ADAS definition | Instructions to switch on parking assistance | Compose retrieved feature examples with their relevant limitation |
| Smart features | Generic clarification | Describe features supported by retrieved ADAS evidence, with a variant caveat |
| Cruise-control definition | An operational note instead of the definition | Prefer the actual definition and its speed/distance behavior |
| Current battery information | Manufacturer/threshold prose | Read the status tool |
| “còn đi được bao lâu” | Answered km as if it answered duration | Clearly distinguish unknown time from available range |
| Time to charge fully | Wireless phone-charging controls | Ask AC/DC first; exclude phone-charging evidence |
| Maximum temperature | Increased by 2°C | Set 30°C; minimum means 16°C |

The vector search already returned the proper ACC definition, but short unrelated
sentences won the excerpt scoring. Topic filters now distinguish EV and phone
charging, and definition questions select definition sentences rather than
control instructions. Count questions require the object being counted, not two
syllables from another topic. A handbook procedure for entering a password is not
the user's actual Wi-Fi password. These checks are deterministic; they do not
constitute a general semantic entailment validator.

Canonical retrieval queries from routing are now used without rewriting the
original logged utterance. A new explicit topic does not inherit the previous
handbook question merely because it starts with “còn/vậy”. Elliptical follow-ups
can recover the previous canonical query. Original unsafe/out-of-scope requests
remain protected even if a model proposes a different retrieval query.

New SQLite history rows include `diagnostics_json`: generation provider,
retrieval query, retrieved/accepted source IDs and stage errors. Existing rows
remain readable through an additive migration; missing historical diagnostics
are not reconstructed. User conversation and long-term memory are not reset.

`voice_cases.jsonl` now contains 57 draft development cases. The runner also
checks selected expected answer terms, forbidden terms and climate arguments;
these checks remain coarse regression assertions, not semantic grading.
`results/voice-oct07.json` records 57/57 route/intent cases with no asserted answer
or argument failures. `results/rag-oct07-dev.*` and `results/rag-oct07-test.*`
record the separate handbook evaluation. Development hybrid Recall@5 remains
0.9524; both development unanswerable handbook cases now abstain. The test split
still has a routing miss for a mirror-control question and insufficient handling
of an unanswerable case. No held-out labels were changed or used to fix those
remaining failures. Broad conversation and semantic correctness still need a
configured local SLM, expanded independent evaluation and manual review.

```bash
.venv/bin/python -m eval.voice_benchmark --output eval/results/voice-oct07.json
.venv/bin/python -m eval.benchmark --split dev --output eval/results/rag-oct07-dev.json
.venv/bin/python -m eval.benchmark --split test --retriever hybrid \
  --output eval/results/rag-oct07-test.json
```

Restart `run.py` after these changes: its Uvicorn configuration disables automatic
reload. These measurements use a simulated vehicle and development host; they do
not measure real-car behavior, STT transcription errors, TTS prosody or Xavier load.
