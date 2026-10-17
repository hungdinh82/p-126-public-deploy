# Workflow Core v2

Nguồn tự động: [core_cases.jsonl](core_cases.jsonl). Các input này kiểm workflow với
semantic được viết độc lập; chất lượng hiểu câu đo bằng [semantic eval](semantic_cases.jsonl).

| ID | Invariant |
|---|---|
| navigate-then-music | Preview chưa lái/nhạc; consent start mới chạy cả hai |
| itinerary-then-music | Tính xong là chạy nhạc; chưa tự lái |
| conditional-no-prefix | Câu có điều kiện không thực thi tiền tố |
| offline-compound-preserves-task | Câu ghép không bị fallback cắt nghĩa; giữ question/task |
| offline-bound-approval | Đồng ý toàn câu bind đúng question khi model lỗi |
| stream-replay-collision | Stream/HTTP replay thống nhất; payload khác cùng ID bị chặn |
| restart-does-not-drive | Restart giữ route, park, không gọi lại model hay tự lái |
| new-draft-keeps-live-journey | Draft B không sửa journey A; reply cũ bị chặn |
| place-selection-ordinal | Chọn handle server cấp; chọn POI không cấp consent start |
| sam-son-deictic-followup | Semantic khứ hồi tạo hai chặng; chưa tự lái |

Các invariant bổ sung trong `tests/integration/test_core_*`: pin/vị trí/cơ sở tính
lại thu hồi consent; thiếu POI giữ tên khi bổ sung vùng; P khi map/model chờ;
unknown effect không gửi lại; crash sau effect đối chiếu ledger; geometry không
nằm trong checkpoint; telemetry không sửa draft/checkpoint; memory qua restart.
Xem [README](README.md) để biết phương pháp và giới hạn phép đo.
