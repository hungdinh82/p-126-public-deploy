# Requirements

Nhiều file vì ViVi chạy trên nhiều môi trường (PC dev, Docker, Jetson). Mỗi file là một
"profile" cài đặt riêng; các profile `-r` (include) lẫn nhau theo chuỗi bên dưới.
Chạy mọi lệnh từ thư mục gốc của repo.

## Chọn file nào?

| Bạn muốn... | Cài |
|---|---|
| Phát triển + chạy test/lint (mặc định) | `requirements-dev.txt` |
| Thêm AI nặng trên PC (PhoWhisper, ZeroTTS, SDK cloud) | `requirements-ai.txt` (cài thêm sau `dev`) |
| Môi trường RAG CPU độc lập / Docker mặc định | `requirements-rag.txt` |
| Thêm RAG vào môi trường đã có ONNX Runtime GPU | `requirements-rag-base.txt` |
| Runtime tối thiểu (Jetson Nano, Docker nhẹ) | `requirements.txt` |
| AI gateway riêng trên Jetson AGX | các file `*jetson*` (do `scripts/jetson_ai.sh` tự cài) |

```bash
.venv/bin/python -m pip install -r requirements/requirements-dev.txt
# Tùy chọn trên PC:
.venv/bin/python -m pip install -r requirements/requirements-ai.txt
```

## Quan hệ include

```text
requirements.txt                 # lõi: API, LangGraph, MQTT
  └─ requirements-rag-base.txt   # + numpy, tokenizers (không có onnxruntime)
       ├─ requirements-rag.txt   # + onnxruntime (CPU)         → Dockerfile mặc định
       ├─ requirements-dev.txt   # + pytest, ruff, ingestion    → CI, scripts/setup.sh
       └─ requirements-ai.txt    # + PhoWhisper, ZeroTTS, SDK   → PC tùy chọn
```

`requirements-rag-base.txt` cố ý **không** kéo `onnxruntime`, để không ghi đè bản
ONNX Runtime GPU đã cài sẵn trong môi trường speech.

## File Jetson và lock

- `requirements-jetson-ai.txt`: nguồn (không hash) cho AI gateway Jetson, tách khỏi API/RAG.
- `requirements-jetson-ai.lock`: bản ghim đầy đủ có hash, sinh từ file `.txt` ở trên
  (lệnh `uv pip compile` ở đầu file). Được `scripts/jetson_ai.sh` cài và
  `scripts/check_jetson_runtime.py` băm để kiểm tra.
- `requirements-jetson-zerotts.lock`: chỉ wheel ZeroTTS; phụ thuộc còn lại nằm trong lock trên.
- `requirements-runtime.lock`: phiên bản đã dùng khi kiểm thử navigation (Linux x86_64,
  Python 3.12). Chỉ để tham khảo/tái lập, không có script nào cài tự động.

## Ai dùng file nào

- Docker (`Dockerfile`): `requirements-rag.txt`, đổi bằng `--build-arg REQUIREMENTS_FILE=requirements/<file>`.
- Docker Nano (`Dockerfile.nano`): `requirements.txt`.
- CI (`.github/workflows/deploy-render.yml`) và `scripts/setup.sh`: `requirements-dev.txt`.
- `scripts/release.sh`: ghi hash của `requirements.txt`, `-ai`, `-dev` vào release manifest.

Khi thêm hoặc đổi tên file ở đây, nhớ cập nhật các nơi trên.
