# Setup trên PC

PC là môi trường development, ingestion và benchmark đầy đủ. Runtime công khai duy nhất là
`src.vivi.api.app:app`; không chạy `src.main` hoặc `server.app` trực tiếp.

## 1. Yêu cầu

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

## 2. Môi trường Python

Chỉ chạy API, rules, SQLite handbook và simulator:

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements-dev.txt
```

Cài thêm PhoWhisper và ZeroTTS trên PC:

```bash
.venv/bin/python -m pip install -r requirements-pc-ai.txt
cp .env.pc.example .env
```

Máy không dùng NVIDIA nên cài wheel PyTorch phù hợp hệ điều hành trước, sau đó cài phần
còn lại. Không cài các wheel CUDA 12 của PC lên Jetson Nano.

## 3. Tải model trước để chạy offline

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

```dotenv
LLM_PROVIDER=local
LOCAL_LLM_BASE_URL=http://127.0.0.1:8080/v1
LOCAL_LLM_MODEL=qwen2.5-3b-instruct-q4_k_m.gguf
```

Để kiểm tra luồng trước khi tải LLM, dùng `LLM_PROVIDER=rules`.

## 4. Handbook SQLite

Thực hiện theo [Handbook SQLite](handbook_sqlite.md). Sau bước import phải có:

```text
data/handbooks/handbook.sqlite3
```

## 5. Chạy

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

## 6. Kiểm thử

```bash
.venv/bin/python -m pytest -q
.venv/bin/python -m ruff check server src vehicle_simulator tests
node --check app.js
```
