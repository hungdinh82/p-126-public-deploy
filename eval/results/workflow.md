# LangGraph workflow benchmark

Thời điểm: 2026-10-11T12:34:59.893654+07:00.
Revision: `3697bd16f60c`; working tree có thay đổi: True.
Dataset SHA-256: `fa97e0ff25933a87ee3e5c464a8a2e832bbba5214433da69ab9937180e2cc466`.

Kết quả: **210/216 episode qua**.

| Provider | Episode qua | Operation qua | Turn p50 / p95 ms | Cold build p95 ms |
|---|---:|---:|---:|---:|
| rules | 96/99 | 264/267 | 103.72 / 147.68 | 137.37 |
| fixture | 114/117 | 294/297 | 117.28 / 235.16 | 312.48 |

| Nhóm | Episode qua |
|---|---:|
| vehicle_dialogue | 54/54 |
| scheduler | 6/6 |
| consent | 12/12 |
| policy | 12/12 |
| recovery | 18/18 |
| memory | 6/6 |
| handbook | 6/6 |
| transport | 6/6 |
| place_selection | 24/24 |
| dependency_failure | 6/6 |
| journey_lifecycle | 12/12 |
| control_order | 12/12 |
| concurrency | 9/9 |
| persistence | 6/6 |
| trip_planning | 9/15 |
| provider_failure | 9/9 |
| cancellation | 3/3 |

## Lỗi còn lại

- `rules/sam-son-deictic-followup/return`: `{"trip_plan.trip_kind": {"passed": false, "expected": "round_trip", "actual": "one_way"}, "trip_plan.waypoint_routes:length": {"passed": false, "expected": 2, "actual": 1}}`
- `rules/sam-son-deictic-followup/return`: `{"trip_plan.trip_kind": {"passed": false, "expected": "round_trip", "actual": "one_way"}, "trip_plan.waypoint_routes:length": {"passed": false, "expected": 2, "actual": 1}}`
- `rules/sam-son-deictic-followup/return`: `{"trip_plan.trip_kind": {"passed": false, "expected": "round_trip", "actual": "one_way"}, "trip_plan.waypoint_routes:length": {"passed": false, "expected": 2, "actual": 1}}`
- `fixture/sam-son-deictic-followup/return`: `{"trip_plan.trip_kind": {"passed": false, "expected": "round_trip", "actual": "one_way"}, "trip_plan.waypoint_routes:length": {"passed": false, "expected": 2, "actual": 1}}`
- `fixture/sam-son-deictic-followup/return`: `{"trip_plan.trip_kind": {"passed": false, "expected": "round_trip", "actual": "one_way"}, "trip_plan.waypoint_routes:length": {"passed": false, "expected": 2, "actual": 1}}`
- `fixture/sam-son-deictic-followup/return`: `{"trip_plan.trip_kind": {"passed": false, "expected": "round_trip", "actual": "one_way"}, "trip_plan.waypoint_routes:length": {"passed": false, "expected": 2, "actual": 1}}`

## Giới hạn

- Model fixture is a semantic contract stub, not model NLU accuracy.
- Maps, car and energy are labelled synthetic fixtures; SQLite/checkpoints/policy are real.
- No real STT/TTS, GPU/browser, VietMap, physical vehicle or cloud inference is measured.
- Episode latency samples are functional regression, not a 100-sample P/D performance gate.

Kết quả ghi cả failure; exit code 1 nếu bất kỳ episode nào không qua. Không đổi oracle theo output để làm xanh báo cáo.
