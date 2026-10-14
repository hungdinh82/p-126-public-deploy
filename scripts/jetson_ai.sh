#!/usr/bin/env bash
# Native, user-space AI service for Jetson AGX Xavier (L4T R35.x).
set -Eeuo pipefail

source "$(dirname "${BASH_SOURCE[0]}")/jetson_profile.sh"
VIVI_MODEL="$VIVI_DATA/models/Qwen3-1.7B-Q4_K_M.gguf"
VIVI_MODEL_SHA=b139949c5bd74937ad8ed8c8cf3d9ffb1e99c866c823204dc42c0d91fa181897
VIVI_MODEL_URL=https://huggingface.co/unsloth/Qwen3-1.7B-GGUF/resolve/d7f544eead698dbd1f15126ef60b45a1e1933222/Qwen3-1.7B-Q4_K_M.gguf
VIVI_LLAMA_REF=b11447
VIVI_WHISPER_REF=v1.7.6
VIVI_BACKEND_PORT="${JETSON_AI_BACKEND_PORT:-1235}"
VIVI_PORT="${JETSON_AI_PORT:-8789}"
VIVI_STT_PORT="${JETSON_AI_STT_PORT:-1236}"

fail() { echo "jetson_ai: $*" >&2; exit 1; }
check_platform() { jetson_platform || fail "Platform preflight failed"; }
check_sdk() {
  jetson_sdk_check || fail "Install system prerequisites first: bash scripts/jetson_system.sh install"
  jetson_activate_sdk
}
check_space() {
  local available required=20971520
  available="$(df -Pk "$(dirname "$VIVI_DATA")" | awk 'NR==2 {print $4}')"
  if [[ -f "$VIVI_COMPLETE" ]] && \
     [[ -x "$VIVI_DATA/build/llama.cpp/$VIVI_NATIVE_BUILD/bin/llama-server" ]] && \
     [[ -f "$VIVI_MODEL" ]] && echo "$VIVI_MODEL_SHA  $VIVI_MODEL" | sha256sum -c --status; then
    required=524288
  fi
  (( available >= required )) || fail "Insufficient free space on the data mount (need $required KiB)"
}
find_python() {
  uv python install "$VIVI_PYTHON" >&2 || fail "Could not prepare private Python $VIVI_PYTHON"
  uv python find "$VIVI_PYTHON"
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
  local source="$VIVI_DATA/build/llama.cpp" binary="$VIVI_DATA/build/llama.cpp/$VIVI_NATIVE_BUILD/bin/llama-server"
  command -v cmake >/dev/null || fail "cmake is required to build llama.cpp"
  command -v git >/dev/null || fail "git is required to fetch llama.cpp"
  if [[ ! -d "$source/.git" ]]; then
    git clone --depth 1 --branch "$VIVI_LLAMA_REF" https://github.com/ggml-org/llama.cpp.git "$source" \
      || fail "Could not fetch pinned llama.cpp ref $VIVI_LLAMA_REF"
  fi
  [[ "$(git -C "$source" describe --tags --exact-match HEAD)" == "$VIVI_LLAMA_REF" ]] \
    || fail "llama.cpp checkout does not match $VIVI_LLAMA_REF"
  [[ -x "$binary" && -f "$VIVI_DATA/build/llama-mode-cu118-gcc10" && "$(cat "$VIVI_DATA/build/llama-mode-cu118-gcc10")" == cuda ]] && return
  cmake -S "$source" -B "$source/$VIVI_NATIVE_BUILD" -DGGML_CUDA=ON -DGGML_CUDA_NO_VMM=ON \
    -DLLAMA_CURL=OFF -DLLAMA_BUILD_TESTS=OFF \
    -DCMAKE_CUDA_ARCHITECTURES=72 -DCMAKE_CUDA_COMPILER="$VIVI_CUDA/bin/nvcc" \
    -DCMAKE_CUDA_HOST_COMPILER="$CXX" -DCMAKE_C_COMPILER="$CC" -DCMAKE_CXX_COMPILER="$CXX" \
    -DCMAKE_BUILD_TYPE=Release || fail "llama.cpp CUDA configure failed"
  cmake --build "$source/$VIVI_NATIVE_BUILD" --target llama-server -j 2 || fail "llama.cpp CUDA build failed (no CPU fallback)"
  echo cuda > "$VIVI_DATA/build/llama-mode-cu118-gcc10"
}
build_whisper() {
  local source="$VIVI_DATA/build/whisper.cpp"
  [[ -d "$source/.git" ]] || git clone --depth 1 --branch "$VIVI_WHISPER_REF" \
    https://github.com/ggml-org/whisper.cpp.git "$source"
  [[ "$(git -C "$source" describe --tags --exact-match HEAD)" == "$VIVI_WHISPER_REF" ]] \
    || fail "whisper.cpp checkout does not match $VIVI_WHISPER_REF"
  [[ -x "$source/$VIVI_NATIVE_BUILD/bin/whisper-server" ]] && return
  cmake -S "$source" -B "$source/$VIVI_NATIVE_BUILD" -DGGML_CUDA=ON -DGGML_CUDA_NO_VMM=ON \
    -DCMAKE_CUDA_ARCHITECTURES=72 -DCMAKE_CUDA_COMPILER="$VIVI_CUDA/bin/nvcc" \
    -DCMAKE_CUDA_HOST_COMPILER="$CXX" -DCMAKE_C_COMPILER="$CC" -DCMAKE_CXX_COMPILER="$CXX" \
    -DCMAKE_BUILD_TYPE=Release
  cmake --build "$source/$VIVI_NATIVE_BUILD" --target whisper-server -j 2
}
build_ort() {
  local python_bin="$VIVI_VENV/bin/python" source="$VIVI_DATA/build/onnxruntime-$VIVI_ORT"
  local installed_fix="$VIVI_VENV/ort-platform-fix"
  if [[ -f "$installed_fix" && "$(cat "$installed_fix")" == "$VIVI_ORT_FIX" ]] && \
     "$python_bin" -c 'import onnxruntime as o; assert o.__version__ == "1.20.1"; assert "CUDAExecutionProvider" in o.get_available_providers()' 2>/dev/null; then return; fi
  if [[ -n "${JETSON_AI_ORT_WHEEL:-}" ]]; then
    [[ -f "$JETSON_AI_ORT_WHEEL" && -n "${JETSON_AI_ORT_WHEEL_SHA256:-}" ]] \
      || fail "Custom wheel requires a local file and JETSON_AI_ORT_WHEEL_SHA256"
    [[ "${JETSON_AI_ORT_PLATFORM_FIX:-}" == "$VIVI_ORT_FIX" ]] \
      || fail "Custom ORT wheel must include $VIVI_ORT_FIX; set JETSON_AI_ORT_PLATFORM_FIX after verifying its build"
    echo "$JETSON_AI_ORT_WHEEL_SHA256  $JETSON_AI_ORT_WHEEL" | sha256sum -c --status \
      || fail "Custom ORT wheel checksum mismatch"
    uv pip install --python "$python_bin" --no-deps --reinstall --refresh "$JETSON_AI_ORT_WHEEL"
  else
    [[ -d "$source/.git" ]] || git clone --depth 1 --branch "v$VIVI_ORT" --recursive \
      --shallow-submodules https://github.com/microsoft/onnxruntime.git "$source"
    [[ "$(git -C "$source" describe --tags --exact-match HEAD)" == "v$VIVI_ORT" ]] \
      || fail "ORT checkout does not match v$VIVI_ORT"
    # GitLab's regenerated ZIP has identical source at the pinned commit.
    # Keep archive verification, using the verified current SHA1 only for Eigen.
    "$python_bin" "$VIVI_REPO/scripts/prepare_ort_metadata.py" "$source/cmake/deps.txt"
    "$python_bin" "$VIVI_REPO/scripts/prepare_ort_platform.py" "$source/onnxruntime/core/common/cpuid_info.cc"
    local wheels=("$source"/build/Release/dist/onnxruntime_gpu-1.20.1-cp312-cp312-linux_aarch64.whl)
    if [[ ! -f "${wheels[0]}" || ! -f "${wheels[0]}.sha256" || \
          ! -f "${wheels[0]}.platform-fix" || "$(cat "${wheels[0]}.platform-fix")" != "$VIVI_ORT_FIX" ]]; then
      rm -f "${wheels[0]}.platform-fix"
      (cd "$source"
       "$python_bin" tools/ci_build/build.py --config Release --build_wheel --parallel 2 \
         --skip_tests --nvcc_threads 1 --use_cuda --cuda_home "$VIVI_CUDA" --cudnn_home /usr \
         --cmake_path "$VIVI_VENV/bin/cmake" --build_dir "$source/build" \
         --compile_no_warning_as_error --cmake_extra_defines CMAKE_CUDA_ARCHITECTURES=72 \
         "CMAKE_CUDA_COMPILER=$VIVI_CUDA/bin/nvcc" "CMAKE_CUDA_HOST_COMPILER=$CXX" \
         "CMAKE_C_COMPILER=$CC" "CMAKE_CXX_COMPILER=$CXX" onnxruntime_BUILD_UNIT_TESTS=OFF)
      [[ -f "${wheels[0]}" ]] || fail "Native cp312/aarch64 CUDA wheel was not produced"
      sha256sum "${wheels[0]}" > "${wheels[0]}.sha256"
      printf '%s\n' "$VIVI_ORT_FIX" > "${wheels[0]}.platform-fix"
    fi
    sha256sum -c "${wheels[0]}.sha256" || fail "Cached ORT wheel checksum mismatch"
    uv pip install --python "$python_bin" --no-deps --reinstall --refresh "${wheels[0]}"
  fi
  "$python_bin" -c 'import onnxruntime as o; assert o.__version__ == "1.20.1"; assert "CUDAExecutionProvider" in o.get_available_providers()' \
    || fail "ORT 1.20.1 CUDA is required for this Xavier profile"
  printf '%s\n' "$VIVI_ORT_FIX" > "$installed_fix"
}
setup() {
  [[ "${1:-}" == --profile && "${2:-}" == agx ]] || fail "Usage: bash scripts/jetson_ai.sh setup --profile agx"
  check_platform
  check_sdk
  check_space
  mkdir -p "$VIVI_VENV" "$VIVI_DATA"/{models,build,cache,logs}
  rm -f "$VIVI_COMPLETE"
  cd "$VIVI_REPO"
  export UV_CACHE_DIR="$VIVI_DATA/cache/uv" PIP_CACHE_DIR="$VIVI_DATA/cache/pip" \
    UV_PYTHON_INSTALL_DIR="$VIVI_DATA/python" HF_HOME="$VIVI_DATA/cache/huggingface" TMPDIR="$VIVI_DATA/cache/tmp"
  mkdir -p "$TMPDIR"
  export PATH="$HOME/.local/bin:$PATH"
  command -v uv >/dev/null || fail "Install uv first; setup uses an isolated Python environment"
  local python_bin
  python_bin="$(find_python)"
  if [[ ! -x "$VIVI_VENV/bin/python" ]]; then
    uv venv --python "$python_bin" "$VIVI_VENV" || fail "Could not create private venv"
  fi
  "$VIVI_VENV/bin/python" -c 'import sys; assert sys.version_info[:3] == (3,12,15)' \
    || fail "Profile venv must use Python 3.12.15"
  uv pip install --python "$VIVI_VENV/bin/python" --only-binary :all: --require-hashes \
    -r "$VIVI_REPO/requirements-jetson-ai.lock" || fail "Locked dependency install failed"
  "$VIVI_VENV/bin/python" scripts/check_jetson_runtime.py --gpu-only
  download_model
  export PATH="$VIVI_VENV/bin:$PATH"
  build_llama
  build_whisper
  build_ort
  # ZeroTTS declares the CPU distribution name; the verified GPU distribution
  # supplies that same Python API. Validate dependencies without installing CPU ORT.
  uv pip install --python "$VIVI_VENV/bin/python" --no-deps --only-binary :all: --require-hashes \
    -r "$VIVI_REPO/requirements-jetson-zerotts.lock"
  "$VIVI_VENV/bin/python" scripts/check_jetson_runtime.py
  "$VIVI_VENV/bin/python" scripts/prepare_speech_models.py --root "$VIVI_DATA/models/speech" --engine whisper_cpp
  "$VIVI_VENV/bin/python" scripts/smoke_jetson_profile.py --data "$VIVI_DATA" \
    --native-build "$VIVI_NATIVE_BUILD" --model "$VIVI_MODEL" \
    --backend-port "$VIVI_BACKEND_PORT" --stt-port "$VIVI_STT_PORT"
  if [[ ! -f "$VIVI_DATA/service.env" ]]; then
    umask 077
    command -v openssl >/dev/null || fail "openssl is required to create the service token"
    local token
    token="$(openssl rand -hex 32)" || fail "Could not generate service token"
    printf 'JETSON_AI_TOKEN=%s\n' "$token" > "$VIVI_DATA/service.env"
    chmod 600 "$VIVI_DATA/service.env"
  fi
  (set -a; source "$VIVI_DATA/service.env"; set +a
   "$VIVI_VENV/bin/python" -c 'from jetson_ai.app import app; assert app is not None') \
    || fail "Gateway import check failed"
  "$VIVI_VENV/bin/python" scripts/check_jetson_runtime.py --write-manifest "$VIVI_MANIFEST"
  touch "$VIVI_COMPLETE"
  echo "Setup complete: SLM + STT + TTS CUDA."
  echo "Service token: $VIVI_DATA/service.env (copy securely to Laptop)"
  if command -v tailscale >/dev/null 2>&1; then
    echo "Tailscale URL: http://$(tailscale ip -4 2>/dev/null | head -n 1):$VIVI_PORT"
  fi
  echo "Run: bash scripts/jetson_ai.sh run"
}
run() {
  check_platform
  check_sdk
  [[ -x "$VIVI_VENV/bin/python" ]] || fail "Run setup first: missing venv"
  [[ -x "$VIVI_DATA/build/llama.cpp/$VIVI_NATIVE_BUILD/bin/llama-server" ]] || fail "Run setup first: missing llama-server"
  [[ -f "$VIVI_COMPLETE" ]] || fail "Setup incomplete for $VIVI_PROFILE; run setup --profile agx"
  "$VIVI_VENV/bin/python" "$VIVI_REPO/scripts/check_jetson_runtime.py" --check-manifest "$VIVI_MANIFEST"
  local auth_required
  auth_required="$(cd "$VIVI_REPO" && "$VIVI_VENV/bin/python" -c 'from jetson_ai.config import auth_required; print(int(auth_required()))')"
  [[ "$auth_required" == 0 || -r "$VIVI_DATA/service.env" ]] || fail "Setup incomplete: missing service token"
  [[ -x "$VIVI_DATA/build/whisper.cpp/$VIVI_NATIVE_BUILD/bin/whisper-server" ]] || fail "Run setup again: missing STT server"
  [[ -f "$VIVI_DATA/models/speech/whisper/ggml-base.bin" ]] || fail "Run setup again: missing speech weights"
  echo "$VIVI_MODEL_SHA  $VIVI_MODEL" | sha256sum -c --status || fail "Model missing or checksum mismatch"
  set -a
  # Created only by setup and readable by the service user.
  if [[ -r "$VIVI_DATA/service.env" ]]; then source "$VIVI_DATA/service.env"; fi
  set +a
  export JETSON_AI_LLM_URL="http://127.0.0.1:$VIVI_BACKEND_PORT"
  # The Laptop reaches the gateway through the direct USB-C subnet
  # (Jetson 192.168.55.1). Bearer auth is mandatory in the vehicle profile, so
  # listening on all Jetson interfaces is safe and avoids Tailscale-dependent
  # automatic routing. Tailscale remains available for manual maintenance.
  local bind="${JETSON_AI_BIND:-0.0.0.0}"
  [[ "$(cat "$VIVI_DATA/build/llama-mode-cu118-gcc10")" == cuda ]] || fail "Run setup again: a CUDA build is required"
  local gpu_layers=99 started=$SECONDS
  "$VIVI_DATA/build/llama.cpp/$VIVI_NATIVE_BUILD/bin/llama-server" \
    --model "$VIVI_MODEL" --alias vivi-qwen3-1.7b --host 127.0.0.1 \
    --port "$VIVI_BACKEND_PORT" --ctx-size 4096 --parallel 1 \
    --n-gpu-layers "$gpu_layers" --reasoning off --log-verbosity 4 \
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
  "$VIVI_DATA/build/whisper.cpp/$VIVI_NATIVE_BUILD/bin/whisper-server" \
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
  export HF_HUB_OFFLINE=1 HF_HOME="$VIVI_DATA/cache/huggingface"
  echo "Jetson AI API: http://$bind:$VIVI_PORT (STT/TTS tiếp tục nạp ở nền)."
  echo "Giao diện test: http://$bind:$VIVI_PORT/test"
  cd "$VIVI_REPO"
  "$VIVI_VENV/bin/python" -m uvicorn jetson_ai.app:app --host "$bind" --port "$VIVI_PORT" --workers 1 &
  local gateway_pid=$!
  trap 'kill "$gateway_pid" "$stt_pid" "$llm_pid" 2>/dev/null || true; wait "$gateway_pid" "$stt_pid" "$llm_pid" 2>/dev/null || true' EXIT INT TERM
  wait "$gateway_pid"
  kill "$stt_pid" "$llm_pid" 2>/dev/null || true
  wait "$stt_pid" "$llm_pid" 2>/dev/null || true
  trap - EXIT INT TERM
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
case "${1:-}" in
  setup) shift; setup "$@" ;;
  run) shift; [[ $# == 0 ]] || fail "Usage: bash scripts/jetson_ai.sh run"; run ;;
  doctor) check_platform; check_sdk; [[ -x "$VIVI_VENV/bin/python" ]] || fail "Run setup first: missing profile venv"
    "$VIVI_VENV/bin/python" "$VIVI_REPO/scripts/check_jetson_runtime.py" ;;
  *) fail "Usage: bash scripts/jetson_ai.sh {setup --profile agx|run|doctor}" ;;
esac
fi
