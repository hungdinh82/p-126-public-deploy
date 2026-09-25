# ViVi Space Concept

Prototype giao diện trợ lý ô tô: bầu trời đêm, lõi sáng, hai dải LED được chiếu từ hình học 3D lên Canvas, chuyển động theo trạng thái tương tác.

## Chạy ViVi local

```sh
python3.12 -m venv .venv-ai
.venv-ai/bin/python -m pip install -r requirements-ai.txt
cp .env.example .env
.venv-ai/bin/python run.py
```

Mở http://127.0.0.1:8787. Backend chỉ bind `127.0.0.1`. Lần đầu dùng STT/TTS sẽ tải model; các lần sau có thể chạy offline từ cache. Font Google Fonts có fallback font hệ thống khi offline.

Voice ZeroTTS của ViVi nằm trong `voices/VIVI.zip` với ID `VIVI` (`ZEROTTS_VOICE=VIVI`). Pack này chứa giọng community “Mai Chi”; backend nạp trực tiếp từ ZIP, không cần cài voice vào thư mục người dùng.

Nếu chỉ phát triển backend/UI mà chưa cần model nặng:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python run.py
```

## Các luồng có thể thử

- Nhấn mic một lần để bắt đầu thu âm. ViVi tự dừng sau khoảng lặng hoặc khi nhấn mic lần nữa.
- Nhập “Đặt nhiệt độ 25 độ”, “Giảm nhiệt độ 2 độ”, “Mở cửa sổ bên tài”, “Đóng cửa sổ bên tài”, “Phát nhạc thư giãn”, “Dừng nhạc”, “Trạng thái xe”.
- Chuyển sang “Đang lái xe” rồi thử mở cửa sổ để thấy policy minh họa chặn lệnh.
- Nhập “Đặt nhiệt độ 35 độ” để thử kiểm tra giới hạn.
- Mở cẩm nang để xem cách trình bày câu trả lời với nguồn của prototype.
- Bật “Mai Chi” để phát phản hồi bằng ZeroTTS local; SpeechSynthesis của trình duyệt là fallback khi TTS chưa sẵn sàng.
- Mở thiết lập để giảm chuyển động. Giao diện cũng tôn trọng prefers-reduced-motion.

## Phạm vi

Đây là concept độc lập, chưa phải sản phẩm VinFast chính thức. Xe vẫn là simulator; policy chặn mở cửa khi lái chỉ là quy tắc demo. Audio, transcript và metadata được lưu trong `data/` cho đến khi người vận hành tự xóa. Thư mục này không được commit.

## Provider LLM

Chọn một provider trong `.env`:

- `LLM_PROVIDER=rules`: mặc định, chạy offline ngay và hữu ích cho test.
- `LLM_PROVIDER=local`: API local tương thích OpenAI Chat Completions; cấu hình `LOCAL_LLM_BASE_URL` và `LOCAL_LLM_MODEL`.
- `LLM_PROVIDER=openai`: OpenAI Responses API; cấu hình `OPENAI_API_KEY` và `OPENAI_MODEL`.
- `LLM_PROVIDER=google`: Gemini API; cấu hình `GOOGLE_API_KEY` và `GOOGLE_MODEL`.

OpenAI/Google cần mạng. Chỉ `rules` và `local` đáp ứng runtime offline. Nếu provider được chọn nhưng cấu hình thiếu, server khởi động an toàn bằng `rules` và báo chi tiết ở `/api/v1/health`.

## API local

- `GET /api/v1/health`
- `POST /api/v1/stt` — multipart audio, `session_id`, `turn_id`
- `POST /api/v1/turn` — transcript và trạng thái xe
- `POST /api/v1/tts` — text sang WAV Mai Chi

## Kiểm thử

```sh
.venv/bin/python -m unittest discover -s tests -v
node --check app.js
```

## Cấu trúc

- `index.html`: bố cục, điều khiển và thông tin trạng thái.
- `style.css`: ngôn ngữ thị giác, layout responsive, reduced motion.
- `app.js`: render không gian, trạng thái hội thoại, Digital Twin trong bộ nhớ, policy demo.
- `server/`: orchestration API, provider adapters, safety gateway, storage và vehicle simulator.
- `tests/`: test contract, safety, idempotency và API.

Pipeline hiện tại: thu âm → PhoWhisper → LLM/rules → safety gateway → vehicle simulator → verify → ZeroTTS. Khi nối xe thật, chỉ báo thành công sau acknowledgement từ vehicle adapter thật.

## Handbook RAG preview (`src/`)

Nhánh handbook RAG nằm trong `src/`; nhánh action dùng bridge để tái sử dụng safety policy,
confirmation store và vehicle simulator trong `server/`. Phạm vi cẩm nang hiện tại là VF8
đời 2026, output có thể xem dạng text trong terminal hoặc JSON qua API.

```sh
.venv/bin/pip install -r requirements-rag.txt

# Tải đủ cây 56 chương/tab từ public Owner's Manual API.
.venv/bin/python -m src.cli.crawl_manual

# Parse, chunk, gọi Google embedding và lưu Chroma local.
.venv/bin/python -m src.cli.build_index

# Transcript thô (giống output cuối của STT) → LangGraph → RAG → Gemini → citation.
.venv/bin/python -m src.cli.ask \
  --text "Sạc pin VF8 thế nào?" \
  --session demo-text

# Lệnh điều khiển đi qua safety gateway, simulator và verify.
.venv/bin/python -m src.cli.ask \
  --text "Đặt nhiệt độ 25 độ" \
  --session demo-action

# Hội thoại nhiều lượt, có SQLite history.
.venv/bin/python -m src.cli.ask --interactive --session demo-interactive
```

Google free-tier giới hạn số embedding theo phút/ngày. `build_index` lưu từng batch,
tái sử dụng vector theo checksum và có thể chạy lại để resume. Khi quota embedding chưa
reset, dùng retrieval BM25 local trên toàn bộ corpus; Gemini vẫn sinh và kiểm tra câu trả lời:

```sh
.venv/bin/python -m src.cli.ask \
  --retrieval lexical \
  --text "Cách bật chế độ cắm trại?"
```

Mọi câu trả lời phải ánh xạ claim tới `source_id` đã retrieve. Citation không tồn tại,
thiếu bằng chứng, câu hỏi thương mại hoặc yêu cầu can thiệp hệ thống nguy hiểm đều dẫn
đến abstain. Snapshot, Chroma index và SQLite history nằm trong `data/` và không được commit.

### Contract LangGraph hiện tại

Input chính là transcript cuối từ STT trong trường `input_text`; JSON đã phân loại qua
`--input` chỉ còn được giữ để tương thích fixture cũ. Graph tự phân loại và trả một envelope:

- `response_text` và `tts_text`: text để hiển thị/phát giọng nói.
- `action_proposal`: intent cùng arguments đã kiểm tra kiểu/range.
- `execution`: kết quả allow/execute/verify; chỉ `verified=true` mới được xác nhận hoàn tất.
- `confirmation`: mã xác nhận cho action R2 như mở cửa sổ/cửa xe.
- `vehicle_state`: trạng thái simulator đọc lại sau action.
- `citations` và `grounding_status`: nguồn và trạng thái grounding cho nhánh handbook.
- `status`, `errors`, `timings`: kết quả và quan sát từng lượt.

Với action R0/R1, graph chạy safety gateway → vehicle simulator → verify ngay. Action R2
trả `confirmation_required`; client gửi lượt mới cùng `confirmation_id` và
`confirmation_decision=approve|deny`. `tts_text` chỉ xác nhận hoàn tất sau verify. Endpoint
chính cho pipeline mới là `POST /api/v1/assist`; `/api/v1/chat` chỉ được giữ để tương thích.
