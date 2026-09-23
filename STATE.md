# Trạng thái dự án ViVi

> Snapshot: 2026-09-22  
> Mục đích: Ghi lại sự thật hiện tại của repo. Cập nhật file này sau mỗi mốc tích hợp.

## Tổng quan

ViVi hiện là một prototype giao diện web độc lập. Giao diện thể hiện trợ lý dạng không gian, state hội thoại và Digital Twin mô phỏng, nhưng chưa kết nối microphone, STT, LLM, TTS bên ngoài hoặc xe thật.

## Những gì đang có

| Thành phần | Trạng thái hiện tại | Vị trí |
|---|---|---|
| Giao diện và responsive layout | Hoạt động | `index.html`, `style.css` |
| Orb/ribbon không gian và animation | Hoạt động bằng Canvas 2D | `app.js` |
| State UI nghe → nghĩ → thực hiện → nói | Mô phỏng bằng timer | `app.js` |
| Nhận lệnh chữ | Hoạt động | `app.js` |
| Hiểu intent | Luật regex cục bộ trong `resolveCommand()` | `app.js` |
| Digital Twin | Dữ liệu trong bộ nhớ trình duyệt | `app.js` |
| Safety policy | Quy tắc demo ở frontend | `app.js` |
| Microphone | Click-to-record, auto-stop theo khoảng lặng | `app.js` |
| STT / PhoWhisper medium | Đã tải model và nhận diện thật thành công | `server/adapters/stt.py` |
| LLM API | Adapter OpenAI, Google, local và rules đã có; model chưa khóa | `server/adapters/llm.py` |
| TTS / ZeroTTS local | Đã tích hợp và synthesis thật thành công | `server/adapters/tts.py` |
| Giọng community Mai Chi (`VIVI`) | Pack `voices/VIVI.zip`, WAV mono 48 kHz | `server/adapters/tts.py` |
| TTS trình duyệt | Có `SpeechSynthesis` tùy chọn, phụ thuộc thiết bị | `app.js` |
| Backend/API | FastAPI local hoạt động tại `127.0.0.1:8787` | `server/`, `run.py` |
| Môi trường backend | Đã chốt local/offline, chỉ `localhost` trong MVP đầu | — |
| Chế độ thu âm | Đã chốt click-to-record, không cần giữ nút | — |
| Lưu audio/transcript | Đã chốt lưu vô thời hạn, người vận hành tự xóa; chưa triển khai | — |
| Quản lý secret | Chưa cần ở prototype; chưa có cơ chế backend | — |
| Kết nối xe thật | Chưa có | — |
| Automated tests trong repo | 8 test core/API đang đạt | `tests/test_core.py` |

## Hành vi demo hiện tại

- Nhiệt độ được giới hạn từ 16–30°C.
- Có thể mở/đóng cửa sổ bên tài, điều khiển trạng thái nhạc và đọc trạng thái xe mô phỏng.
- Quy tắc minh họa chặn mở cửa sổ khi bật chế độ đang lái.
- Thành công hiện được xác nhận sau khi cập nhật biến trong bộ nhớ, không phải sau acknowledgement từ xe.
- Reload trang sẽ đặt lại toàn bộ trạng thái.

## Khoảng cách đến pipeline thật

1. Không có audio capture, VAD, resampling hoặc upload audio.
2. Không có backend để giữ secret và điều phối dịch vụ.
3. Không có session/turn identity, retry, timeout hoặc idempotency.
4. Không có schema chuẩn cho LLM tool call.
5. Safety policy đang ở client và có thể bị bỏ qua.
6. Không có vehicle adapter/acknowledgement thật.
7. Animation đang chạy theo thời gian chờ giả, chưa dựa trên event thật.
8. Không có quan sát độ trễ, lỗi theo chặng hoặc đánh giá chất lượng tiếng Việt.

## Mốc hiện tại

`M1 — Backend skeleton`: Hoàn tất backend, storage, safety gateway, simulator, UI integration và adapters. ZeroTTS và PhoWhisper đã chạy thật. OpenAI/Google/local đã có adapter nhưng chưa thể integration-test khi chưa có key/endpoint/model.

## Việc tiếp theo

- Benchmark PhoWhisper medium trên Windows GTX 1650 4 GB và MacBook Air M1; xác định CUDA/MPS/CPU/offload profile ổn định.
- Thiết kế cấu trúc thư mục dữ liệu, hiển thị dung lượng và hướng dẫn xóa thủ công.
- Kiểm tra điều khoản sử dụng giọng community Mai Chi ngoài phạm vi đánh giá/demo.
- Chốt contract của LLM local khi model được chọn.
- Sau khi chốt, thực hiện Giai đoạn 1: backend skeleton + fake adapters + chuyển safety policy ra server.

## Rủi ro đang mở

| Rủi ro | Ảnh hưởng | Cách giảm thiểu dự kiến |
|---|---|---|
| GTX 1650 chỉ có 4 GB VRAM; M1 Air không có CUDA | PhoWhisper medium có thể không nằm trọn VRAM hoặc backend tăng tốc không ổn định | Benchmark từng máy, cho phép CPU/offload fallback và không tải đồng thời model không cần thiết |
| Giọng community Mai Chi có thể chỉ phù hợp demo/evaluation | Rủi ro quyền sử dụng khi thương mại hóa | Xác nhận license/quyền sử dụng trước production |
| LLM chưa được chọn | Chưa chốt auth, schema support, streaming | Dùng interface trung lập và contract test |
| Policy chỉ là minh họa | Không đủ an toàn cho xe thật | Giữ vehicle simulator; review policy riêng trước tích hợp xe |
| Tiếng ồn cabin và giọng vùng miền | Giảm độ chính xác STT | Tạo tập đánh giá đại diện và đo theo intent/entity |
| Lưu audio/transcript vô thời hạn | Rủi ro riêng tư và đầy ổ đĩa | Không commit/sync, hiển thị dung lượng, tài liệu vị trí dữ liệu/xóa thủ công và thông báo thu âm |

## Nhật ký cập nhật

- **2026-09-22:** Tạo snapshot ban đầu; ghi nhận đề xuất PhoWhisper, LLM API chưa chọn và ZeroTTS giọng Ngọc Huyền (sau đó được thay bằng Mai Chi).
- **2026-09-22:** Chốt PhoWhisper medium trên macOS M1/Windows GPU, click-to-record, ZeroTTS local giọng Mai Chi (`maichi`), backend offline và lưu audio/transcript cục bộ.
- **2026-09-22:** Chốt Windows GTX 1650 4 GB VRAM, chỉ truy cập qua `localhost`, dữ liệu không tự hết hạn và do người vận hành tự xóa.
- **2026-09-22:** Triển khai FastAPI local, click-to-record, data store, safety gateway, vehicle simulator, OpenAI/Google/local/rules adapters, PhoWhisper và ZeroTTS adapters. 8 test đạt; ZeroTTS Mai Chi đã synthesis WAV thật.
- **2026-09-22:** Tải xong PhoWhisper medium 3,06 GB. Nhận diện đúng file TTS 1,68 giây thành “xin chào mình là vi vi.”; lượt warm-cache trên MacBook Air M1 mất khoảng 6,25 giây.
