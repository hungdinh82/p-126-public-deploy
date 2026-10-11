# Đánh giá workflow hiện tại

Bộ đánh giá chính nằm ở `workflow_cases.jsonl` và `workflow_benchmark.py`.
Runner gọi LangGraph và orchestrator đang dùng trong ứng dụng, tạo SQLite riêng cho
checkpoint, navigation state, receipt, effect ledger, history và memory trong thư mục tạm.
Không ghi vào phiên hoặc database đang chạy của người dùng.

```bash
.venv/bin/python -m eval.workflow_benchmark --repeats 3
.venv/bin/python -m eval.navigation_controls_benchmark --samples 100
.venv/bin/pytest -q tests/test_workflow_eval.py
```

Kết quả mặc định: [report tổng hợp](results/report.md),
[workflow Markdown](results/workflow.md), [workflow JSON](results/workflow.json),
[P/D JSON](results/navigation-controls.json). Runner workflow ghi report cả khi có lỗi
và trả exit code 1 nếu bất kỳ episode nào không đạt. Runner P/D cũng trả 1 nếu vượt
budget; lỗi hành vi/receipt khiến phép đo dừng và không tạo report thành công.

## Profile và phạm vi đo

| Profile | Dependency | Ý nghĩa |
|---|---|---|
| `rules` | OfflineParser thật; map/energy tổng hợp | Regression hiểu lệnh bằng rules và workflow |
| `fixture` | Model contract stub; map/energy tổng hợp | Provider timeout, JSON lỗi, fallback, semantic contract và orchestration |
| P/D | Xe mô phỏng, SQLite/receipt/ledger thật | Latency backend khi bình thường, model chờ, map chờ |

Model fixture mặc định chuyển câu nói qua parser để dựng semantic; một số operation
cấp semantic cố định hoặc gây timeout/JSON lỗi. Đây **không phải điểm chính xác của
Google/OpenAI/local model**. Report ghi cả provider được yêu cầu và provider thực sự
phục vụ để không tính fallback rules là thành công của model thật.

Các fixture không chứa API key, không gọi VietMap/model bên ngoài. Network guard
chỉ cho phép loopback; runner chính không cần HTTP. STT/TTS tắt. Handbook fixture
trả evidence rỗng để kiểm abstention, không đo chất lượng trả lời cẩm nang.

## Hợp đồng scenario

Mỗi dòng JSONL là một episode độc lập với `id`, `group`, `source`, `operations` và
`providers` tùy chọn. Mỗi episode/repeat/profile dùng runtime mới. `restart` giữ
SQLite và vehicle fixture, dựng lại graph để kiểm recovery mà không phát lại effect.

Mỗi operation có ID riêng, kiểu thao tác và `expect` độc lập. `setup` chỉ chuẩn bị
fixture; các thao tác còn lại bắt buộc có assertion. Kết quả được so với trạng thái
vehicle adapter thực tế (`observed.vehicle`) thay vì chỉ đọc prose hoặc `verified`.

```json
{"id":"pause","type":"event","event":{"kind":"gear","gear":"P"},"expect":{"receipt.status":"completed","observed.vehicle.driving":false}}
```

`expect` dùng đường dẫn qua object/list, so sánh bằng hoặc các toán tử `contains`,
`not_contains`, `length`, `min`, `exists`, `one_of`, `same_members`.
`{"$ref":"select.trip_plan.id"}` ràng buộc ID/revision từ bước trước vào event,
action hoặc oracle. Thiếu reference, typo operator và dataset rỗng/trùng ID đều bị chặn.
Không cập nhật expected từ output của chính lượt chạy.

| Operation | Kiểm chứng |
|---|---|
| `turn`, `action`, `event` | Hội thoại, typed action và điều khiển trực tiếp |
| `stream` | Event stream có final; effect không nhân đôi khi replay |
| `snapshot` | Bootstrap/đối chiếu receipt, domain state và checkpoint không chứa geometry |
| `restart` | Phục hồi SQLite, giữ memory, không tự phát D |
| `replay`, `collision` | Dedup turn; cùng ID với payload khác bị từ chối |
| `lost_response` | Bỏ response nhận được; lấy receipt từ snapshot để đối chiếu |
| `parallel_park` | Barrier giữ model/map chờ, P hoàn tất trước khi thả barrier |
| `cancel` | Hủy khi model còn chờ, chưa dispatch, không có effect |
| `setup` | Battery, driving, map lỗi, tuyến 10.000 điểm |

Các nhóm bao phủ slot/correction/exclusion, multi-step scheduler, consent, policy,
memory, abstention, stream, chọn POI bằng số/tên, số ngoài danh sách, draft/journey,
P/D, restart/replay, hai tab với revision cũ, provider/dependency failure, cancel,
checkpoint lớn và Sầm Sơn/khứ hồi. Danh sách thao tác và oracle đầy đủ ở [kịch bản](workflow_scenarios.md).

## Phương pháp benchmark

Workflow mặc định chạy mỗi episode ba lần trên cả hai profile, trừ các case chỉ có
ý nghĩa với model fixture. Ghi riêng thời gian dựng runtime và thời gian từng operation,
node spans, provider attempts và latency search/route/energy khi runtime cung cấp.
P50 là median; p95 là nearest rank `ceil(0.95 * n)`, không bỏ failure khỏi latency.
Ghi thời điểm UTC+7, revision/dirty flag, hash dataset, workflow/prompt version và
phiên bản dependency. Dataset này là development regression đã biết, không phải holdout.

P/D chạy tuần tự sau workflow để tránh hai benchmark tranh tài nguyên. Mỗi tải có
5 warmup và ít nhất 100 mẫu xen kẽ P/D trên hành trình đã nhận. Mỗi mẫu kiểm receipt
completed và trạng thái xe. Dependency chậm phải còn chờ suốt phép đo. Budget p95
backend là 300 ms bình thường, 500 ms khi dependency chờ. **Chưa gồm HTTP, click,
render/browser, mạng thật hay tải nhiều worker.** Muốn nghiệm thu click-to-confirm
phải chạy thêm browser E2E và lưu trace trên cùng revision.

## Các suite chuyên biệt còn dùng

- RAG: `benchmark.py`, `build_golden.py`, `golden_dataset.jsonl` và metadata.
  Giữ `results/local-dev.*`, `local-test.*` làm baseline lịch sử, không trộn với điểm workflow.
- Memory: `memory_benchmark.py`, `memory_cases.jsonl` còn được regression sử dụng.
- Voice/SLM: `voice_benchmark.py`, `voice_cases.jsonl`, `conversation_cases.jsonl`,
  `task_cases.jsonl` cho smoke text/SLM riêng; tên voice không có nghĩa đã đo audio.
- UI/map: `navigation_control_server.py`, `navigation_control_browser.cjs`,
  `navigation_frame_browser.cjs`, `information_rail_browser.cjs`, `vietmap_map_browser.cjs`.
  Xem [navigation runtime](../docs/navigation_runtime.md) về setup browser/fixture server.
- `charging_map_smoke.py`, `navigation_followups_replay.py`: phép thử chuyên biệt;
  chế độ live phải chạy tách biệt, có nguồn dữ liệu và dependency thật được ghi rõ.
  Replay log phụ thuộc dữ liệu local, không dùng làm bộ regression di động chính.

```bash
.venv/bin/python -m eval.benchmark --split dev --output eval/results/local-dev.json
.venv/bin/python -m eval.benchmark --split test --output eval/results/local-test.json
.venv/bin/python -m eval.memory_benchmark
.venv/bin/python -m eval.voice_benchmark
```

Các runner workflow cũ chia theo ngày và snapshot trung gian đã được thay thế.
[Manifest dọn dẹp](cleanup.json) liệt kê file xóa/chuyển. Audit JSONL còn được test
đọc đã chuyển sang `tests/fixtures/dialogue_audits`; các test được sửa đường dẫn.
`eval/artifacts/navigation-runtime` giữ bằng chứng UI/map trước đó và có ngày riêng;
không tính những artifacts đó là đã đo lại trong lượt benchmark mới. Lịch sử Git
vẫn chứa kết quả đã dọn nếu cần đối chiếu.
