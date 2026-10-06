# ViVi Space Concept

> Runtime chuẩn sau khi hợp nhất là `src.vivi.api.app:app`; không còn backend `/assist`
> hay entrypoint thứ hai. Xem [contract runtime](docs/contracts.md),
> [setup PC](docs/setup_pc.md), [setup Jetson Nano 4 GB](docs/setup_jetson_nano.md) và
> [handbook SQLite](docs/handbook_sqlite.md). Xem thêm [cấu trúc repo và hướng dẫn phát
> triển](docs/REPOSITORY_STRUCTURE.md).

Prototype giao diện trợ lý ô tô: bầu trời đêm, lõi sáng, hai dải LED được chiếu từ hình học 3D lên Canvas, chuyển động theo trạng thái tương tác.

## Chạy ViVi local

Baseline chạy ngay không cần tải model, GPU hay API key:

```bash
bash scripts/setup.sh
.venv/bin/python run.py
```

Mở http://127.0.0.1:8787, sau đó chạy smoke test ở terminal khác:

```bash
.venv/bin/python scripts/smoke_runtime.py --provider rules
```

Xem [setup PC](docs/setup_pc.md) để cài PhoWhisper, ZeroTTS, LLM local hoặc MQTT. Jetson
Nano dùng hướng dẫn riêng trong [setup Jetson Nano 4 GB](docs/setup_jetson_nano.md), không
cài wheel CUDA dành cho PC.

Backend chỉ bind
`127.0.0.1` ở profile PC. ZeroTTS và PhoWhisper chỉ được nạp/warm lúc khởi động khi các
giá trị `zerotts_preload` và `phowhisper_preload` trong `config.toml` được bật; nếu tắt, model được nạp ở lượt
dùng đầu tiên. Lần đầu có thể tải model, các lần sau dùng cache local. Font Google Fonts có
fallback font hệ thống khi offline.

### Chạy riêng qua ngrok

Để truy cập demo từ thiết bị bên ngoài, cài ngrok và điền `NGROK_AUTHTOKEN` trong `.env`.
Nếu muốn URL cố định, tạo/reserve static domain trong dashboard ngrok và điền URL vào
`NGROK_URL` trong `.env`. Có thể ghi đè domain bằng tham số `--url`:

```bash
.venv/bin/python scripts/run_ngrok.py --url https://ten-mien-cua-ban.ngrok.app
```

Launcher khởi động backend hiện tại trên cổng trong cấu hình (mặc định `8787`) và chạy
ngrok trỏ vào cổng đó. URL HTTPS xuất hiện trong output của ngrok. Nhấn `Ctrl+C` để dừng
cả tunnel và backend. Luồng này dùng nguyên API/pipeline hiện tại; chỉ bật khi chủ động
chạy launcher. URL ngrok công khai API và giao diện ra Internet, vì vậy chỉ dùng cho demo
với dữ liệu phù hợp. Domain cố định phải được reserve trong tài khoản ngrok của bạn;
authtoken xác thực agent nhưng không tự tạo domain.

Thiết lập provider/model trong `config.toml`; khóa API và ghi đè riêng cho máy đặt trong
`.env`. Thay đổi cấu hình cần restart backend:

| Component | Biến | Lựa chọn |
| --- | --- | --- |
| Text input | luôn bật | gửi thẳng transcript tới `/api/v1/turn` |
| STT | `stt_provider` trong `config.toml` | `off`, `phowhisper`, `whisper_cpp`, `soniox` |
| LLM/router | `llm_provider` trong `config.toml` | `rules`, `local`, `openai`, `google`, `openrouter` |
| TTS | `tts_provider` trong `config.toml` | `off`, `zerotts` |
| Handbook retrieval | `rag_retrieval_mode` trong `config.toml` | `sqlite`, `lexical`, `hybrid` |
| Vehicle | `vehicle_provider` trong `config.toml` | `memory`, `mqtt` |

Profile text-only nhẹ nhất cho dev/test:

```toml
stt_provider = "off"
llm_provider = "rules"
tts_provider = "off"
rag_retrieval_mode = "sqlite"
vehicle_provider = "memory"
```

`phowhisper_preload = false` hoặc `zerotts_preload = false` không tắt component; chúng
chỉ chuyển model sang lazy-load ở request đầu tiên. Đặt provider thành `"off"` khi muốn
chắc chắn model không được nạp trong phiên dev/test.

Model tải từ Hugging Face nằm trong `~/.cache/huggingface/hub/`. Ví dụ PhoWhisper
medium nằm tại `models--vinai--PhoWhisper-medium`, còn ZeroTTS nằm tại
`models--zeroweight-ai--ZeroTTS`. Có thể đổi gốc cache bằng biến `HF_HOME`.

Voice ZeroTTS của ViVi nằm trong `voices/VIVI.zip` với ID `VIVI` (`ZEROTTS_VOICE=VIVI`). Pack này chứa giọng community “Mai Chi”; backend nạp trực tiếp từ ZIP, không cần cài voice vào thư mục người dùng.

PC có thể preload speech bằng `requirements-ai.txt`. Profile Nano chỉ cài
`requirements.txt` và không preload model;
API vẫn khởi động và health báo rõ adapter nào chưa sẵn sàng. `/api/v1/health` trả
`tts.detail=loaded` khi model đã ở trong bộ nhớ.

## Các luồng có thể thử

- Nhấn mic một lần để bắt đầu thu âm. ViVi tự dừng sau khoảng lặng hoặc khi nhấn mic lần nữa.
- Nhập “Đặt nhiệt độ 25 độ”, “Giảm nhiệt độ 2 độ”, “Phát nhạc thư giãn”, “Dừng nhạc”, “Trạng thái xe”.
- Thử “Mở cửa sổ bên tài”, “Mở cửa bên phụ”, “Sưởi ghế sau trái mức 2”, “Khóa tất cả cửa”. ViVi hỗ trợ bốn vị trí cabin và `tất cả`; nếu chưa nói vị trí, ViVi hỏi lại trước khi tạo thao tác. Lệnh cửa/kính chờ “Xác nhận” hoặc “Hủy” trong 30 giây; trước khi xác nhận, state không đổi.
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

Đặt provider mặc định trong `config.toml`, sau đó có thể đổi model hội thoại ngay trên giao diện mà không cần khởi động lại backend. Danh sách UI chỉ cho chọn các provider đã cấu hình; model ID ở `config.toml`, khóa API ở `.env`.

- `llm_provider = "rules"`: mặc định, chạy offline ngay và hữu ích cho test.
- `llm_provider = "local"`: API local tương thích OpenAI Chat Completions; cấu hình `local_llm_base_url` và `local_llm_model`.
- `llm_provider = "openai"`: OpenAI Responses API; cấu hình `OPENAI_API_KEY` trong `.env` và `openai_model` trong `config.toml`.
- `llm_provider = "google"`: Gemini API; cấu hình `GOOGLE_API_KEY` trong `.env` và `google_model` trong `config.toml`.

Với Google, `GOOGLE_MODEL` phân loại intent/hội thoại; `RAG_GENERATION_MODEL` sinh câu
trả lời handbook sau retrieval. Structured routing cố định ở temperature 0 để kết quả
JSON và action ổn định, không dùng temperature như một tham số runtime chung.

OpenAI/Google cần mạng. Chỉ `rules` và `local` đáp ứng runtime offline. Nếu provider được chọn nhưng cấu hình thiếu, server khởi động an toàn bằng `rules` và báo chi tiết ở `/api/v1/health`.

`rules`, `google`, `openai` và `local` đều đi qua LangGraph thống nhất. `rules` dùng bộ định
tuyến deterministic và câu trả lời handbook extractive; ba provider model dùng structured
classification, SQLite retrieval và grounded generation với cùng safety graph. Sau khi thay
đổi model trong `config.toml` hoặc khóa trong `.env`, cần khởi động lại backend. Nếu giao diện báo backend
offline thì nó không gửi lệnh vehicle và chỉ hiển thị fallback an toàn.

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
.venv/bin/python -m ruff check src vehicle_simulator tests
node --check app.js
```

## Kiểm tra local và phát hành

Repo không dùng GitHub-hosted runner. Mỗi developer bật pre-push hook một lần sau khi
clone hoặc pull thay đổi này:

```sh
bash scripts/setup_hooks.sh
```

Trên Windows PowerShell:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\setup_hooks.ps1
```

Trước mỗi lần push, hook chạy quality gate dùng chung rồi mới gửi AI log. Gate gồm
whitespace, Ruff, JavaScript syntax, core tests và MQTT integration tests. Có thể chạy
trực tiếp để lấy evidence điền vào pull request:

```sh
bash scripts/check_local.sh
```

Script dùng profile deterministic `rules + memory + SQLite`, xóa API key khỏi môi
trường test và yêu cầu Mosquitto được cài trên máy. Khi thay đổi Dockerfile, dependency
runtime hoặc startup, chạy thêm:

```sh
bash scripts/check_docker.sh
```

Docker gate build image, kiểm tra process non-root, gọi health API, chạy luồng handbook,
action và confirmation, đồng thời xác nhận credential/log local không lọt vào image.
PhoWhisper, ZeroTTS, OpenAI và microphone vẫn được kiểm tra thủ công trên máy demo.

Sau khi checkpoint ổn định đã được merge vào `main`, nhóm trưởng hoàn tất
[release checklist](docs/RELEASE_CHECKLIST.md) rồi phát hành theo Semantic Versioning:

```sh
git switch main
git pull --ff-only
bash scripts/release.sh v0.1.0
```

Release script yêu cầu working tree sạch và `main` khớp `origin/main`, chạy lại cả hai
quality gate, tạo annotated tag, checksum cùng manifest rồi publish GitHub Release bằng
GitHub CLI. Không cần GitHub Actions. Trong giai đoạn `0.x`, tăng minor cho tính năng mới
(`v0.2.0`) và tăng patch cho bản sửa lỗi tương thích (`v0.1.1`).

## Cấu trúc

- `index.html`: bố cục, điều khiển và thông tin trạng thái.
- `style.css`: ngôn ngữ thị giác, layout responsive, reduced motion.
- `app.js`: render không gian, trạng thái hội thoại, Digital Twin trong bộ nhớ, policy demo.
- `src/vivi/`: application package duy nhất, gồm API, LangGraph, domain policy, speech,
  vehicle adapters, RAG và persistence.
- `vehicle_simulator/`: service mô phỏng xe độc lập, giao tiếp với ViVi qua MQTT.
- `tests/`: test contract, safety, idempotency và API.

Pipeline đầy đủ: thu âm → STT → LangGraph → handbook RAG hoặc safety/action →
verify → TTS. Khi STT/TTS tắt, pipeline rút gọn thành text → LangGraph → text.
Handbook chỉ được đọc sau khi citation hợp lệ; action chỉ được đọc là
thành công sau khi simulator xác minh trạng thái.

## Chuẩn bị handbook VF8 2026

```sh
.venv-ai/bin/python -m src.vivi.cli.crawl_manual

# Tạo chunks cho BM25 local; đây là bước đủ để backend/UI dùng handbook.
.venv-ai/bin/python -m src.vivi.cli.parse_manual

# Tạo artifact SQLite FTS5 dùng chung cho PC và Jetson.
.venv-ai/bin/python -m src.vivi.cli.import_handbook_sqlite

# Tùy chọn: tạo Chroma hybrid khi quota Google embedding sẵn sàng.
.venv-ai/bin/python -m src.vivi.cli.build_index
```

Server/UI mặc định dùng SQLite FTS5 trên corpus đã import để không phụ thuộc quota embedding.
Snapshot handbook, Chroma và SQLite history đều nằm trong `data/` và không được commit.

Output handbook được ánh xạ vào `TurnResponse.evidence`; UI hiển thị câu trả lời cùng link
nguồn trong panel Cẩm nang. `/api/v1/turn/stream` chỉ phát câu trả lời sau khi grounding,
safety và verify tương ứng đã hoàn tất—không stream sớm nội dung chưa được kiểm chứng.
