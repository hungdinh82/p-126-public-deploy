#!/usr/bin/env bash
set -Eeuo pipefail
TASK_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$TASK_ROOT"
export PATH="$HOME/.local/bin:$PATH"
[[ "$(uname -m)" == x86_64 ]] || { echo 'Trên Xavier: bash scripts/jetson_ai.sh setup --profile agx' >&2; exit 1; }
command -v uv >/dev/null || { echo 'Cần uv để setup môi trường local.' >&2; exit 1; }
[[ -x .venv/bin/python ]] || uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python fastapi httpx uvicorn python-multipart \
  huggingface-hub tokenizers scipy soundfile 'transformers>=4.51,<5' \
  'onnxruntime-gpu==1.23.2' diskcache==5.6.3
uv pip install --python .venv/bin/python --no-deps zerotts==0.1.5
if ! .venv/bin/python -c 'import torch; assert torch.version.cuda' 2>/dev/null; then
  uv pip install --python .venv/bin/python torch==2.6.0 --index-url https://download.pytorch.org/whl/cu124
fi
uv pip install --python .venv/bin/python --no-deps \
  https://github.com/abetlen/llama-cpp-python/releases/download/v0.3.36-cu124/llama_cpp_python-0.3.36-py3-none-manylinux_2_35_x86_64.whl
[[ -f models/vivi-slm/Qwen3-1.7B-Q4_K_M.gguf ]] || bash scripts/setup_local_slm.sh cpu
.venv/bin/python scripts/prepare_speech_models.py --root models/ai-speech
mkdir -p models/ai-speech
touch models/ai-speech/.setup-complete
echo 'Setup xong. Chạy: bash scripts/ai_server.sh run'
