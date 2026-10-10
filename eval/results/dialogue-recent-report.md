# Kiểm tra log 898–909 và sửa lỗi hội thoại

Nguồn: đọc SQLite `data/vivi_rag.sqlite3` ở chế độ read-only, 12 lượt mới sau
đợt kiểm tra 854–897, cùng session, ngày 2026-10-09. Không sửa history, memory
hay ledger trong đợt này. Log mới chỉ có hội thoại và điều khiển xe; không có
yêu cầu dẫn đường để đánh giá thêm navigation thực tế.

## Lỗi và nguyên nhân

| Lượt | Biểu hiện | Nguyên nhân | Sửa |
|---|---|---|---|
| 899 | “mở cửa” vẫn hỏi “mở hay đóng … vị trí nào” | Câu hỏi mặc định của tool dùng cho cả lỗi thiếu vị trí, dù `open=true` đã biết | Câu hỏi dựa vào slot thực sự thiếu và giữ trạng thái mở/đóng đã biết |
| 902 | “không, cửa sổ phía trước” vẫn preview mở cửa tài | Grammar bổ sung vị trí lấy capability từ task cũ, bỏ danh từ window; semantic guard cũng dùng frame sai này | Danh từ mới quyết định capability trước khi hợp nhất vị trí; registry chuyển nghĩa mở cửa → kính 100%, chỉ giữ trường tương thích |
| 906 | “tăng lên 3 độ” bị đặt 3°C và báo ngoài phạm vi | Model trả giá trị tuyệt đối; parser chưa hiểu dạng tương đối có “lên”, nên guard không có frame đúng để kiểm tra | Grammar tăng/giảm số độ với lên/xuống và °C; kiểm tra bằng chứng số và sửa absolute/delta tại biên diễn giải |
| 907, 909 | Đã đổi sang “tăng thêm 3” hoặc “đặt 25” vẫn hỏi lại | Reducer merge cộng dồn, giữ đồng thời `value_celsius` và `temperature_delta_celsius`; registry từ chối hai mode | Registry khai báo nhóm tham số loại trừ nhau; mode mới thay mode cũ, gồm cả checkpoint đã bị lưu sai |
| 908 | “25 độ và phát nhạc” bị hỏi “sửa bước nào” | Reducer chỉ nhận đúng một draft khi modify; parser cũng loại frame modify trong câu ghép | Hợp nhất các bước chưa chạy liên tiếp, kiểm tra cả tập trước khi sửa, giữ task/step IDs và tăng revision một lần |

Các lượt 898, 900, 901, 903, 904 và 905 không chứng minh có thực thi sai. Lượt
901 được model xử lý đúng trong log nhưng parser chưa có grammar tự sửa
“phía trước à không phía sau”; bổ sung để tầng offline xử lý nhất quán. Các lượt
902–904 trong log đều còn chờ xác nhận, không có bằng chứng cửa đã thực sự mở.

Prompt `semantic-unified-7` làm rõ thay capability theo danh từ, sửa vị trí từng
phần, relative/absolute temperature và modify nhiều bước. Grammar tiếng Việt vẫn
nằm trong parser; reducer/registry không đọc keyword hoặc nội dung câu trả lời.

## Hành vi sau sửa

- Mở cửa → bên trái → phía trước à không phía sau: chờ xác nhận cửa sau trái.
- Không, cửa sổ phía trước: chuyển sang kính trước trái 100%, thu hồi grant cũ.
- Tăng điều hoà và bật nhạc → tăng lên 3 độ: từ 23°C lên 26°C, rồi phát nhạc.
- Tăng thêm 3 độ C ấy: tạo yêu cầu mới sau hoàn tất, tăng lên 29°C.
- 25 độ và phát nhạc: đặt 25°C; khi còn task pending thì sửa cùng task, khi task
  đã hoàn tất thì tạo yêu cầu mới, không phát lại bước cũ.
- Checkpoint chứa cả giá trị sai 3°C và delta +3 được phục hồi bằng “đặt nhiệt độ
  25”; bước nhạc còn lại tiếp tục, không cần xoá task thủ công.
- Modify nhiều bước không khớp phải hỏi lại và thu hồi consent cũ; đồng ý hoặc
  dùng nút xác nhận cũ không chạy thao tác. Replay cùng turn ID không gửi trùng.

## Kiểm thử

Fixture `eval/audit_898_909.jsonl` giữ câu hỏi và semantic output **đã ghi lại**,
không chứa session/confirmation ID hay GPS. Replay cả 12 lượt qua parser và
provider google/local mock, với oracle về capability, vị trí, nhiệt độ, thứ tự,
revision, consent và effects. Mocks phát lại chính output lỗi cũ, không dùng parser
để giả lập output đúng; đây là regression của guard/reducer, không phải phép đo
NLU của model local.

`tests/test_dialogue_audit_recent.py`: 15 test mới, gồm replay ba tầng, sửa mode,
sửa nhiều bước, paraphrase, restart và consent/replay. Đã tái hiện 9 test thất bại
trước sửa; sau sửa toàn bộ pass. Có 92 test pass khi gộp log mới, log 854–897,
dialogue và navigation; sau bổ sung consent còn có 68 test mục tiêu pass.

Lượt xác minh cuối: **448/448 test full suite pass**, gồm MQTT cục bộ, trong
200.34 giây. Bốn file test frontend (confirmation, barge-in, navigation, workflow)
pass với Node; Ruff và `git diff --check` pass. Hai assertion về cụm “vị trí”
trong lượt full suite trước đã được xử lý bằng câu hỏi rõ hơn “Bạn muốn mở cửa
ở vị trí nào?”, không thay hoặc bỏ assertion kiểm tra hành vi.

Dataset riêng `eval/dialogue_recent_cases.jsonl`: ba episode, 15 lượt, gồm câu từ
log và paraphrase. Chạy orchestrator/API contract với DB tạm và VehicleSimulator:

| Tầng thực sự phục vụ | Lượt đạt | Episode đạt | Hỏi lại | p50/p95 latency | Thực thi sai theo oracle |
|---|---:|---:|---:|---|---:|
| Parser | 15/15 | 3/3 | 4 | 178.30 / 295.51 ms | 0 |
| Google + semantic guard | 15/15 | 3/3 | 4 | 2161.55 / 5898.75 ms | 0 |

Google phục vụ cả 15 lượt, không fallback; 8 lượt có guard ghi nhận thay đổi
semantic. Không diễn giải con số này thành số lỗi NLU vì guard cũng chuẩn hoá
handle/text. Số liệu là development regression đã dùng để sửa, không phải độ
chính xác trên tập độc lập. Local chỉ kiểm tra bằng captured-output mock, chưa
đo Qwen thật. Thời gian trên là `latency_ms.total` của orchestrator.

Kết quả chi tiết: `dialogue-recent-parser.json`, `dialogue-recent-google.json`.
Không restart/deploy backend trong đợt này; cần nạp code mới để phiên đang chạy
hiển thị prompt version 7. Không cài model, thay dữ liệu xe/trạm hoặc gọi xe thật.
