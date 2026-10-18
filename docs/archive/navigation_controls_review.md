# Ổn định chuyển P/D và đồng bộ navigation

> Tài liệu lịch sử. Runtime hiện tại và kết quả kiểm chứng mới nhất: [navigation_runtime.md](../navigation_runtime.md).

Ngày kiểm tra: 2026-10-10. Báo cáo này bổ sung, thay thế các kết luận về P/D và khả năng chạy khi Map SDK lỗi trong [báo cáo refactor trước](navigation_refactor_review.md).

## Thay đổi

- Xe, hành trình đã nhận và bản nháp tiếp tục được quản lý riêng. P/D chỉ tác động hành trình đã nhận; D không tự nhận bản nháp.
- Typed gear/pause/resume/end/clear/history/progress/charge dùng nhánh điều khiển ngắn trong workflow hiện có, cùng checkpoint và navigation reducer. Không gọi model hoặc tạo TTS cho nút chuyển số và telemetry. Giọng nói dùng capability `navigation.set_gear`, đi qua cùng bộ chuyển trạng thái.
- Writer theo phiên ưu tiên P trước các request đang chờ; D cũ bị P mới vượt qua không được thực thi. Model, tìm địa điểm và tính tuyến nhả writer khi chờ IO; kết quả bị thay thế phải kiểm tra generation trước khi ghi.
- Progress đang chờ được gộp; sạc chỉ cộng elapsed từ event chưa áp dụng. Lưu tất cả ID trong batch để replay không cộng lại thời gian sạc. Telemetry không làm mất câu trả lời model hoặc ghi đè tiến trình mới.
- Endpoint `GET /api/v1/navigation/snapshot?session_id=...` trả xe, workspace, trip plan và control receipts. Response/stream/snapshot có `runtime_epoch`, `snapshot_sequence`, `event_id`; epoch mới chỉ được nhận qua snapshot có thẩm quyền. Các event tạm của stream không ghi đè trạng thái đã xác nhận.
- Frontend dùng một bộ nhận snapshot và `GearController`. P đóng băng chuyển động ngay; số hiển thị vẫn là số đã xác nhận. P trong lúc chờ D được giữ để gửi tiếp, không bật chuyển động giữa hai request. Sau 3 giây hiển thị đồng bộ; sau 8 giây đối chiếu snapshot/receipt. Kết quả chưa rõ giữ xe đứng yên.
- Điều khiển xe được cập nhật trước khi tải bản đồ. Map SDK lỗi không chặn chuyển số, route controller hoặc cảnh 3D. Đồng hồ sạc độc lập với bản đồ và vòng animation; nội suy không giật lùi bởi progress cũ.
- Geometry được nhận theo danh tính tuyến; không stringify toàn bộ geometry mỗi tick. Giữ trạng thái ẩn panel, focus và vùng cuộn; màn hình P vẫn đầy chiều rộng.
- Log điều khiển ghi loại sự kiện, đích số, event ID, trạng thái trước/sau, queue/control latency và lý do từ chối. Control receipt không bị các cập nhật telemetry đẩy ra khỏi danh sách.

Không thay đổi chất lượng tìm địa điểm trong mốc này. Các chỉnh sửa sẵn có trong workspace được giữ nguyên.

## Kiểm thử

Backend regression cuối: **142 passed** trên controls, workspace, lifecycle, workflow, trip plans, runtime/follow-up review, dialogue và API routes. Có cảnh báo deprecation Pydantic hiện có trong trips; không có test failure.

Kiểm tra core/streaming/endpoint cũ chạy riêng: **36 passed**. Ruff và `git diff --check` cũng qua.

Các ca mới dùng graph, SQLite checkpoint và simulator thật, kiểm tra:

- P trong khi model hoặc VietMap đang chờ, không thực thi proposal/bản nháp cũ.
- P vượt D đang xếp hàng; pause/resume/restart và receipt/replay.
- Progress trong lúc model trả lời không bị lùi hoặc huỷ câu trả lời.
- Gộp progress, ưu tiên P; gộp sạc với ID trùng, ID đã áp dụng và replay từng thành viên batch.
- Giọng nói P/D dùng đúng tuyến đã nhận; phủ định không tạo lệnh D.

Frontend: **49 assertions/cases** trong 5 nhóm navigation, gear, barge-in, confirmation và workflow. Node báo 5 file test vì các file dùng runner nội bộ.

Browser layout dùng HTML/CSS/controller thật với backend/map fixture: 1440×900, 1024×768, 390×844 và 390×568; kiểm tra hide/reopen, scroll, chọn thẻ và không che điều khiển.

Browser P/D E2E dùng FastAPI, graph, checkpoint, ledger và WebGL thật; dữ liệu tuyến xác định và provider fixture. Desktop 1440×900 và mobile 390×844 đều qua: nút P/D thật, free-drive, nhận hành trình, cảnh 3D, D phản hồi chậm rồi P/bấm đôi. Map SDK bị chặn chủ động. E2E bắt được và đã sửa hai lỗi mà mock không phát hiện: nút P bị disable trong khoảng pending D và native timer gọi sai receiver.

## Đo thời gian và giới hạn

| Phép đo | Mẫu | p95 |
| --- | ---: | ---: |
| Backend trực tiếp, gồm orchestration/checkpoint/ledger | 30, sau 5 warmup | 38.59 ms |
| Queue của phép đo trực tiếp | 30 | 1.82 ms |
| Bộ chuyển trạng thái của phép đo trực tiếp | 30 | 5.77 ms |
| Ledger commit của phép đo trực tiếp | 30 | 6.46 ms |
| Backend khi browser/WebGL chạy, desktop / mobile | 8 mỗi viewport | 143.62 / 195.51 ms |
| Handler P tới cập nhật DOM dừng, desktop / mobile | Browser fixture | 11.4 / 8.9 ms |

Đây là số đo trên máy phát triển, một worker, DB tạm và tuyến fixture; p95 dùng nearest-rank. Không thay thế latency trên thiết bị IVI. Handler-to-DOM không phải thời gian frame thực sự được GPU trình bày.

Phép đo click-to-ack DOM của lần browser đã chạy trộn cả ca cố tình trì hoãn D và startup: desktop 3360.4 ms, mobile 300.1 ms. Không dùng số đó để kết luận latency bình thường đã đạt mục tiêu; script đã tách/reset cửa sổ đo, nhưng chưa chạy lại phép đo render này.

Browser sử dụng SwiftShader GPU phần mềm. Chụp màn hình WebGL bị timeout; không có ảnh 3D từ lượt E2E này. Đã xác minh controller/canvas WebGL và chuyển trạng thái, chưa nghiệm thu độ mượt/thẩm mỹ trên GPU, màn hình và nút của IVI thật. Live VietMap, model cloud/local, MQTT và phần cứng audio cũng chưa thử.

## Chạy lại và đưa vào sử dụng

- Backend: `.venv/bin/python -m pytest -q tests/test_navigation_controls.py tests/test_navigation_workspace.py tests/test_navigation_lifecycle.py tests/test_navigation_workflow.py tests/test_trip_plans.py tests/test_navigation_runtime_review.py tests/test_navigation_followup_review.py tests/test_dialogue_v2.py tests/test_api/test_routes.py`.
- Frontend: `node --test tests/test_frontend_navigation.cjs tests/test_frontend_gear.cjs tests/test_frontend_bargein.cjs tests/test_frontend_confirmation.cjs tests/test_frontend_workflow.cjs`.
- Benchmark: `PYTHONPATH=. .venv/bin/python eval/navigation_controls_benchmark.py`.
- E2E: `VIETMAP_API_KEY= PYTHONPATH=. .venv/bin/python eval/navigation_control_server.py`, sau đó chạy `node eval/navigation_control_browser.cjs` với Playwright và Three.js 0.169.0 khả dụng qua Node resolution/`NODE_PATH`. Server test dùng DB tạm, port riêng 8768.

Chưa restart backend người dùng, deploy hoặc sửa DB hội thoại thật. Khi áp dụng cần restart backend rồi reload trang để nhận endpoint và module JS mới. Cơ chế backup checkpoint trước migration hiện có được giữ; không phát lại hành trình cũ. Cần một vòng trên IVI thật trước khi kết luận đạt toàn bộ tiêu chí độ mượt và latency hiển thị.
