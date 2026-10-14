#!/usr/bin/env bash
# Shared paths/version selection. Sourcing this file never installs anything.
VIVI_REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VIVI_DATA="${JETSON_AI_DATA_DIR:-/mnt/jetson-data/vivi-ai}"
VIVI_PROFILE=agx-cu118-ort1201-py312
VIVI_PYTHON=3.12.15
VIVI_ORT=1.20.1
VIVI_ORT_FIX=ort-arm-cpuid-bounds-v1
VIVI_TRT_FIX=trt85-engine-inspection-v2
VIVI_CUDA=/usr/local/cuda-11.8
VIVI_VENV="$VIVI_DATA/envs/$VIVI_PROFILE/venv"
VIVI_NATIVE_BUILD=build-cu118-gcc10
VIVI_COMPLETE="$VIVI_DATA/$VIVI_PROFILE.complete"
VIVI_MANIFEST="$VIVI_DATA/$VIVI_PROFILE.json"

jetson_platform() {
  [[ "$(uname -m)" == aarch64 ]] || { echo 'AGX profile requires aarch64' >&2; return 1; }
  [[ -r /etc/nv_tegra_release ]] && grep -q 'R35' /etc/nv_tegra_release \
    || { echo 'AGX profile requires existing L4T R35; do not upgrade OS/JetPack' >&2; return 1; }
  # shellcheck disable=SC1091
  source /etc/os-release
  [[ "$ID" == ubuntu && "$VERSION_ID" == 20.04 ]] \
    || { echo 'AGX profile expects Ubuntu 20.04' >&2; return 1; }
  mountpoint -q "$(dirname "$VIVI_DATA")" \
    || { echo "Data mount is missing: $(dirname "$VIVI_DATA")" >&2; return 1; }
  [[ -w "$(dirname "$VIVI_DATA")" ]] \
    || { echo 'Data mount is not writable by the service user' >&2; return 1; }
}
jetson_sdk_check() {
  local missing=0
  if [[ ! -x "$VIVI_CUDA/bin/nvcc" ]] || \
     ! "$VIVI_CUDA/bin/nvcc" --version | grep -q 'V11.8.89'; then
    echo 'Missing CUDA nvcc 11.8.89' >&2; missing=1
  fi
  [[ -r "$VIVI_CUDA/compat/libcuda.so.1" ]] \
    || { echo 'Missing cuda-compat-11-8 driver libraries' >&2; missing=1; }
  # ORT's full CUDA provider includes cuSPARSE even without linking its API.
  # Check all required SDK headers before spending hours on a native build.
  local header
  for header in cuda.h cuda_runtime.h cuda_fp16.h cublas_v2.h cublasLt.h \
      cusparse.h curand.h cufft.h nvrtc.h; do
    [[ -r "$VIVI_CUDA/include/$header" ]] \
      || { echo "Missing CUDA 11.8 header: $header (run jetson_system.sh install)" >&2; missing=1; }
  done
  for tool in gcc-10 g++-10; do
    if [[ ! -x "/usr/bin/$tool" ]] || \
       [[ "$(/usr/bin/"$tool" -dumpfullversion 2>/dev/null)" != 10.5.0 ]]; then
      echo "Missing $tool 10.5.0" >&2; missing=1
    fi
  done
  for package in libcudnn8 libcudnn8-dev; do
    [[ "$(dpkg-query -W -f='${Version}' "$package" 2>/dev/null)" == 8.6.0.166-1+cuda11.4 ]] \
      || { echo "Expected $package 8.6.0.166-1+cuda11.4 from existing JetPack" >&2; missing=1; }
  done
  for tool in git curl openssl make; do
    command -v "$tool" >/dev/null || { echo "Missing $tool" >&2; missing=1; }
  done
  dpkg-query -W -f='${db:Status-Status}' libportaudio2 2>/dev/null | grep -qx installed \
    || { echo 'Missing libportaudio2 (ZeroTTS dependency)' >&2; missing=1; }
  (( missing == 0 ))
}
jetson_activate_sdk() {
  export CUDA_HOME="$VIVI_CUDA" CUDACXX="$VIVI_CUDA/bin/nvcc" \
    CC=/usr/bin/gcc-10 CXX=/usr/bin/g++-10 CUDAHOSTCXX=/usr/bin/g++-10
  export PATH="$VIVI_CUDA/bin:$HOME/.local/bin:$PATH"
  export LD_LIBRARY_PATH="$VIVI_CUDA/compat:$VIVI_CUDA/lib64:/usr/lib/aarch64-linux-gnu:${LD_LIBRARY_PATH:-}"
}

jetson_trt_check() {
  local package version
  for package in libnvinfer8 libnvinfer-dev libnvinfer-plugin8 libnvinfer-plugin-dev libnvonnxparsers8 libnvonnxparsers-dev; do
    version="$(dpkg-query -W -f='${Version}' "$package" 2>/dev/null)" || {
      echo "Missing $package; run bash scripts/jetson_system.sh install --tensorrt" >&2; return 1;
    }
    [[ "$version" =~ ^8\.5\.2([.-]|$) ]] || { echo "Expected JetPack TensorRT 8.5.2, got $package=$version" >&2; return 1; }
  done
  [[ -r /usr/include/aarch64-linux-gnu/NvInfer.h && -r /usr/include/aarch64-linux-gnu/NvInferPlugin.h ]] \
    || { echo 'TensorRT SDK headers missing' >&2; return 1; }
}
