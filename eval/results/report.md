# Benchmark LangGraph — 11/10/2026

**Workflow đạt 210/216 episode (97,22%); gate chức năng vẫn FAIL vì câu hỏi khứ hồi Sầm Sơn.** P/D đạt budget backend ở cả ba tải. Đây là kết quả rules và model fixture trên máy phát triển, chưa phải nghiệm thu model thật hoặc toàn bộ UI.

Thời điểm workflow: `2026-10-11T12:34:59.893654+07:00`. P/D: `2026-10-11T12:33:02.714954+07:00`.
Revision: `3697bd16f60cf06a7ddf8efe75879461e7619f50`; working tree có thay đổi: `True`.
Workflow: `unified-6`; prompt: `semantic-unified-16`.
Dataset SHA-256: `fa97e0ff25933a87ee3e5c464a8a2e832bbba5214433da69ab9937180e2cc466`.

## Cách chạy và nguồn kết quả

```bash
.venv/bin/python -m eval.workflow_benchmark --repeats 3
.venv/bin/python -m eval.navigation_controls_benchmark --samples 100
```

Workflow trả **exit code 1** và vẫn ghi đầy đủ failure. P/D trả **exit code 0**. Không sửa expected của câu hỏi tự nhiên để làm xanh report.

- [Kịch bản và oracle](../workflow_scenarios.md), [dataset chạy tự động](../workflow_cases.jsonl).
- [Workflow JSON](workflow.json): từng operation, checks expected/actual, node spans, provider attempts và latency.
- [Workflow Markdown](workflow.md): kết quả theo nhóm và từng failure.
- [P/D JSON](navigation-controls.json): cả 300 mẫu, phân tách P/D, queue và control.
- [Hướng dẫn](../README.md), [validation](validation.json), [manifest dọn dẹp](../cleanup.json).

## Môi trường và phương pháp

WSL2 Linux x86_64; Intel Core i5-11300H @ 3.10 GHz, 4 core/8 thread; RAM khả dụng cho môi trường khoảng 5,69 GiB. Python 3.12.12; LangGraph 1.2.14; checkpoint-sqlite 3.1.1; Node 20.20.2. Xe mô phỏng, một runtime/worker. Không dùng GPU trong benchmark này.

39 episode với 102 operation khai báo, gồm setup fixture. Chạy ba lần trên rules và model fixture; sáu case chỉ chạy fixture vì kiểm model contract/fault. Tổng cộng 216 lần chạy episode và 564 operation có oracle, không tính setup. Mỗi episode có SQLite riêng cho checkpoint/domain/receipt/ledger/history/memory. Restart dựng lại graph trên cùng SQLite và không tự phát D. Mọi runtime được đóng sau case.

Vehicle adapter, policy, graph, reducer/scheduler và SQLite là code thật; bản đồ/energy/model là fixture tổng hợp có nhãn. Model fixture mặc định dùng parser dựng semantic, hoặc nhận semantic/fault cố định. Điểm fixture đo orchestration/contract/fallback, **không đo NLU model thật**. Handbook dùng evidence rỗng để kiểm abstention; không đo factual accuracy. Guard chặn TCP ngoài loopback, không ghi vào database/session thật.

Đo wall time bằng perf_counter. P50 dùng median; p95 dùng nearest rank ceil(0,95 × n). Không loại failure khỏi phân bố thời gian. Cold build được đo riêng và không gộp vào turn latency. Turn latency gộp turn/stream/action/event nên không đại diện riêng tốc độ model. Không có confidence interval hoặc claim holdout. Workflow hoàn tất trong khoảng 86,2 giây; P/D được đo riêng khi không có suite khác chạy.

## Kết quả chức năng và latency

| Profile yêu cầu | Episode qua | Operation qua | Turn p50 / p95 (ms) | Cold build p95 (ms) |
|---|---:|---:|---:|---:|
| rules | 96/99 | 264/267 | 103.72 / 147.68 | 137.37 |
| fixture | 114/117 | 294/297 | 117.28 / 235.16 | 312.48 |

**558/564 operation đạt (98,94%).** Sáu operation lỗi là cùng một câu hỏi tự nhiên, trên hai profile × ba repeat. Mỗi repeat lỗi giống nhau.

Provider phục vụ thực tế được ghi riêng: profile fixture có 159 lượt do fixture phục vụ, 48 lượt rules và 51 typed event. Rules ở các lượt chọn điểm hoặc fallback là hành vi mong đợi, không được tính thành độ chính xác model. Restart/snapshot/cancel và barrier có metric riêng, không tính vào bảng turn latency.

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

Các ca chọn POI bằng số/tên, số ngoài danh sách, policy, consent, replay/collision, memory/restart, stream, journey A + draft B, D cũ sau P và hai client có revision cũ đều đạt oracle đã khai báo. P vượt request model/map còn chờ và kết quả navigation cũ bị chặn. Tuyến 10.000 điểm phục hồi được trong snapshot; geometry không nằm trong checkpoint hội thoại.

## Lỗi còn lại: câu hỏi khứ hồi về Sầm Sơn

Kịch bản `sam-son-deictic-followup`: lập kế hoạch đi Sầm Sơn → “chọn số 1” → “tôi sẽ đến đấy chơi một chút xong quay về. lúc về cần sạc không”. Vị trí fixture được cấp trong request; xe vẫn đứng yên.

Expected: trip_kind = round_trip và hai waypoint routes. Actual: trip_kind = one_way, chỉ có một waypoint route; trả “Mình chưa có bằng chứng phù hợp phiên bản xe trong cẩm nang.” Trace profile fixture cho thấy semantic chuyển sang manual.search. Ca này thất bại 6/6 lần. **Lượt benchmark này không tái hiện HTTP timeout; lỗi xác nhận được là routing/ngữ cảnh.**

Ca đối chứng `sam-son-followup-semantic-contract` cấp semantic trip.plan round_trip đúng, sau đó xác nhận địa điểm mới theo hợp đồng của trip.plan. Ca đối chứng đạt 3/3, tính được hai chặng mà không tự bắt đầu chạy. Điều này chứng minh graph xử lý được semantic khứ hồi; chưa chứng minh thao tác sửa kế hoạch giữ nguyên ID/địa điểm đã chọn, và chưa chứng minh model thật hiểu câu tự nhiên.

Hướng sửa tiếp theo:

1. Ưu tiên ngữ cảnh trip đang xem cho các tham chiếu “đấy/đó”, “quay về/lúc về” và câu hỏi năng lượng; không đẩy câu này sang manual.search.
2. Chốt contract cập nhật trip_kind/năng lượng trên plan ID + revision hiện tại, giữ điểm đã chọn; hỏi lại khi thiếu hoặc có nhiều target. Tránh biến follow-up thành yêu cầu đích mới vô điều kiện.
3. Dùng chính ca tự nhiên này làm gate trước khi sửa; bổ sung paraphrase, ngữ cảnh đã end/cancel, draft B cạnh journey A và provider timeout. Giữ ca đối chứng semantic tách biệt.
4. Sau khi gate offline đạt, đo model/VietMap thật và HTTP/browser trên phiên thử biệt lập; lưu decision source và timeout stage để không nhầm fallback với thành công của model.

## Benchmark P/D

Ba tải, mỗi tải 5 warmup + 100 thao tác xen kẽ P/D trên hành trình đã nhận: 50 mẫu P và 50 mẫu D. Tất cả receipt completed; trạng thái driving khớp từng lệnh. Model/map bị giữ bằng barrier và phải còn pending suốt phép đo.

| Tải | Tổng mẫu | Backend p50 / p95 (ms) | P p95 (ms) | D p95 (ms) | Budget p95 (ms) |
|---|---:|---:|---:|---:|---:|
| normal | 100 | 29.41 / 41.79 | 44.41 | 41.79 | 300 |
| model_pending | 100 | 29.83 / 40.23 | 40.52 | 39.56 | 500 |
| vietmap_pending | 100 | 27.33 / 37.54 | 38.48 | 35.66 | 500 |

Đạt budget backend 300 ms bình thường / 500 ms khi dependency chờ. Số đo gồm orchestrator, navigation domain, receipt SQLite và turn ledger; **không gồm HTTP, click, render/browser hoặc mạng thật**. Chưa kết luận mục tiêu phản hồi trực quan P ≤ 50 ms hoặc click-to-confirm đã đạt trên revision này.

## Kiểm tra code và phần chưa nghiệm thu

- Eval + memory/RAG: 50 tests passed; eval + voice regression: 78 tests passed (8 eval tests chạy lại trong cả hai nhóm, không cộng thành 128 test khác nhau).
- Frontend Node: 67 tests passed. Đây là regression code/DOM fixtures, không phải browser benchmark.
- Audit sau khi chuyển dữ liệu: 63 tests collect thành công; chưa chạy lại toàn bộ các ca audit này.
- Voice runner retained: smoke rules 1 case đạt, không lỗi; chỉ xác minh đường runner/shutdown, không dùng làm điểm chất lượng audio.
- Ruff và git diff --check đạt. Chưa chạy full backend suite; baseline failure từ các đợt trước không được đổi thành pass của lượt này.

Chưa đo: model NLU thật, STT/TTS, VietMap search/route thật, SDK/style/tile/retry, HTTP timeout/reconnect, browser E2E/responsive/frame latency, vòng sạc có station/charging progress, progress cạnh replace route, effect chưa rõ sau adapter lỗi, shutdown tại thời điểm ghi checkpoint và multiworker. Artifacts UI/map cũ còn giữ ở ../artifacts/navigation-runtime, có thời điểm riêng và không cộng vào nghiệm thu mới.

## Dọn eval

Đã xóa 71 file bị thay thế: 70 file trong eval và một test của runner cũ; chuyển 4 audit JSONL còn dùng sang tests/fixtures/dialogue_audits và sửa các đường dẫn regression. Manifest liệt kê đầy đủ.
Giữ RAG golden/baseline, memory, voice/conversation/task và các công cụ UI/map/replay chuyên biệt còn dùng. Các runner memory/voice được thêm đóng graph trong finally để không để tài nguyên SQLite còn sống khi xóa thư mục tạm.

Các baseline RAG [dev](local-dev.md)/[test](local-test.md) là lịch sử trên golden riêng, không phải số đo lại của revision này. Report trung gian bị thay thế vẫn nằm trong lịch sử Git. Tài liệu/đường dẫn runner chính đã cập nhật theo bộ eval mới.
