# Setup trên PC

PC là môi trường development, ingestion và benchmark đầy đủ. Runtime công khai duy nhất là
`src.vivi.api.app:app`; toàn bộ application code nằm trong package `src/vivi/`.

## 1. Nhận code và chạy baseline

Baseline không cần API key, GPU hay model AI. Nó chạy API + UI, LangGraph rules, handbook
SQLite và vehicle simulator trong bộ nhớ; đây là cách nhanh nhất để xác nhận máy mới đã setup
đúng.

```bash
git clone <repository-url>
cd P-126
git switch <team-branch>
bash scripts/setup.sh
.venv/bin/python run.py
```

Mở `http://127.0.0.1:8787`. Ở terminal khác:

```bash
curl http://127.0.0.1:8787/api/v1/health
.venv/bin/python scripts/smoke_runtime.py --provider rules
```

Smoke test phải đi qua được truy vấn handbook, action thường, action cần xác nhận và bước
verify state. `config.toml` mặc định dùng rules, simulator memory và tắt STT/TTS.
`.env` chỉ chứa API keys và log-server credentials. Nhập text
trên UI vẫn chạy đủ LangGraph, RAG và vehicle flow.

Nếu `scripts/setup.sh` báo thiếu handbook, kiểm tra file đã được commit/push từ máy nguồn:

```bash
git ls-files data/handbooks/handbook.sqlite3
ls -lh data/handbooks/handbook.sqlite3
```

## 2. Yêu cầu hệ thống

- Python 3.11 hoặc 3.12.
- `ffmpeg` cho audio.
- Mosquitto nếu kiểm thử MQTT.
- Tối thiểu 15 GB trống nếu cài toàn bộ STT, TTS và LLM.

Ubuntu/Debian:

```bash
sudo apt update
sudo apt install -y ffmpeg mosquitto mosquitto-clients python3-venv
```

macOS:

```bash
brew install ffmpeg mosquitto python@3.12
```

## 3. Cài thủ công

Chỉ chạy API, rules, SQLite handbook và simulator:

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements-dev.txt
cp .env.example .env
```

Cài thêm PhoWhisper và ZeroTTS trên PC:

```bash
.venv/bin/python -m pip install -r requirements-ai.txt
```

Sau đó bật các component cần dùng trong `config.toml`:

```toml
runtime_profile = "pc"
stt_provider = "phowhisper"
phowhisper_preload = true
llm_provider = "rules"
tts_provider = "zerotts"
zerotts_preload = true
```

Có thể chỉ bật STT mà không bật TTS:

```toml
stt_provider = "phowhisper"
phowhisper_preload = false
tts_provider = "off"
```

`*_preload = false` là lazy-load, không phải disable. Muốn component không bao giờ nạp
model trong phiên dev/test, đặt provider của component thành `off`.

Máy không dùng NVIDIA nên cài wheel PyTorch phù hợp hệ điều hành trước, sau đó cài phần
còn lại. Không cài các wheel CUDA 12 của PC lên Jetson Nano.

## 4. Cài model AI (tùy chọn)

Các adapter tự tải model ở lần chạy đầu. Để chủ động tải cache:

```bash
.venv/bin/python -m pip install huggingface_hub
.venv/bin/hf download vinai/PhoWhisper-medium
.venv/bin/hf download zeroweight-ai/ZeroTTS
```

Voice pack `VIVI` nằm trong `voices/VIVI.zip`. Kiểm tra license của voice pack trước khi
dùng ngoài demo.

LLM local nên chạy bằng llama.cpp, tách khỏi tiến trình API:

```bash
llama-server \
  -hf Qwen/Qwen2.5-3B-Instruct-GGUF:Q4_K_M \
  --host 127.0.0.1 --port 8080 --ctx-size 2048
```

Cấu hình:

```toml
llm_provider = "local"
local_llm_base_url = "http://127.0.0.1:8080/v1"
local_llm_model = "qwen2.5-3b-instruct-q4_k_m.gguf"
```

Để kiểm tra luồng trước khi tải LLM, dùng `llm_provider = "rules"` trong `config.toml`.

## 5. Handbook SQLite

Artifact dùng để chạy đã được chia sẻ cùng repository và phải có tại:

```text
data/handbooks/handbook.sqlite3
```

Đồng nghiệp chỉ cần pull file này, không phải crawl/import lại. Chỉ người phụ trách cập nhật
handbook mới cần thực hiện pipeline trong [Handbook SQLite](handbook_sqlite.md), rồi commit
lại artifact đã đóng kết nối. Không commit các file `handbook.sqlite3-wal` hoặc
`handbook.sqlite3-shm`.

## 6. Chạy

Chế độ memory simulator:

```bash
.venv/bin/python run.py
```

Mở `http://127.0.0.1:8787` và kiểm tra health:

```bash
curl http://127.0.0.1:8787/api/v1/health
```

Chế độ MQTT:

```bash
.venv/bin/python scripts/setup_mqtt_broker.py
mosquitto -c data/mqtt/mosquitto.conf
.venv/bin/python -m vehicle_simulator --mqtt --db data/vehicle-simulator.sqlite3
```

Đổi `VIVI_VEHICLE_PROVIDER=mqtt`, sau đó khởi động lại API.

## 7. Kiểm thử

```bash
.venv/bin/python -m pytest -q
.venv/bin/python -m ruff check server src vehicle_simulator tests
node --check app.js
```

Khi API đang chạy và handbook đã import, kiểm tra toàn bộ core flow qua public contract:

```bash
.venv/bin/python scripts/smoke_runtime.py --provider rules
# Hoặc, khi llama-server đang chạy:
.venv/bin/python scripts/smoke_runtime.py --provider local
```
