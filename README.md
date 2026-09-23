# ViVi Space Concept

Prototype giao diện trợ lý ô tô: bầu trời đêm, lõi sáng, hai dải LED được chiếu từ hình học 3D lên Canvas, chuyển động theo trạng thái tương tác.

## Chạy ViVi local

```sh
python3.12 -m venv .venv-ai
.venv-ai/bin/python -m pip install -r requirements-ai.txt
cp .env.example .env
.venv-ai/bin/python run.py
```

Mở http://127.0.0.1:8787 (hoặc cổng `VIVI_PORT` trong `.env`). Backend chỉ bind `127.0.0.1`. ZeroTTS, voice VIVI và PhoWhisper được nạp/warm trong lúc backend khởi động; server chỉ sẵn sàng khi hoàn tất. Lần đầu khởi động có thể tải model; các lần sau dùng cache local. Có thể đặt `PHOWHISPER_PRELOAD=false` nếu ưu tiên khởi động nhanh hơn độ trễ của lượt nói đầu tiên. Font Google Fonts có fallback font hệ thống khi offline.

Voice ZeroTTS của ViVi nằm trong `voices/VIVI.zip` với ID `VIVI` (`ZEROTTS_VOICE=VIVI`). Pack này chứa giọng community “Mai Chi”; backend nạp trực tiếp từ ZIP, không cần cài voice vào thư mục người dùng.

Backend hiện yêu cầu cài `requirements-ai.txt` và có voice pack VIVI để khởi động. Nếu thiếu model/dependency/voice pack, quá trình startup sẽ báo lỗi thay vì chạy với TTS chưa sẵn sàng. `/api/v1/health` trả `tts.detail=loaded` khi model đã ở trong bộ nhớ.

## Các luồng có thể thử

- Nhấn mic một lần để bắt đầu thu âm. ViVi tự dừng sau khoảng lặng hoặc khi nhấn mic lần nữa.
- Nhập “Đặt nhiệt độ 25 độ”, “Giảm nhiệt độ 2 độ”, “Mở cửa sổ bên tài”, “Đóng cửa sổ bên tài”, “Phát nhạc thư giãn”, “Dừng nhạc”, “Trạng thái xe”.
- Chuyển sang “Đang lái xe” rồi thử mở cửa sổ để thấy policy minh họa chặn lệnh.
- Nhập “Đặt nhiệt độ 35 độ” để thử kiểm tra giới hạn.
- Mở cẩm nang để xem cách trình bày câu trả lời với nguồn của prototype.
- Bật “Mai Chi” để phát phản hồi VIVI theo luồng từ ZeroTTS local; có thể tắt giọng để ngắt phát. SpeechSynthesis của trình duyệt là fallback nếu TTS chưa phát được.
- Mở thiết lập để giảm chuyển động. Giao diện cũng tôn trọng prefers-reduced-motion.

## Phạm vi

Đây là concept độc lập, chưa phải sản phẩm VinFast chính thức. Xe vẫn là simulator; policy chặn mở cửa khi lái chỉ là quy tắc demo. Audio, transcript và metadata được lưu trong `data/` cho đến khi người vận hành tự xóa. Thư mục này không được commit.

## Provider LLM

Đặt provider mặc định trong `.env`, sau đó có thể đổi model hội thoại ngay trên giao diện mà không cần khởi động lại backend. Danh sách UI chỉ cho chọn các provider đã cấu hình; model ID và khóa API vẫn được quản lý trên backend.

- `LLM_PROVIDER=rules`: mặc định, chạy offline ngay và hữu ích cho test.
- `LLM_PROVIDER=local`: API local tương thích OpenAI Chat Completions; cấu hình `LOCAL_LLM_BASE_URL` và `LOCAL_LLM_MODEL`.
- `LLM_PROVIDER=openai`: OpenAI Responses API; cấu hình `OPENAI_API_KEY` và `OPENAI_MODEL`.
- `LLM_PROVIDER=google`: Gemini API; cấu hình `GOOGLE_API_KEY` và `GOOGLE_MODEL`.

OpenAI/Google cần mạng. Chỉ `rules` và `local` đáp ứng runtime offline. Nếu provider được chọn nhưng cấu hình thiếu, server khởi động an toàn bằng `rules` và báo chi tiết ở `/api/v1/health`.

Để ViVi trả lời câu hỏi tự nhiên (ví dụ “Bạn là ai?”), chọn `openai`, `google` hoặc `local` trên UI. `rules` chỉ xử lý kịch bản cố định. Sau khi thay đổi các model ID hoặc khóa trong `.env`, cần khởi động lại backend. `/api/v1/health` cho biết provider mặc định và các lựa chọn khả dụng; `POST /api/v1/turn` nhận `llm_provider` tùy chọn cho từng lượt. Nếu giao diện báo backend offline thì nó sẽ dùng kịch bản demo tại trình duyệt. Lệnh điều khiển xe vẫn phải qua safety gateway và vehicle simulator; phản hồi hội thoại không thực hiện thao tác xe.

## API local

- `GET /api/v1/health`
- `POST /api/v1/stt` — multipart audio, `session_id`, `turn_id`
- `POST /api/v1/turn` — transcript và trạng thái xe
- `POST /api/v1/tts` — text sang WAV Mai Chi
- `POST /api/v1/turn/stream` — NDJSON gồm các mệnh đề hội thoại có thể đọc sớm và một sự kiện `final` chứa kết quả chính thức. Lệnh điều khiển xe không phát trước khi qua safety/acknowledgement.
- `POST /api/v1/tts/stream` — PCM mono 16-bit little-endian theo luồng; sample rate ở header `X-ViVi-Sample-Rate`, giọng ở `X-ViVi-Voice`. Frontend nhận các chunk để đệm và phát liên tục.

Frontend giữ khoảng 1 giây audio trong bộ đệm trước khi phát để tránh hụt tiếng giữa các chunk đầu của ZeroTTS. Câu trả lời ngắn hơn được phát ngay khi tổng hợp xong; tắt giọng vẫn hủy luồng và playback.

## Kiểm thử

```sh
.venv/bin/python -m unittest tests.test_core -v
node --check app.js
```

## Cấu trúc

- `index.html`: bố cục, điều khiển và thông tin trạng thái.
- `style.css`: ngôn ngữ thị giác, layout responsive, reduced motion.
- `app.js`: render không gian, trạng thái hội thoại, Digital Twin trong bộ nhớ, policy demo.
- `server/`: orchestration API, provider adapters, safety gateway, storage và vehicle simulator.
- `tests/`: test contract, safety, idempotency và API.

Pipeline hiện tại: thu âm → PhoWhisper → LLM/rules → safety gateway → vehicle simulator → verify → ZeroTTS. PhoWhisper được preload/warm và dùng FP16 tự động trên MPS/CUDA. Với hội thoại, LLM chuyển từng mệnh đề hoàn chỉnh sang hàng đợi ZeroTTS ngay khi sinh ra; với thao tác xe, chỉ báo thành công sau acknowledgement từ vehicle adapter thật.
