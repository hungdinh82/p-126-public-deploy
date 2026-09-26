# ViVi Space Concept

> Runtime chuẩn sau khi hợp nhất là `src.vivi.api.app:app`. `src.main` chỉ còn shim tương
> thích; không còn backend `/assist` thứ hai. Xem [contract runtime](docs/contracts.md),
> [setup PC](docs/setup_pc.md), [setup Jetson Nano 4 GB](docs/setup_jetson_nano.md) và
> [handbook SQLite](docs/handbook_sqlite.md).

Prototype giao diện trợ lý ô tô: bầu trời đêm, lõi sáng, hai dải LED được chiếu từ hình học 3D lên Canvas, chuyển động theo trạng thái tương tác.

## Chạy ViVi local

```sh
python3.12 -m venv .venv-ai
mkdir -p /var/tmp/vivi-pip
TMPDIR=/var/tmp/vivi-pip .venv-ai/bin/python -m pip install 'torch==2.6.0' \
  --index-url https://download.pytorch.org/whl/cpu
TMPDIR=/var/tmp/vivi-pip .venv-ai/bin/python -m pip install -r requirements-ai.txt
cp .env.example .env
.venv-ai/bin/python run.py
```

Cấu hình trên dùng PyTorch CPU để tránh tải bộ CUDA lớn của Torch. ZeroTTS dùng
`onnxruntime-gpu` độc lập và tự chọn CUDA khi `ZEROTTS_DEVICE=auto`; đặt `cuda` để yêu
cầu CUDA và báo lỗi ngay nếu provider không hoạt động. Muốn chạy cả PhoWhisper bằng
NVIDIA, bỏ bước cài Torch CPU và cài thẳng `requirements-ai.txt`; wheel PyTorch 2.6 sẽ
dùng CUDA 12.4. Luôn đặt `TMPDIR` trên ổ đĩa chính nếu `/tmp` là tmpfs nhỏ, nếu không
pip có thể báo `No space left on device` dù ổ đĩa vẫn còn trống.

Mở http://127.0.0.1:8787 (hoặc cổng `VIVI_PORT` trong `.env`). Backend chỉ bind
`127.0.0.1` ở profile PC. ZeroTTS và PhoWhisper chỉ được nạp/warm lúc khởi động khi các
biến `ZEROTTS_PRELOAD` và `PHOWHISPER_PRELOAD` được bật; nếu tắt, model được nạp ở lượt
dùng đầu tiên. Lần đầu có thể tải model, các lần sau dùng cache local. Font Google Fonts có
fallback font hệ thống khi offline.

Model tải từ Hugging Face nằm trong `~/.cache/huggingface/hub/`. Ví dụ PhoWhisper
medium nằm tại `models--vinai--PhoWhisper-medium`, còn ZeroTTS nằm tại
`models--zeroweight-ai--ZeroTTS`. Có thể đổi gốc cache bằng biến `HF_HOME`.

Voice ZeroTTS của ViVi nằm trong `voices/VIVI.zip` với ID `VIVI` (`ZEROTTS_VOICE=VIVI`). Pack này chứa giọng community “Mai Chi”; backend nạp trực tiếp từ ZIP, không cần cài voice vào thư mục người dùng.

PC có thể preload speech bằng `requirements-pc-ai.txt`. Profile Nano không preload model;
API vẫn khởi động và health báo rõ adapter nào chưa sẵn sàng. `/api/v1/health` trả
`tts.detail=loaded` khi model đã ở trong bộ nhớ.

## Các luồng có thể thử

- Nhấn mic một lần để bắt đầu thu âm. ViVi tự dừng sau khoảng lặng hoặc khi nhấn mic lần nữa.
- Nhập “Đặt nhiệt độ 25 độ”, “Giảm nhiệt độ 2 độ”, “Phát nhạc thư giãn”, “Dừng nhạc”, “Trạng thái xe”.
- Thử “Mở cửa sổ bên tài”, “Đóng cửa sổ bên tài”, “Mở cửa xe bên tài”, “Khóa cửa xe”, “Mở khóa cửa xe”. ViVi đọc lại đúng thao tác và chờ câu trả lời “Xác nhận” hoặc “Hủy” trong 30 giây; trước khi xác nhận, state không đổi.
- Thử “Sưởi ghế mức 2” hoặc “Tắt sưởi ghế”; các thao tác ghế không cần xác nhận.
- Chuyển sang “Đang lái xe” rồi thử mở cửa sổ để thấy policy minh họa chặn lệnh.
- Nhập “Đặt nhiệt độ 35 độ” để thử kiểm tra giới hạn.
- Mở cẩm nang để xem cách trình bày câu trả lời với nguồn của prototype.
- Bật “Mai Chi” để phát phản hồi VIVI theo luồng từ ZeroTTS local; có thể tắt giọng để ngắt phát. SpeechSynthesis của trình duyệt là fallback nếu TTS chưa phát được.
- Mở thiết lập để giảm chuyển động. Giao diện cũng tôn trọng prefers-reduced-motion.

## Vehicle Simulator service qua MQTT

Service mô phỏng xe chạy độc lập với ViVi. Để dùng MQTT trên máy local, cài Mosquitto và tạo cấu hình broker (mỗi lệnh chỉ cần làm một lần):

```sh
.venv/bin/python scripts/setup_mqtt_broker.py
```

Lệnh này sinh mật khẩu ngẫu nhiên và ACL trong `data/mqtt/` (đã bị Git bỏ qua). Broker chỉ nghe tại `127.0.0.1:1883`; tài khoản ViVi chỉ gửi command, tài khoản simulator chỉ gửi ACK/state/availability. Mở ba terminal:

```sh
mosquitto -c data/mqtt/mosquitto.conf
```

Trên Homebrew macOS, có thể cần dùng `/opt/homebrew/opt/mosquitto/sbin/mosquitto` thay cho `mosquitto`.

```sh
.venv/bin/python -m vehicle_simulator --mqtt --db data/vehicle-simulator.sqlite3
```

```sh
.venv/bin/python run.py
```

Mở http://127.0.0.1:8787 và thử “Đặt nhiệt độ 25 độ”, “Phát nhạc” hoặc “Trạng thái xe”. Backend lấy state qua MQTT trước khi ra quyết định, kiểm tra ACK đúng command rồi chủ động đọc state thêm lần nữa trước khi báo thành công. Khi ACK mất hoặc service ngắt, kết quả là “chưa xác minh”, không báo đã làm. Nếu backend offline, giao diện cũng không tự giả lập thao tác cabin. Giao diện không cho bật trạng thái lái khi dùng MQTT; muốn thử các fixture/fault, khởi động simulator thêm `--enable-test-control` và dùng HTTP test API.

Nếu muốn chạy service HTTP độc lập mà chưa bật MQTT:

```sh
.venv/bin/python -m vehicle_simulator --db data/vehicle-simulator.sqlite3
```

Mặc định service chỉ nghe tại `127.0.0.1:8788`. Mở `http://127.0.0.1:8788/docs` để xem và thử API: đọc state ở `GET /api/v1/vehicles/{vehicle_id}/state`, gửi lệnh ở `POST /api/v1/vehicles/{vehicle_id}/commands`. Command có `command_id`, `correlation_id`, `idempotency_key`, expiry và `expected_state_version`; phản hồi gồm acknowledgement và state hiện hành. State và kết quả chống lặp được lưu trong SQLite qua lần khởi động lại.

Để thử các tình huống lỗi và fixture trạng thái xe trong demo, khởi động với `--enable-test-control`; khi đó các API `/api/v1/test/vehicles/{vehicle_id}/...` xuất hiện trong trang `/docs`. Chỉ dùng chế độ này trên máy local. Chi tiết catalog, fault mode và tiêu chí nghiệm thu ở [đặc tả Vehicle Simulator](docs/VEHICLE_SIMULATOR_SPEC.md).

Để quay lại simulator trong bộ nhớ, đổi `VIVI_VEHICLE_PROVIDER=memory` trong
`data/mqtt/credentials.env` hoặc tạm đổi tên file này, rồi chạy lại backend. Không cần broker
khi dùng chế độ memory. Với cửa kính và cửa xe, backend tạo yêu cầu xác nhận gắn với đúng
action, phiên và state version. Lời xác nhận chỉ dùng một lần; hủy, quá 30 giây hoặc state xe
thay đổi đều không gửi lệnh. API client gửi `confirmation_id` tới endpoint riêng
`POST /api/v1/confirmations/{confirmation_id}`; không gửi xác nhận lẫn trong một `/turn` mới.
Yêu cầu đang chờ chỉ lưu trong RAM backend, nên hết hiệu lực khi backend khởi động lại.

## Phạm vi

Đây là concept độc lập, chưa phải sản phẩm VinFast chính thức. Xe vẫn là simulator; policy
chặn mở cửa khi lái chỉ là quy tắc demo. Audio và transcript mặc định không được lưu
(`VIVI_STORE_AUDIO=false`, `VIVI_STORE_TRANSCRIPTS=false`). Khi chủ động bật retention,
dữ liệu nằm trong `data/` cho đến khi người vận hành xóa; thư mục này không được commit.

## Provider LLM

Đặt provider mặc định trong `.env`, sau đó có thể đổi model hội thoại ngay trên giao diện mà không cần khởi động lại backend. Danh sách UI chỉ cho chọn các provider đã cấu hình; model ID và khóa API vẫn được quản lý trên backend.

- `LLM_PROVIDER=rules`: mặc định, chạy offline ngay và hữu ích cho test.
- `LLM_PROVIDER=local`: API local tương thích OpenAI Chat Completions; cấu hình `LOCAL_LLM_BASE_URL` và `LOCAL_LLM_MODEL`.
- `LLM_PROVIDER=openai`: OpenAI Responses API; cấu hình `OPENAI_API_KEY` và `OPENAI_MODEL`.
- `LLM_PROVIDER=google`: Gemini API; cấu hình `GOOGLE_API_KEY` và `GOOGLE_MODEL`.

OpenAI/Google cần mạng. Chỉ `rules` và `local` đáp ứng runtime offline. Nếu provider được chọn nhưng cấu hình thiếu, server khởi động an toàn bằng `rules` và báo chi tiết ở `/api/v1/health`.

`google` và `rules` đi qua LangGraph thống nhất; `google` dùng Gemini cho phân loại và trả
lời handbook, còn `rules` dùng bộ định tuyến deterministic nhưng vẫn giữ cùng safety graph.
`openai` và `local` hiện được giữ qua orchestrator tương thích cho hội thoại/action, chưa
tham gia nhánh handbook RAG. Sau khi thay đổi model hoặc khóa trong `.env`, cần khởi động
lại backend. Nếu giao diện báo backend offline thì nó dùng kịch bản demo trong trình duyệt.

## API local

- `GET /api/v1/health`
- `POST /api/v1/stt` — multipart audio, `session_id`, `turn_id`
- `POST /api/v1/turn` — transcript → LangGraph/RAG hoặc action → response thống nhất
- `POST /api/v1/confirmations/{confirmation_id}` — approve/deny đúng một action R2 đang chờ
- `POST /api/v1/tts` — text sang WAV Mai Chi
- `POST /api/v1/turn/stream` — NDJSON gồm các mệnh đề hội thoại có thể đọc sớm và một sự kiện `final` chứa kết quả chính thức. Lệnh điều khiển xe không phát trước khi qua safety/acknowledgement.
- `POST /api/v1/tts/stream` — PCM mono 16-bit little-endian theo luồng; sample rate ở header `X-ViVi-Sample-Rate`, giọng ở `X-ViVi-Voice`. Frontend nhận các chunk để đệm và phát liên tục.

Frontend giữ khoảng 1 giây audio trong bộ đệm trước khi phát để tránh hụt tiếng giữa các chunk đầu của ZeroTTS. Câu trả lời ngắn hơn được phát ngay khi tổng hợp xong; tắt giọng vẫn hủy luồng và playback.

## Kiểm thử

```sh
.venv/bin/python -m pytest -q
.venv/bin/python -m ruff check server src vehicle_simulator tests
node --check app.js
```

## Cấu trúc

- `index.html`: bố cục, điều khiển và thông tin trạng thái.
- `style.css`: ngôn ngữ thị giác, layout responsive, reduced motion.
- `app.js`: render không gian, trạng thái hội thoại, Digital Twin trong bộ nhớ, policy demo.
- `server/`: orchestration API, provider adapters, safety gateway, storage và vehicle simulator.
- `tests/`: test contract, safety, idempotency và API.

Pipeline hiện tại: thu âm → PhoWhisper → LangGraph → handbook RAG hoặc safety/action →
verify → ZeroTTS. Handbook chỉ được đọc sau khi citation hợp lệ; action chỉ được đọc là
thành công sau khi simulator xác minh trạng thái.

## Chuẩn bị handbook VF8 2026

```sh
.venv-ai/bin/python -m src.cli.crawl_manual

# Tạo chunks cho BM25 local; đây là bước đủ để server/UI dùng handbook.
.venv-ai/bin/python -m src.cli.parse_manual

# Tạo artifact SQLite FTS5 dùng chung cho PC và Jetson.
.venv-ai/bin/python -m src.cli.import_handbook_sqlite

# Tùy chọn: tạo Chroma hybrid khi quota Google embedding sẵn sàng.
.venv-ai/bin/python -m src.cli.build_index
```

Server/UI mặc định dùng SQLite FTS5 trên corpus đã import để không phụ thuộc quota embedding.
Snapshot handbook, Chroma và SQLite history đều nằm trong `data/` và không được commit.

Output handbook được ánh xạ vào `TurnResponse.evidence`; UI hiển thị câu trả lời cùng link
nguồn trong panel Cẩm nang. `/api/v1/turn/stream` chỉ phát câu trả lời sau khi grounding,
safety và verify tương ứng đã hoàn tất—không stream sớm nội dung chưa được kiểm chứng.
