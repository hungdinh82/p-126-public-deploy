#!/usr/bin/env bash
# Native, user-space AI service for Jetson AGX Xavier (L4T R35.x).
set -Eeuo pipefail

VIVI_REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VIVI_DATA="${JETSON_AI_DATA_DIR:-/mnt/jetson-data/vivi-ai}"
VIVI_MODEL="$VIVI_DATA/models/Qwen3-1.7B-Q4_K_M.gguf"
VIVI_MODEL_SHA=b139949c5bd74937ad8ed8c8cf3d9ffb1e99c866c823204dc42c0d91fa181897
VIVI_MODEL_URL=https://huggingface.co/unsloth/Qwen3-1.7B-GGUF/resolve/d7f544eead698dbd1f15126ef60b45a1e1933222/Qwen3-1.7B-Q4_K_M.gguf
VIVI_LLAMA_REF=b11447
VIVI_WHISPER_REF=v1.7.6
VIVI_BACKEND_PORT="${JETSON_AI_BACKEND_PORT:-1235}"
VIVI_PORT="${JETSON_AI_PORT:-8789}"
VIVI_STT_PORT="${JETSON_AI_STT_PORT:-1236}"

fail() { echo "jetson_ai: $*" >&2; exit 1; }
check_platform() {
  [[ "$(uname -m)" == aarch64 ]] || fail "profile agx requires aarch64"
  [[ -r /etc/nv_tegra_release ]] || fail "L4T release was not found"
  grep -q 'R35' /etc/nv_tegra_release || fail "This profile expects L4T R35.x"
  mountpoint -q "$(dirname "$VIVI_DATA")" || fail "Data mount is missing: $(dirname "$VIVI_DATA")"
  [[ -w "$(dirname "$VIVI_DATA")" ]] || fail "Data mount is not writable"
}
check_space() {
  local available required=15728640
  available="$(df -Pk "$(dirname "$VIVI_DATA")" | awk 'NR==2 {print $4}')"
  if [[ -f "$VIVI_DATA/speech-setup-complete" ]] && \
     [[ -x "$VIVI_DATA/build/llama.cpp/build/bin/llama-server" ]] && \
     [[ -f "$VIVI_MODEL" ]] && echo "$VIVI_MODEL_SHA  $VIVI_MODEL" | sha256sum -c --status; then
    required=524288
  fi
  (( available >= required )) || fail "Insufficient free space on the data mount (need $required KiB)"
}
find_python() {
  uv python install 3.10 >&2 || fail "Could not prepare private Python 3.10"
  uv python find 3.10
}
download_model() {
  if [[ -f "$VIVI_MODEL" ]] && echo "$VIVI_MODEL_SHA  $VIVI_MODEL" | sha256sum -c --status; then return; fi
  mkdir -p "$(dirname "$VIVI_MODEL")"
  curl --fail --location --retry 2 --connect-timeout 10 --max-time 3600 \
    --continue-at - "$VIVI_MODEL_URL" -o "$VIVI_MODEL.part" || fail "Model download failed; rerun setup"
  echo "$VIVI_MODEL_SHA  $VIVI_MODEL.part" | sha256sum -c --status || fail "Model checksum mismatch: $VIVI_MODEL.part"
  mv "$VIVI_MODEL.part" "$VIVI_MODEL"
}
build_llama() {
  local source="$VIVI_DATA/build/llama.cpp" binary="$VIVI_DATA/build/llama.cpp/build/bin/llama-server"
  [[ -x "$binary" && -f "$VIVI_DATA/build/llama-mode" && "$(cat "$VIVI_DATA/build/llama-mode")" == cuda ]] && return
  command -v cmake >/dev/null || fail "cmake is required to build llama.cpp"
  command -v git >/dev/null || fail "git is required to fetch llama.cpp"
  if [[ ! -d "$source/.git" ]]; then
    git clone --depth 1 --branch "$VIVI_LLAMA_REF" https://github.com/ggml-org/llama.cpp.git "$source" \
      || fail "Could not fetch pinned llama.cpp ref $VIVI_LLAMA_REF"
  fi
  cmake -S "$source" -B "$source/build" -DGGML_CUDA=ON -DGGML_CUDA_NO_VMM=ON \
    -DLLAMA_CURL=OFF -DLLAMA_BUILD_TESTS=OFF \
    -DCMAKE_CUDA_ARCHITECTURES=72 -DCMAKE_CUDA_COMPILER=/usr/local/cuda/bin/nvcc \
    -DCMAKE_BUILD_TYPE=Release || fail "llama.cpp CUDA configure failed"
  cmake --build "$source/build" --target llama-server -j 2 || fail "llama.cpp CUDA build failed (no CPU fallback)"
  echo cuda > "$VIVI_DATA/build/llama-mode"
}
build_whisper() {
  local source="$VIVI_DATA/build/whisper.cpp"
  [[ -x "$source/build/bin/whisper-server" ]] && return
  [[ -d "$source/.git" ]] || git clone --depth 1 --branch "$VIVI_WHISPER_REF" \
    https://github.com/ggml-org/whisper.cpp.git "$source"
  cmake -S "$source" -B "$source/build" -DGGML_CUDA=ON -DGGML_CUDA_NO_VMM=ON \
    -DCMAKE_CUDA_ARCHITECTURES=72 -DCMAKE_CUDA_COMPILER=/usr/local/cuda/bin/nvcc \
    -DCMAKE_BUILD_TYPE=Release
  cmake --build "$source/build" --target whisper-server -j 2
}
build_ort() {
  local python_bin="$VIVI_DATA/venv/bin/python" source="$VIVI_DATA/build/onnxruntime"
  if "$python_bin" -c 'import onnxruntime as o; assert o.__version__ == "1.16.3"; assert "CUDAExecutionProvider" in o.get_available_providers()' 2>/dev/null; then return; fi
  # CUDA 11.4 / cuDNN 8 on JetPack 5: build a native wheel, keep system libraries intact.
  if [[ -n "${JETSON_AI_ORT_WHEEL:-}" ]]; then
    uv pip install --python "$python_bin" --no-deps "$JETSON_AI_ORT_WHEEL"
  else
    uv pip install --python "$python_bin" 'setuptools<70' wheel packaging 'numpy==1.26.4' \
      coloredlogs flatbuffers 'protobuf<5' sympy
    [[ -d "$source/.git" ]] || git clone --depth 1 --branch v1.16.3 --recursive \
      --shallow-submodules https://github.com/microsoft/onnxruntime.git "$source"
    (cd "$source"
     "$python_bin" tools/ci_build/build.py --config Release --build_wheel --parallel 2 \
       --skip_tests --use_cuda --cuda_home /usr/local/cuda --cudnn_home /usr \
       --cmake_path "$VIVI_DATA/venv/bin/cmake" --build_dir "$source/build" \
       --compile_no_warning_as_error --cmake_extra_defines CMAKE_CUDA_ARCHITECTURES=72)
    local wheels=("$source"/build/Release/dist/onnxruntime_gpu-*.whl)
    [[ -f "${wheels[0]}" ]] || fail "Native CUDA ONNX Runtime wheel was not produced"
    uv pip install --python "$python_bin" --no-deps "${wheels[0]}"
  fi
  "$python_bin" -c 'import onnxruntime as o; assert o.__version__ == "1.16.3"; assert "CUDAExecutionProvider" in o.get_available_providers()' \
    || fail "ORT 1.16.3 CUDA is required for this Xavier profile"
}
setup() {
  [[ "${1:-}" == --profile && "${2:-}" == agx ]] || fail "Usage: bash scripts/jetson_ai.sh setup --profile agx"
  check_platform
  check_space
  mkdir -p "$VIVI_DATA"/{venv,models,build,cache,logs}
  cd "$VIVI_REPO"
  export UV_CACHE_DIR="$VIVI_DATA/cache/uv" PIP_CACHE_DIR="$VIVI_DATA/cache/pip" \
    UV_PYTHON_INSTALL_DIR="$VIVI_DATA/python" HF_HOME="$VIVI_DATA/cache/huggingface" TMPDIR="$VIVI_DATA/cache/tmp"
  mkdir -p "$TMPDIR"
  export PATH="$HOME/.local/bin:$PATH"
  command -v uv >/dev/null || fail "Install uv first; setup uses an isolated Python environment"
  [[ -x /usr/local/cuda/bin/nvcc ]] || fail "CUDA toolkit is missing"
  command -v g++ >/dev/null || fail "C++ compiler is missing (build-essential)"
  local python_bin
  python_bin="$(find_python)"
  if [[ ! -x "$VIVI_DATA/venv/bin/python" ]] || ! "$VIVI_DATA/venv/bin/python" -c 'import sys; assert sys.version_info[:2] == (3,10)' 2>/dev/null; then
    # Preserve the prior LLM-only Python 3.12 environment if one exists.
    [[ ! -x "$VIVI_DATA/venv/bin/python" ]] || mv "$VIVI_DATA/venv" "$VIVI_DATA/venv-before-speech-$(date +%s)"
    uv venv --python "$python_bin" "$VIVI_DATA/venv" || fail "Could not create private venv"
  fi
  if command -v uv >/dev/null 2>&1; then
    uv pip install --python "$VIVI_DATA/venv/bin/python" -r "$VIVI_REPO/requirements-jetson-ai.txt" \
      || fail "Gateway dependency install failed; rerun setup"
  else
    "$VIVI_DATA/venv/bin/python" -m pip install -r "$VIVI_REPO/requirements-jetson-ai.txt" \
      || fail "Gateway dependency install failed; rerun setup"
  fi
  download_model
  uv pip install --python "$VIVI_DATA/venv/bin/python" 'cmake==3.27.9' \
    coloredlogs flatbuffers 'protobuf<5' sympy
  export PATH="$VIVI_DATA/venv/bin:$PATH"
  build_llama
  build_whisper
  build_ort
  # ZeroTTS declares ORT >=1.17; this Xavier profile tries 1.16.3 CUDA.
  # A real load + synthesis below must pass before setup is marked complete.
  # Do not let pip replace the native CUDA wheel with an incompatible CPU package.
  uv pip install --python "$VIVI_DATA/venv/bin/python" --no-deps zerotts==0.1.5
  "$VIVI_DATA/venv/bin/python" scripts/prepare_speech_models.py --root "$VIVI_DATA/models/speech" --engine whisper_cpp
  JETSON_AI_DEVICE=cuda JETSON_AI_TTS_MODEL="$VIVI_DATA/models/speech/zerotts" \
    "$VIVI_DATA/venv/bin/python" -c 'from jetson_ai.speech import SpeechModels; m=SpeechModels(); m.load("tts"); assert m.info["tts"]["using_gpu"], m.info["tts"]; assert m.synthesize("Xin chào bạn.")[:4] == b"RIFF"' \
    || fail "TTS CUDA smoke failed; setup is incomplete"
  touch "$VIVI_DATA/speech-setup-complete"
  if [[ ! -f "$VIVI_DATA/service.env" ]]; then
    umask 077
    command -v openssl >/dev/null || fail "openssl is required to create the service token"
    printf 'JETSON_AI_TOKEN=%s\n' "$(openssl rand -hex 32)" > "$VIVI_DATA/service.env"
    chmod 600 "$VIVI_DATA/service.env"
  fi
  (set -a; source "$VIVI_DATA/service.env"; set +a
   "$VIVI_DATA/venv/bin/python" -c 'from jetson_ai.app import app; assert app is not None') \
    || fail "Gateway import check failed"
  echo "Setup complete: SLM + STT + TTS CUDA."
  echo "Service token: $VIVI_DATA/service.env (copy securely to Laptop)"
  if command -v tailscale >/dev/null 2>&1; then
    echo "Tailscale URL: http://$(tailscale ip -4 2>/dev/null | head -n 1):$VIVI_PORT"
  fi
  echo "Run: bash scripts/jetson_ai.sh run"
}
run() {
  check_platform
  [[ -x "$VIVI_DATA/venv/bin/python" ]] || fail "Run setup first: missing venv"
  [[ -x "$VIVI_DATA/build/llama.cpp/build/bin/llama-server" ]] || fail "Run setup first: missing llama-server"
  [[ -r "$VIVI_DATA/service.env" ]] || fail "Run setup first: missing service token"
  [[ -f "$VIVI_DATA/speech-setup-complete" ]] || fail "Run setup again: speech CUDA smoke has not passed"
  [[ -x "$VIVI_DATA/build/whisper.cpp/build/bin/whisper-server" ]] || fail "Run setup again: missing STT server"
  [[ -f "$VIVI_DATA/models/speech/whisper/ggml-base.bin" ]] || fail "Run setup again: missing speech weights"
  echo "$VIVI_MODEL_SHA  $VIVI_MODEL" | sha256sum -c --status || fail "Model missing or checksum mismatch"
  set -a
  # Created only by setup and readable by the service user.
  source "$VIVI_DATA/service.env"
  set +a
  export JETSON_AI_LLM_URL="http://127.0.0.1:$VIVI_BACKEND_PORT"
  local bind="${JETSON_AI_BIND:-}"
  if [[ -z "$bind" ]] && command -v tailscale >/dev/null 2>&1; then
    bind="$(tailscale ip -4 2>/dev/null | head -n 1 || true)"
  fi
  bind="${bind:-127.0.0.1}"
  [[ "$(cat "$VIVI_DATA/build/llama-mode")" == cuda ]] || fail "Run setup again: a CUDA build is required"
  local gpu_layers=99 started=$SECONDS
  "$VIVI_DATA/build/llama.cpp/build/bin/llama-server" \
    --model "$VIVI_MODEL" --alias vivi-qwen3-1.7b --host 127.0.0.1 \
    --port "$VIVI_BACKEND_PORT" --ctx-size 4096 --parallel 1 \
    --n-gpu-layers "$gpu_layers" --reasoning off \
    > "$VIVI_DATA/logs/llama-server.log" 2>&1 &
  local llm_pid=$!
  trap 'kill "$llm_pid" 2>/dev/null || true; wait "$llm_pid" 2>/dev/null || true' EXIT INT TERM
  local ready=0
  for _ in $(seq 1 120); do
    if curl --silent --fail "http://127.0.0.1:$VIVI_BACKEND_PORT/health" | \
       grep -Eq '"status"[[:space:]]*:[[:space:]]*"ok"'; then
      ready=1; break
    fi
    kill -0 "$llm_pid" 2>/dev/null || fail "llama-server exited; see $VIVI_DATA/logs/llama-server.log"
    sleep 1
  done
  (( ready == 1 )) || fail "llama-server did not become ready; see $VIVI_DATA/logs/llama-server.log"
  export JETSON_AI_LLM_LOAD_MS="$(((SECONDS-started)*1000))" JETSON_AI_LLM_PID="$llm_pid"
  started=$SECONDS
  "$VIVI_DATA/build/whisper.cpp/build/bin/whisper-server" \
    --model "$VIVI_DATA/models/speech/whisper/ggml-base.bin" --host 127.0.0.1 \
    --port "$VIVI_STT_PORT" --language vi > "$VIVI_DATA/logs/whisper-server.log" 2>&1 &
  local stt_pid=$!
  trap 'kill "$stt_pid" "$llm_pid" 2>/dev/null || true; wait "$stt_pid" "$llm_pid" 2>/dev/null || true' EXIT INT TERM
  ready=0
  for _ in $(seq 1 120); do
    if curl --silent --fail "http://127.0.0.1:$VIVI_STT_PORT/" >/dev/null; then ready=1; break; fi
    kill -0 "$stt_pid" 2>/dev/null || fail "STT exited; see whisper-server.log"
    sleep 1
  done
  (( ready == 1 )) || fail "STT did not become ready"
  export JETSON_AI_STT_LOAD_MS="$(((SECONDS-started)*1000))"
  export JETSON_AI_ENABLE_SPEECH=1 JETSON_AI_DEVICE=cuda JETSON_AI_STT_ENGINE=whisper_cpp \
    JETSON_AI_STT_MODEL="$VIVI_DATA/models/speech/whisper/ggml-base.bin" \
    JETSON_AI_STT_URL="http://127.0.0.1:$VIVI_STT_PORT" JETSON_AI_STT_PID="$stt_pid" \
    JETSON_AI_STT_LOG="$VIVI_DATA/logs/whisper-server.log" \
    JETSON_AI_TTS_MODEL="$VIVI_DATA/models/speech/zerotts" \
    JETSON_AI_LLM_MODEL_PATH="$VIVI_MODEL" JETSON_AI_LLM_LOG="$VIVI_DATA/logs/llama-server.log"
  export HF_HUB_OFFLINE=1 HF_HOME="$VIVI_DATA/cache/huggingface" \
    LD_LIBRARY_PATH="/usr/local/cuda/lib64:/usr/lib/aarch64-linux-gnu:${LD_LIBRARY_PATH:-}"
  echo "Jetson AI API: http://$bind:$VIVI_PORT (STT/TTS tiếp tục nạp ở nền)."
  echo "Giao diện test: http://$bind:$VIVI_PORT/test"
  cd "$VIVI_REPO"
  "$VIVI_DATA/venv/bin/python" -m uvicorn jetson_ai.app:app --host "$bind" --port "$VIVI_PORT" --workers 1 &
  local gateway_pid=$!
  trap 'kill "$gateway_pid" "$stt_pid" "$llm_pid" 2>/dev/null || true; wait "$gateway_pid" "$stt_pid" "$llm_pid" 2>/dev/null || true' EXIT INT TERM
  wait "$gateway_pid"
  kill "$stt_pid" "$llm_pid" 2>/dev/null || true
  wait "$stt_pid" "$llm_pid" 2>/dev/null || true
  trap - EXIT INT TERM
}

case "${1:-}" in
  setup) shift; setup "$@" ;;
  run) shift; [[ $# == 0 ]] || fail "Usage: bash scripts/jetson_ai.sh run"; run ;;
  *) fail "Usage: bash scripts/jetson_ai.sh {setup --profile agx|run}" ;;
esac
