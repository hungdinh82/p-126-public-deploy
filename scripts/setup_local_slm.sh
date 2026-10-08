#!/usr/bin/env bash
# Provision once online; inference stays offline. Pinned artifacts, SHA-256 checked.
set -Eeuo pipefail
VIVI_ROOT="$(git rev-parse --show-toplevel)"
cd "$VIVI_ROOT"
VIVI_VARIANT="${1:-cpu}"
[[ "$VIVI_VARIANT" == cpu || "$VIVI_VARIANT" == cuda || "$VIVI_VARIANT" == model-only ]] || {
  echo "Usage: $0 [cpu|cuda|model-only]" >&2; exit 2;
}
fetch_checked() {
  local url="$1" target="$2" digest="$3"
  if [[ -f "$target" ]] && echo "$digest  $target" | sha256sum --check --status; then return; fi
  mkdir -p "$(dirname "$target")"
  curl --fail --location --retry 2 --connect-timeout 10 --max-time 1800 --continue-at - "$url" -o "$target.part"
  echo "$digest  $target.part" | sha256sum --check --status || {
    echo "Checksum mismatch: $target.part" >&2; return 1;
  }
  mv "$target.part" "$target"
}
fetch_checked \
  "https://huggingface.co/unsloth/Qwen3-1.7B-GGUF/resolve/d7f544eead698dbd1f15126ef60b45a1e1933222/Qwen3-1.7B-Q4_K_M.gguf" \
  models/vivi-slm/Qwen3-1.7B-Q4_K_M.gguf \
  b139949c5bd74937ad8ed8c8cf3d9ffb1e99c866c823204dc42c0d91fa181897
[[ "$VIVI_VARIANT" != model-only ]] || exit 0
[[ "$(uname -m)" == x86_64 ]] || {
  echo "On Jetson, build llama.cpp for its JetPack/CUDA toolchain and set LLAMA_SERVER_BIN. Model is ready." >&2; exit 1;
}
if [[ "$VIVI_VARIANT" == cuda ]]; then
  VIVI_ASSET=llama-b11447-bin-ubuntu-cuda-12.8-x64.tar.gz
  VIVI_DIGEST=019197b113a8efba0db928262262b4d52de090908587fe10cd87cdad40f8f0e9
  VIVI_DEST=models/llama-server-cuda
else
  VIVI_ASSET=llama-b11447-bin-ubuntu-x64.tar.gz
  VIVI_DIGEST=f256d5ea6c41153a6ceba37f8d4fe31a729e3296deb915937194660488666f9f
  VIVI_DEST=models/llama-server
fi
fetch_checked "https://github.com/ggml-org/llama.cpp/releases/download/b11447/$VIVI_ASSET" "models/downloads/$VIVI_ASSET" "$VIVI_DIGEST"
mkdir -p "$VIVI_DEST"
tar -xzf "models/downloads/$VIVI_ASSET" -C "$VIVI_DEST" --strip-components=1
echo "Ready: $VIVI_DEST/llama-server. GPU builds need a compatible host driver; see docs/dialogue_tasks.md."

