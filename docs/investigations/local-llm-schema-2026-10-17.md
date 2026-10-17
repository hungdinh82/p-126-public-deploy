# Local Qwen3 semantic payload investigation — 2026-10-17

## Scope and method

Read-only inference against `http://100.123.27.125:8789/v1/chat/completions`, model `vivi-qwen3-1.7b` (Qwen3-1.7B Q4_K_M, CUDA). No vehicle effects, persistent conversation state, or production configuration changes.

Capture the actual `SemanticResolver.resolve` / `interpret` system prompt, tools and serialized context with a fake client; then replay the captured payload against Jetson. Use a fresh empty `SessionState`, synthetic Vietnamese utterances, full capability catalog with navigation/conversation/information enabled, temperature 0, thinking disabled, and production planner cap of 2048 output tokens. This is a full-catalog scenario, not evidence that every deployed session enables all capabilities. No production history or memory was used.

Variants:

- **current**: native `interpret_turn` tool with `strict_schema(semantic_schema())`.
- **optional**: same request except use the original semantic schema, preserving optional fields and closed objects.
- **compact**: optional schema plus capability descriptions, slot names and required names instead of duplicated full capability parameter schemas. Experimental prompt reduction only; detailed constraints remain in the tool schema and server validators.
- **examples**: compact variant plus three short examples (greeting, climate at a different temperature, driver window at a different percentage) and explicit act/slot instructions. These are development probes close to the target cases, not held-out accuracy.

Requests run sequentially, with one sample per case/variant. Cache reuse differs between variants; these are diagnostic measurements, not p50/p95 benchmarks. Direct calls allow up to 95 seconds so the output can be inspected; production model-gateway deadlines are examined separately. No automatic schema repair in these measurements.

Google live comparison was initially blocked by automatic approval review. The user subsequently explicitly authorized two synthetic requests to the configured Google API. Both requests were completed using `gemini-3.5-flash-lite`, the same captured original prompt/catalog and its production JSON-mode payload. Credentials were used only for authentication and are not saved in artifacts.

## Root causes confirmed in source

1. `src/vivi/dialogue/planner.py:104` applies `strict_schema` to the native local tool. `src/vivi/structured_llm.py:21` recursively marks every property required. Top-level required fields expand from just `act` to 14 fields; every action receives all 35 argument slots, regardless of capability. This contradicts the compact-output instruction in `planner.py:360`. Nullable means a required field may be null, not that it may be omitted.
2. The full capability catalog repeats parameter schemas already represented in the semantic tool. In the greeting probe: system prompt 9,234 characters; user context 22,081 characters, of which capability catalog 21,753 characters. Current input reaches 11,857 tokens. The Jetson startup default context is 12,288 (`scripts/jetson_ai.sh:14`); the response's prompt+completion total reaches exactly 12,288 and ends with `length`.
3. Google uses `response_format=json_object` for `vivi_semantic_turn` and embeds the original portable schema in the prompt (`structured_llm.py:265`). It therefore does not use the local all-fields-required native tool contract. A larger/smarter cloud model and different generation protocol also confound a direct provider comparison.
4. The actual planner cap is `planner_max_tokens=2048`, assigned in `src/vivi/orchestration.py:697`. `local_llm_max_tokens=512` is not the effective planner cap. Raising the output cap alone cannot recover context already consumed by input.
5. `src/vivi/model_gateway.py:175` caps each provider attempt at `min(10, provider.budget_seconds, remaining/available)`. The configured HTTP timeout or increasing `local_budget_seconds` above ten alone cannot extend that cap. Measured direct requests above ten seconds would miss the current orchestration budget; this is source-based inference, not an end-to-end replay.
6. `_tool_result` parses tool arguments without first distinguishing `finish_reason=length`. The planner catches malformed/truncated JSON as a generic validation error and asks for a complete repair, adding more text to an already large prompt.
7. Removing forced fields reduces output, but does not guarantee semantic correctness. Observed optional-schema responses select `act=answer` without a pending question and include premature success text. Existing `SemanticTurn` validation rejects these responses. Do not relax validators to make the benchmark pass.

## Fix plan

### 1. Separate local schema formatting from provider strict mode

Use a local native-tool schema that preserves genuine `required` declarations and `additionalProperties=false`. Keep the existing strict-schema formatter for providers/modes that require it. Do not globally change `strict_schema`.

Add contract tests for the local payload: `act` required; unused top-level fields optional; action capability required; absent argument slots retain server defaults; unknown slots and wrong-capability slots still fail validation. Preserve exactly one native call and the existing reducer/confirmation boundary.

### 2. Reduce prompt duplication and clarify semantic acts

Create a compact capability catalog for model input, preserving capability identity, meaning, applicable slot names and necessary constraints; omit repeated schema metadata/titles/defaults and duplicate definitions. Bound history and task context while preserving pending-question IDs, editable indices, provenance, dependent operations and navigation purpose.

Add small examples for `reply`, `request`, `answer` only with a current question, and `modify`; explicitly forbid reporting a proposed action as completed. Prioritize capability-specific argument schemas and act-specific wire contracts: the examples experiment still invents window slots and silently drops the window clause in a compound request. Map the compact wire result back to the existing internal DTO. Verify llama.cpp grammar support for the proposed unions before adopting them; retain a single interpretation call if possible. Avoid adding another model routing call until measurements justify the latency.

### 3. Detect truncation and make deadlines coherent

Record `finish_reason`, prompt/completion tokens, cache tokens and available context alongside latency/repair counts. Reject truncated tool calls explicitly before parsing or executing. Do not blindly append a repair prompt when the context is exhausted. Reserve output capacity before sending; on overflow, compact optional context deterministically or return a clear bounded failure.

Make the effective provider budget explicit and consistent with the whole-turn deadline, queue time and HTTP timeout. Decide whether the current 10-second service target is mandatory; if so, optimize and test to it. If a larger local budget is accepted, remove the hidden ten-second clamp deliberately and still honor the remaining whole-turn deadline. Increasing timeout alone is not a fix for invalid output.

### 4. Regression and acceptance

Use a fixed corpus with expected semantic results: greetings; absolute/relative climate changes; window position; missing target; compound commands; pending-question answers; corrections; navigation purpose/references; unsupported/negated clauses. Test simulator effects separately after semantic acceptance; never replay diagnostic actions against a real vehicle.

Measure cold and warm requests separately, group equivalent cache conditions, repeat enough samples for latency distributions, and compare local before/after. The two Google baseline cases in this report are authorized and completed; extend the comparative corpus only within agreed data scope.

Acceptance gates: no truncated JSON on the corpus; no unnecessary null slots; correct act/capability/arguments and preserved compound clauses; no premature success claims or invented question/task references; no validator/confirmation regression; prompt plus reserved output fits context; measured p95 fits the agreed effective budget. Proposed initial simple-command target: at most 150 output tokens, subject to semantic accuracy rather than forced clipping.

## Artifacts and results

Raw synthetic requests/responses and summary are retained in `/tmp/p126-schema-probe/`; reproduction harness is `/tmp/p126_schema_probe.py`. Compact experiment uses `/tmp/p126-schema-compact-probe/` and `/tmp/p126_schema_compact_probe.py`. They contain no API credentials. Examples experiment: `/tmp/p126-schema-examples-probe/`, harness `/tmp/p126_schema_examples_probe.py`. Google baseline: `/tmp/p126-google-schema-probe/`, harness `/tmp/p126_google_probe.py`.


### Measured results (18 inference requests)

| Case | Variant | Input tokens | Output tokens | Seconds | Finish | DTO valid | Expected semantics |
|---|---|---:|---:|---:|---|---|---|
| greeting | current | 11857 | 431 | 41.474 | length | no | incorrect/incomplete |
| greeting | optional | 11595 | 82 | 23.579 | tool_calls | no | incorrect/incomplete |
| climate | current | 11866 | 422 | 36.647 | length | no | incorrect/incomplete |
| climate | optional | 11604 | 104 | 24.298 | tool_calls | no | incorrect/incomplete |
| window | current | 11864 | 424 | 36.548 | length | no | incorrect/incomplete |
| window | optional | 11602 | 217 | 28.798 | tool_calls | no | incorrect/incomplete |
| compound | current | 11872 | 416 | 36.466 | length | no | incorrect/incomplete |
| compound | optional | 11610 | 299 | 31.361 | tool_calls | no | incorrect/incomplete |
| greeting | compact | 6975 | 63 | 6.254 | tool_calls | no | incorrect/incomplete |
| climate | compact | 6984 | 59 | 6.306 | tool_calls | no | incorrect/incomplete |
| window | compact | 6982 | 132 | 8.350 | tool_calls | no | incorrect/incomplete |
| compound | compact | 6990 | 98 | 7.171 | tool_calls | no | incorrect/incomplete |
| greeting | examples | 7116 | 29 | 10.059 | tool_calls | yes | correct |
| climate | examples | 7125 | 45 | 5.566 | tool_calls | yes | correct |
| window | examples | 7123 | 105 | 7.788 | tool_calls | no | incorrect/incomplete |
| compound | examples | 7131 | 102 | 7.315 | tool_calls | yes | incorrect/incomplete |
| climate | google | 10834 | 33 | 1.079 | stop | yes | correct |
| compound | google | 10840 | 63 | 1.100 | stop | yes | correct |

All four current full-catalog payloads returned HTTP 200 but hit `finish_reason=length`, total tokens exactly 12,288, and unparseable JSON. Optional fields eliminated null padding and truncation in these four cases, but zero passed the semantic DTO validator. Compact catalog reduced input to about 7,000 tokens and measured latency to 6–8 seconds with substantial cache reuse; zero passed validation. Adding examples produced three DTO-valid outputs, but only two were semantically correct: the compound result omitted the requested window operation. Schema validity alone is not acceptance.

Google correctly returned both requested operations for the compound case, including `position_percent=50` and `zone=front_passenger`, in 63 output tokens and 1.100 seconds. Its climate result used 33 output tokens and 1.079 seconds. This corroborates the reported provider difference on two synthetic cases, not general Google accuracy.

The first examples request had less prefix cache and took 10.059 seconds; subsequent ones took 5.566–7.788 seconds. Do not claim the local ten-second deadline is solved, especially for cold prompts, history-heavy turns, repair calls or concurrent speech load. Diagnostic timings include network and server time and are not a controlled statistical speedup estimate.

No production Python/configuration files were modified. Only this investigation report and its summarized synthetic results were added. The earlier `.env` TTS validation problem is already corrected to `zerotts` in the user's current file; diagnostic processes temporarily used `TTS_PROVIDER=off` without writing `.env`.
