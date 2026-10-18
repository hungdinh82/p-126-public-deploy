# Câu ngắn và form nhiều lượt

85 ca độc lập, tổng 260 lượt; không gộp điểm với baseline 834 ca.

- Mở/khóa cửa, điền vị trí rồi xác nhận.
- Tên vai trò: ghế lái, ghế tài xế, bên tài, ghế phụ.
- Tọa độ: trước/sau, trái/phải; điền hàng rồi phía hoặc phía rồi hàng.
- Trả lời gộp vị trí hoặc không dấu.
- Sửa một chiều tọa độ trước khi bổ sung chiều còn thiếu.
- Than lạnh rồi trả lời số độ bằng số, chữ hoặc cụm ngắn.
- Khóa mọi cửa/tất cả cửa rồi xác nhận ngắn.

Mỗi lượt chưa đủ vị trí phải tiếp tục hỏi và không thay đổi xe. Lượt có đủ vị trí phải yêu cầu xác nhận; chỉ sau xác nhận host mới được thay đổi đúng cửa. Expected được viết trước khi gọi model.

Kết quả nằm tại `../results/jetson-short-turns/` và `../results/offline-short-turns/`.
Đây là bộ development bổ sung, chưa phải holdout độc lập. Các câu này không được đưa vào prompt.

Chạy lại từ gốc repo (output mới theo timestamp):

```bash
PYTHONPATH=. .venv/bin/python scripts/benchmark_agent_supplemental.py --mode fixture
PYTHONPATH=. .venv/bin/python scripts/benchmark_agent_supplemental.py --mode live --provider local
```

`--output <folder> --resume` tiếp tục cùng ca và cùng hash dataset. HTML chỉ ghi lỗi; JSON giữ chi tiết.
