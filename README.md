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

Nếu chỉ phát triển backend/UI mà chưa cần model nặng:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python run.py
```

## Các luồng có thể thử

- Nhấn mic một lần để bắt đầu thu âm. ViVi tự dừng sau khoảng lặng hoặc khi nhấn mic lần nữa.
- Nhập “Đặt nhiệt độ 25 độ”, “Giảm nhiệt độ 2 độ”, “Phát nhạc thư giãn”, “Dừng nhạc”, “Trạng thái xe”.
- Thử “Mở cửa sổ bên tài”, “Đóng cửa sổ bên tài”, “Mở cửa xe bên tài”, “Khóa cửa xe”, “Mở khóa cửa xe”. ViVi đọc lại đúng thao tác và chờ câu trả lời “Xác nhận” hoặc “Hủy” trong 30 giây; trước khi xác nhận, state không đổi.
- Thử “Sưởi ghế mức 2” hoặc “Tắt sưởi ghế”; các thao tác ghế không cần xác nhận.
- Chuyển sang “Đang lái xe” rồi thử mở cửa sổ để thấy policy minh họa chặn lệnh.
- Nhập “Đặt nhiệt độ 35 độ” để thử kiểm tra giới hạn.
- Mở cẩm nang để xem cách trình bày câu trả lời với nguồn của prototype.
- Bật “Mai Chi” để phát phản hồi bằng ZeroTTS local; SpeechSynthesis của trình duyệt là fallback khi TTS chưa sẵn sàng.
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

Để quay lại simulator trong bộ nhớ, đổi `VIVI_VEHICLE_PROVIDER=memory` trong `data/mqtt/credentials.env` hoặc tạm đổi tên file này, rồi chạy lại backend. Không cần broker khi dùng chế độ memory. Với cửa kính và cửa xe, backend tạo một yêu cầu xác nhận gắn với đúng action, phiên và state version. Lời xác nhận chỉ dùng một lần; hủy, quá 30 giây hoặc state xe thay đổi đều không gửi lệnh. Khi dùng giao diện, có thể gõ hoặc nói “Xác nhận”/“Hủy”; API client cần gửi lại `confirmation_id` từ phản hồi `status=confirm` trong lượt kế tiếp. Yêu cầu đang chờ chỉ lưu trong RAM backend, nên sẽ hết hiệu lực khi backend khởi động lại.

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
- `POST /api/v1/turn` — transcript; với MQTT, backend bỏ qua trạng thái xe từ trình duyệt
- `GET /api/v1/vehicle/state` — state xe hiện hành, đọc qua MQTT khi bật service
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
