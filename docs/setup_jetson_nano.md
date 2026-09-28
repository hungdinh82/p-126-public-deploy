# Setup trên Jetson Nano 4 GB

Profile này dành cho Jetson Nano đời cũ với JetPack 4.6.6. JetPack 4 dùng Ubuntu 18.04,
CUDA 10.2 và đã EOL; vì vậy API Python 3.11 chạy trong container, còn llama.cpp và
whisper.cpp chạy native như sidecar. Không dùng `requirements-pc-ai.txt` trên Nano.

Tham chiếu: [JetPack 4.6](https://developer.nvidia.com/embedded/jetpack-sdk-46),
[Jetson FAQ](https://developer.nvidia.com/embedded/faq) và
[Jetson lifecycle](https://developer.nvidia.com/embedded/lifecycle).

## 1. Chuẩn bị thiết bị

- Nguồn 5V/4A ổn định và quạt tản nhiệt.
- SD card tối thiểu 32 GB; USB SSD được khuyến nghị cho model/cache.
- JetPack 4.6.6.
- Docker và Mosquitto. Model native được build trong Debian Bookworm builder container để
  không phụ thuộc CMake cũ của Ubuntu 18.04.

```bash
sudo apt update
sudo apt install -y docker.io mosquitto mosquitto-clients
sudo usermod -aG docker "$USER"
```

Đăng nhập lại sau khi thêm group Docker. Không coi swap là RAM model; swap chỉ giúp tránh
crash tức thời và sẽ làm latency tăng mạnh.

## 2. Build API nhẹ

```bash
cp .env.nano.example .env
docker build -f Dockerfile.nano -t vivi-edge:nano .
docker build -f Dockerfile.nano-builder -t vivi-edge:nano-builder .
```

Nano không preload PhoWhisper hoặc ZeroTTS. API, LangGraph, SQLite FTS5 và MQTT dùng
`requirements-nano.txt`, không kéo PyTorch/Chroma/CUDA 12.

## 3. Cài whisper.cpp

Build CPU trước; chỉ bật CUDA sau khi CPU baseline đã ổn định:

```bash
mkdir -p models
docker run --rm --user "$(id -u):$(id -g)" \
  -v "$PWD/models:/workspace/models" vivi-edge:nano-builder bash -lc '
    git clone https://github.com/ggml-org/whisper.cpp.git models/whisper.cpp
    cmake -S models/whisper.cpp -B models/whisper.cpp/build \
      -DCMAKE_BUILD_TYPE=Release -DGGML_CUDA=OFF
    cmake --build models/whisper.cpp/build -j1
    sh models/whisper.cpp/models/download-ggml-model.sh base
  '
```

Test trực tiếp với WAV mono 16 kHz:

```bash
docker run --rm --user "$(id -u):$(id -g)" \
  -v "$PWD/models:/app/models:ro" \
  -v "$PWD/sample.wav:/app/sample.wav:ro" \
  vivi-edge:nano \
  /app/models/whisper.cpp/build/bin/whisper-cli \
  -m /app/models/whisper.cpp/models/ggml-base.bin \
  -f /app/sample.wav -l vi
```

`STT_PROVIDER=whisper_cpp` khiến API gọi binary này. Bản `base` là baseline nhẹ; chỉ chuyển
sang `small` sau khi đo peak RSS và latency.

Tham chiếu lệnh build/model: [whisper.cpp](https://github.com/ggml-org/whisper.cpp).

## 4. Cài llama.cpp và Qwen 1.5B Q4

Qwen2.5-1.5B Q4_K_M khoảng 1 GB, phù hợp hơn model 3B trên RAM 4 GB.

Model card: [Qwen2.5-1.5B-Instruct-GGUF](https://huggingface.co/Qwen/Qwen2.5-1.5B-Instruct-GGUF).

```bash
docker run --rm --user "$(id -u):$(id -g)" \
  -v "$PWD/models:/workspace/models" vivi-edge:nano-builder bash -lc '
    git clone https://github.com/ggerganov/llama.cpp.git models/llama.cpp
    cmake -S models/llama.cpp -B models/llama.cpp/build \
      -DCMAKE_BUILD_TYPE=Release -DGGML_CUDA=OFF
    cmake --build models/llama.cpp/build -j1 --target llama-server
    mkdir -p models/qwen
    curl -L \
      "https://huggingface.co/Qwen/Qwen2.5-1.5B-Instruct-GGUF/resolve/main/qwen2.5-1.5b-instruct-q4_k_m.gguf?download=true" \
      -o models/qwen/qwen2.5-1.5b-instruct-q4_k_m.gguf
  '
```

Sau khi tải model, ghi checksum và đúng commit runtime vào manifest triển khai:

```bash
git -C models/whisper.cpp rev-parse HEAD
git -C models/llama.cpp rev-parse HEAD
sha256sum models/whisper.cpp/models/ggml-base.bin
sha256sum models/qwen/qwen2.5-1.5b-instruct-q4_k_m.gguf
cp deploy/model-manifest.nano.example.json data/model-manifest.nano.json
```

Điền bốn giá trị vừa nhận vào `data/model-manifest.nano.json`; không để placeholder khi
đem sang thiết bị demo/production.

Chạy với context nhỏ và ba CPU thread để chừa tài nguyên cho API/audio:

```bash
docker run --rm --network host \
  -v "$PWD/models:/workspace/models:ro" vivi-edge:nano-builder \
  models/llama.cpp/build/bin/llama-server \
  --model /workspace/models/qwen/qwen2.5-1.5b-instruct-q4_k_m.gguf \
  --host 127.0.0.1 --port 8080 --ctx-size 2048 --threads 3
```

Nếu latency không đạt, chuyển toàn profile sang `LLM_PROVIDER=rules`; vehicle command vẫn
qua safety/MQTT và handbook dùng câu trả lời extractive có citation mà không cần Qwen.
Không chạy model 3B cùng PhoWhisper/ZeroTTS resident trên Nano 4 GB.

## 5. Handbook

Build handbook SQLite trên PC rồi chép đúng một artifact sang Nano:

```bash
rsync -av data/handbooks/handbook.sqlite3 jetson:/path/to/P-126/data/handbooks/
```

Không chạy crawler, Chroma hoặc embedding build trên Nano.

## 6. MQTT và API

```bash
mkdir -p data
python3 scripts/setup_mqtt_broker.py
mosquitto -c data/mqtt/mosquitto.conf
```

Chạy script trên host (không chạy trong container) để các đường dẫn tuyệt đối trong
`mosquitto.conf` trỏ đúng tới filesystem của Nano. Credentials sinh ra được API tự đọc qua
volume `data/` và không được commit.

Chạy simulator bằng môi trường development hoặc container riêng. Sau đó chạy API với host
network để truy cập llama.cpp và broker trên `127.0.0.1`:

```bash
docker run --rm --network host \
  --env-file .env \
  -v "$PWD/data:/app/data" \
  vivi-edge:nano \
  python -m vehicle_simulator --mqtt --db data/vehicle-simulator.sqlite3
```

Trong terminal khác:

```bash
docker run --rm --network host \
  --env-file .env \
  -v "$PWD/data:/app/data" \
  -v "$PWD/models:/app/models:ro" \
  -v "$PWD/voices:/app/voices:ro" \
  vivi-edge:nano
```

Kiểm tra:

```bash
curl http://127.0.0.1:8787/api/v1/health
docker run --rm --network host vivi-edge:nano \
  python scripts/smoke_runtime.py --provider local
```

## 7. TTS trên Nano

ZeroTTS hiện là profile PC. Trên Nano, `ZEROTTS_PRELOAD=false`; UI vẫn hiển thị text nếu
TTS trả 503. Chỉ cài ZeroTTS CPU sau khi đã benchmark STT + LLM, và không bật preload đồng
thời cả ba model. Một adapter TTS nhẹ hơn có thể thay thế qua `TextToSpeechPort` mà không
đổi orchestrator.

Ghi lại cho mỗi benchmark: commit, model/checksum, power mode, context, peak RSS, thời gian
warm/cold và p50/p95. Profile Nano chưa được coi là đạt cho đến khi WAN-disabled E2E pass.
