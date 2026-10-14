# Navigation latency and Đà Nẵng replan review — 2026-10-13

## Findings from the latest local logs

Source: `data/2026-10-13/turns.jsonl` and read-only conversation history. Times below are UTC.

| Turn time | SOC | Result | Station discovery |
| --- | --- | --- | --- |
| 08:54:01 | 81% | Đà Nẵng trip plan incomplete | 8.36 s; `station_search_budget`, no retained stations |
| 09:05:56 | 29% | Đà Nẵng plan with five charging stops | 4.11 s; candidate limit reached but feasible road legs found |
| 09:11:15 | 28% | Start triggered revalidation and then blocked | 8.01 s; `station_search_budget`, no retained stations |

The destination was recognized in all three cases. Failure was in charging discovery during route planning/revalidation. GPS or SOC changes correctly invalidate the previous start offer; the replan previously discarded all discovery progress at the outer deadline and did not retain the corridor catalogue for reuse.

Semantic interpretation also took about 2–4 s. Several graph nodes took hundreds of milliseconds or more. Long routes duplicate thousands of geometry points in waypoint routes, selected plans and alternatives; repeatedly copying and serializing these structures adds CPU cost. These observations do not attribute every slow node to geometry: SQLite, streaming projections and machine load also contribute.

The persisted `latency_ms.total` was inaccurate: it summed capability spans together with nested dependency spans. The client received an elapsed wall-time value later, after the log had already been written.

## Changes

- Discovery reserves 40% of its budget for resolving successful search results. Slow queries are cancelled; partial results remain available. The built-in discovery owns its deadline, avoiding a second identical timeout that discarded its return value.
- Route geometry is prepared once per discovery. Station projections run in workers and calculate the geodesic gap only for the closest segment.
- Successful plans retain a bounded corridor catalogue. Replans can reuse POIs observed within ten minutes, provided destination, trip kind, battery profile, equipment and assumptions still match. Observation time is not renewed on reuse.
- Cached POIs are projected onto the new route; synthetic or unidentified points and off-corridor stations are excluded. Every road leg, detour and SOC/charge estimate is recalculated. If reuse cannot produce a plan, fresh discovery is attempted once. Cache reuse does not assert station availability or queue status.
- Navigation hydration caches immutable JSON and parses independent mutable state, avoiding copying and serializing geometry that would immediately be replaced. Domain comparisons no longer serialize both states. Checkpoint response construction omits geometry owned by public domain projection.
- Log and API response now receive the same elapsed wall-time total before logging. Nested timings remain diagnostic spans, not additive contributions to total.

Changed routes still require confirmation; inadequate energy or missing road evidence still blocks starting.

## Verification

Deterministic tests cover partial search timeout/cancellation, retaining stations through the workflow, SOC replan with search unavailable, fresh road-leg validation, one refresh when cached stations cannot produce a plan, cache expiry/future timestamps, changed calculation inputs, corridor filtering, hydration isolation and log/client timing consistency for both regular and streamed responses. Existing navigation, charging, trip, concurrency, consent and conversation tests were also run.

Final shared gate: `bash scripts/check_local.sh` passed, including Ruff, whitespace, frontend syntax, and **914 Python tests** (381.75 s). The new latency review contributes thirteen regression cases.

Local microbenchmarks used recorded geometry without making external requests:

- Five projections on a 5,656-point route: approximately 106 ms previously, 41 ms with shared preparation (no gap difference for the sampled stations).
- Rehydrating recorded navigation/trip state: approximately 176 ms previously, 30 ms after immutable JSON caching in a paired run.

These are CPU measurements on the local machine, not an end-to-end latency promise. No live replay of private GPS/history was sent to map or model providers. Restart the backend before measuring new real-service turn logs.

A separate synthetic 746 km corridor replay reproduced the five-stop case with 29% SOC. After changing the origin and lowering SOC to 28%, with station search deliberately unavailable, the replan still produced five stops and 15% arrival reserve. The initial 46 search requests were not repeated; road legs and charging durations were recalculated.

Follow-up review of the 20:02–20:04 Vietnam-time turns addresses default destination confirmation, model fallback delays and streaming projections: [default destination review](default_destination_review_2026-10-13.md). The shared gate after those additional changes passed **939 tests** (379.90 s).
