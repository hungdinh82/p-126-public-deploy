# LangGraph workflow benchmark

Thời điểm: 2026-10-11T18:52:08.130791+07:00.
Revision: `2b65588af19e`; working tree có thay đổi: True.
Dataset SHA-256: `a2b398618b3489ff1280ced46ef5bc76f7fbf4def88d278ba2badb7dbcf71590`.

Kết quả: **12/12 episode qua**.

| Provider | Episode qua | Operation qua | Turn p50 / p95 ms | Cold build p95 ms |
|---|---:|---:|---:|---:|
| rules | 6/6 | 24/24 | 161.91 / 244.36 | 212.83 |
| fixture | 6/6 | 24/24 | 170.44 / 387.18 | 273.92 |

| Nhóm | Episode qua |
|---|---:|
| journey_lifecycle | 12/12 |

## Lỗi còn lại

Không có lỗi trong phạm vi dataset này.

## Giới hạn

- Model fixture is a semantic contract stub, not model NLU accuracy.
- Maps, car and energy are labelled synthetic fixtures; SQLite/checkpoints/policy are real.
- No real STT/TTS, GPU/browser, VietMap, physical vehicle or cloud inference is measured.
- Episode latency samples are functional regression, not a 100-sample P/D performance gate.

Kết quả ghi cả failure; exit code 1 nếu bất kỳ episode nào không qua. Không đổi oracle theo output để làm xanh báo cáo.
