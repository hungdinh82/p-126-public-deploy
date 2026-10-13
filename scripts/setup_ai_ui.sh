#!/usr/bin/env bash
set -Eeuo pipefail
TASK_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$TASK_ROOT"
tts_precision=int8
if [[ $# -gt 0 ]]; then
  [[ $# == 2 && "$1" == --tts-precision && ( "$2" == int8 || "$2" == fp32 || "$2" == int8-gpu ) ]] || {
    echo 'Usage: bash scripts/setup_ai_ui.sh [--tts-precision int8-gpu|int8|fp32]' >&2; exit 2;
  }
  tts_precision="$2"
fi
[[ "$tts_precision" != int8-gpu ]] || {
  echo 'GPU INT8 setup targets Jetson AGX Xavier. Use prepare_speech_models.py on PC to build QDQ artifacts only.' >&2;
  exit 2;
}
export PATH="$HOME/.local/bin:$PATH"
[[ "$(uname -m)" == x86_64 ]] || { echo 'Trên Xavier: bash scripts/jetson_ai.sh setup --profile agx' >&2; exit 1; }
command -v uv >/dev/null || { echo 'Cần uv để setup môi trường local.' >&2; exit 1; }
[[ -x .venv/bin/python ]] || uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python fastapi httpx uvicorn wsproto==1.2.0 python-multipart \
  huggingface-hub tokenizers scipy soundfile 'transformers>=4.51,<5' \
  'onnxruntime-gpu==1.23.2' diskcache==5.6.3
uv pip install --python .venv/bin/python --no-deps zerotts==0.1.5
if ! .venv/bin/python -c 'import torch; assert torch.version.cuda' 2>/dev/null; then
  uv pip install --python .venv/bin/python torch==2.6.0 --index-url https://download.pytorch.org/whl/cu124
fi
uv pip install --python .venv/bin/python --no-deps \
  https://github.com/abetlen/llama-cpp-python/releases/download/v0.3.36-cu124/llama_cpp_python-0.3.36-py3-none-manylinux_2_35_x86_64.whl
[[ -f models/vivi-slm/Qwen3-1.7B-Q4_K_M.gguf ]] || bash scripts/setup_local_slm.sh cpu
if [[ "$tts_precision" == int8 ]]; then
  uv pip install --python .venv/bin/python onnx==1.17.0
fi
.venv/bin/python scripts/prepare_speech_models.py --root models/ai-speech --tts-precision "$tts_precision"
mkdir -p models/ai-speech
touch models/ai-speech/.setup-complete
echo 'Setup xong. Chạy: bash scripts/ai_server.sh run'
echo "ZeroTTS: $tts_precision (INT8 uses CPU; FP32 follows --device)."
