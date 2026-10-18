# Chọn đề xuất và timeout Sầm Sơn — 11/10/2026

## Bằng chứng từ phiên đang dùng

- 02:27 UTC: “cái thứ 2”, “cái thứ hai”, “chọn số 1” bị model tạo `navigation.refine` với `choice_handle=place:…`. Guard báo lựa chọn cũ, dù các POI vẫn còn. Lượt “chọn số 1” còn bị model gán POI thứ hai.
- 02:33 UTC: “bãi biển sầm sơn” hoàn tất trong 21.988 ms.
- Lượt tiếp theo: “tôi sẽ đến đấy chơi một chút xong quay về. lúc về cần sạc không” có ledger `interrupted`. Checkpoint dừng trước response: diễn giải 5.346 ms, capability 31.217 ms, gọi tuyến cộng dồn 9.962 ms. Kế hoạch đã commit nhưng chưa có response cuối. Checkpoint cuối khoảng 835 KB do task/turn/output chứa lại geometry.

Trace cũ không giải thích hết thời gian capability. Không quy toàn bộ 31 giây cho VietMap hay SQLite. Log lỗi mới ghi stage, turn ID, dispatch và thời gian dependency để phân biệt lần chậm sau.

## Thay đổi

- Số thứ tự và tên POI rõ ràng được giải tại language boundary theo danh sách hiện tại, trước khi gọi model. Hỗ trợ “cái thứ 2”, “cái thứ hai”, “thứ hai”, “chọn số 1”. Số không có hỏi lại; không chọn tùy ý. Task đang hỏi sở hữu câu trả lời; không lấy câu trả lời cho task khác làm lựa chọn POI.
- `place_handle` dùng cho `.select`; `choice_handle` dành cho `clarification.choices`. Prompt phiên bản 16 ghi rõ hợp đồng này. Guard sửa model sai khi lời nói chỉ rõ tham chiếu. Chọn POI giữ task/các bước còn lại và không cấp quyền bắt đầu.
- Geometry trong kết quả capability được thay bằng projection gọn ở checkpoint/task/turn/steps. Snapshot domain và response navigation chính vẫn có tuyến đầy đủ. Không xóa lịch sử, ledger hay sửa DB vận hành.
- Chi tiết/tuyến POI chạy batch tối đa 3 request, giữ thứ tự danh sách. Domain writer chỉ nhả/giữ một lần cho cả batch; P vượt thời gian chờ. Generation fence loại bỏ kết quả cũ sau P/đổi state. Lỗi code hủy request cùng batch, không bị biến thành lỗi mạng.
- Tuyến trong cùng capability được tái sử dụng theo tọa độ chính xác, tránh tính lại outbound sau quyết định POI. Cache kết thúc theo capability, không dùng route cũ giữa các lượt.
- Charging discovery nhả writer lúc đọc mạng. Các lượt đọc sạc dùng deadline còn lại, dành 5 giây cho commit/response/checkpoint/receipt. Thiếu dữ liệu trả kế hoạch chưa hoàn chỉnh, không tạo offer khởi hành.
- Stream timeout trả `turn_timeout`/`turn_id`; giao diện đồng bộ snapshot để hiện kế hoạch đã lưu. Log `turn_error` phân biệt timeout, hủy và lỗi; không ghi credentials/prompt hệ thống.

## Kiểm chứng

- 16 test mới trong `tests/test_navigation_followups.py` qua; 67 test frontend qua. Tuyến khứ hồi 10.000 điểm không đi vào checkpoint, vẫn đầy đủ trong snapshot; xe không tự chạy.
- Lần chạy cuối gồm API trước các fixture navigation để giữ runtime sạch: 154 pass, 3 lỗi charging trùng baseline, không có lỗi mới. [Kết quả regression](../../eval/artifacts/navigation-runtime/navigation-followups-regression.json).
- Bộ navigation/dialogue/trip liên quan: 149 pass, 5 lỗi trùng baseline. Nhóm charging có 3 lỗi trùng baseline. Không sửa assertion cũ để che vấn đề contract đã ghi ở báo cáo trước.
- Geometry từ log + semantic fixture: một chiều 389 ms, khứ hồi 1.045 ms. Khứ hồi thiếu trạm trong fixture trả incomplete, không tự đoán trạm.
- VietMap thật + semantic fixture: một chiều 1.387 ms, khứ hồi 14.311 ms; trả kế hoạch khứ hồi ready và một điểm sạc. Checkpoint khoảng 21/57 KB. Route cộng dồn 17.488 ms gồm request song song, không cộng trực tiếp vào wall time. Hai mẫu này không phải p95.
- Google thật chưa nghiệm thu. Lần tiếp tục gọi model bị automatic approval review từ chối vì gửi câu hỏi/ngữ cảnh từ log tới dịch vụ ngoài; cần chấp thuận rõ payload. Không coi fallback rules là kết quả Google.

Artifacts: [fixture](../../eval/artifacts/navigation-runtime/sam-son-followup-fixture.json), [VietMap thật với semantic fixture](../../eval/artifacts/navigation-runtime/sam-son-followup-live-map.json).

Replay bằng DB tạm, không gọi backend đang dùng:

```bash
.venv/bin/python -m eval.navigation_followups_replay
.venv/bin/python -m eval.navigation_followups_replay --live-vietmap
```

Script có `--live-model` để nghiệm thu Google sau chấp thuận, cần key thật. Chưa restart backend hoặc thay session người dùng trong lần sửa này.
