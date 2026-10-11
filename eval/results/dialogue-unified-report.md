# Unified conversation validation — 2026-10-09

Single workflow `unified-3`, prompt `semantic-unified-5`. No v1 builder, shadow
workflow, graph switch or legacy classifier/command interpreter remains in `src`.
All live language routing uses the semantic resolver and fallback parser.

## Local verification

| Suite | Result | Scope |
|---|---:|---|
| Full Python regression | 407/407 | Includes MQTT; no skipped cases with temporary broker available |
| Public API + confirmation (separate) | 21/21 | Turn, stream, typed events, grant lifecycle, fallback and replay |
| MQTT integration (separate) | 13/13 | Real local Mosquitto broker; adapter, state and deduplication |
| Browser workflow | 11/11 | Typed controls, partial steps, confirmation, provider display and stale event/audio protection |
| Speech adapters/API smoke (separate) | 53/53 | Core plus STT/TTS mocked transport/PCM/API; not physical speech quality |
| Parser episodes | 9/9; 21/21 turns | Log-derived contextual tasks, corrections, exclusions, scope and consent |
| Voice/dialogue regression | 57/57 | Route + intent, answer constraints, arguments and simulator effects; zero recorded errors |
| Memory regression | 30/30 | Explicit writes, recall, named commands, preferences, reset consent and persistence |

Final provider/semantic checks: 77/77 after the prompt-only window guidance update.
CLI smoke verified a 25°C command with temporary databases and local-only inference.
`ruff check src/vivi` and changed evaluator files passed; `node --check app.js` and
`git diff --check` passed. MQTT binaries were unpacked under `/tmp`, not installed
as a system service. All test databases are temporary; workspace history/memory
were not reset for tests. Existing greeting text remains intact.

## Provider measurements

| Serving tier / final run | Correct turns | Episode completion | Clarifications | p50 / p95 latency |
|---|---:|---:|---:|---:|
| Parser, full log episodes | 21/21 | 9/9 | 3 | 151 / 248 ms, API total |
| Google, semantic-unified-5 | 21/21 | 9/9 | 3 | 1,898 / 2,983 ms, API total per serving tier |
| Local Qwen | Not measured | Not measured | Not measured | Not measured |

The final Google run served **all 21 fixture turns on cloud**, with no semantic
fallback. Overall end-to-end API p50/p95 was 1,905/2,996 ms; four-second pacing
between turns is excluded. `dialogue-unified-google.json` and
`dialogue-unified-cloud-summary.json` record the final results.

Earlier runs are retained separately, never counted as final cloud accuracy:

- `dialogue-unified-google-fallback.json`: 17/17 cloud turns correct; HTTP 429
  caused the remaining 4 turns to use parser. Mixed run completed 9/9 episodes.
- `dialogue-unified-google-recovery.json`: 13/13 cloud turns correct; a 10-second
  timeout triggered the 30-second breaker and parser served 8/8. Google recovered
  after the breaker. Mixed run completed 9/9 episodes.
- `dialogue-unified-google-window-regression.json`: semantic-unified-4 completed
  8/9 episodes, with 19/21 cloud turns passing; the window motion fault below was
  fixed before the final run.

Parser voice p50/p95: 137/251 ms. Memory p50/p95: 214/287 ms (`memory-unified.json`). The final graph uses synchronous checkpoint durability.
All timings are from this x86_64 WSL host, not on-vehicle hardware. Local was
unavailable in real runs; mock tests cover native semantic calls,
cloud→local→parser, missing local config, schema repair, budgets,
valid-clarification termination and breaker recovery. No Qwen model was downloaded.

## Faults found and fixed

The initial Google run (`dialogue-unified-google-initial.json`) passed 5/9 episodes.
It discarded known actions while asking for slots, proposed incompatible window
fields and misread a cooling/maximum conflict, including an incorrect simulated
climate effect. Capability-specific validation, one schema repair and clearer
semantic instructions fixed these fixtures. A nested capability-union JSON schema
was rejected by Google, so the portable flat contract is used with server-side
capability field validation. Earlier all-parser passes were not cloud measurements.

Broader voice cases exposed how-to/live-status confusion, polite control requests,
troubleshooting statements, ambiguous warning lamps and charging-mode slots. Those
are now parsed at the language boundary, not patched into graph or policy nodes.
The last scope/prompt run (`dialogue-unified-google-window-regression.json`,
semantic-unified-4) passed 8/9: all 21 turns used cloud, but lowering windows was
misinterpreted as closed (0%) and its following confirmation could not complete.
Window parameter descriptions and prompt now explicitly distinguish glass height
from openness; this failure is preserved rather than hidden in aggregate scores.
Consent/exclusion/revision/idempotency regressions assert exact effects; no incorrect
execution was observed in the final suites and final Google run. Additional paraphrases not included in
the model prompt are covered by parser tests.

## Limits

These are development regressions from logs/authored fixtures, not independent
held-out quality estimates. They do not guarantee correctness for arbitrary speech.
Cloud generation factual quality is not measured by router accuracy; grounding,
source metadata, wrong vehicle/year, missing evidence and offline FTS are tested
separately. Physical microphones/speakers, real vehicles and Qwen understanding
remain unmeasured. Deployment remains a single backend worker.

Migration/rollback: [runtime documentation](../../docs/langgraph_unified_runtime.md).
