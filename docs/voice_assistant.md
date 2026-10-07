# ViVi: spoken responses and intent boundaries

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

Clear live-state reads and negative commands bypass the model router in both
sync and async paths. Supported polite controls also have deterministic routing.
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
