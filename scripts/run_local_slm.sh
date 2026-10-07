#!/usr/bin/env bash
# Local inference only. This script never downloads weights.
set -Eeuo pipefail
VIVI_ROOT="$(git rev-parse --show-toplevel)"
cd "$VIVI_ROOT"
VIVI_SERVER="${LLAMA_SERVER_BIN:-$VIVI_ROOT/models/llama-server/llama-server}"
VIVI_MODEL="${VIVI_SLM_MODEL:-$VIVI_ROOT/models/vivi-slm/Qwen3-1.7B-Q4_K_M.gguf}"
[[ -x "$VIVI_SERVER" ]] || { echo "Missing llama-server: set LLAMA_SERVER_BIN." >&2; exit 1; }
[[ -f "$VIVI_MODEL" ]] || { echo "Missing GGUF: run scripts/setup_local_slm.sh while online first." >&2; exit 1; }
# Optional CUDA libraries installed alongside PyTorch, without changing the host.
for VIVI_LIB in "$VIVI_ROOT"/.venv/lib/python*/site-packages/nvidia/{cublas,cuda_runtime}/lib; do
  [[ ! -d "$VIVI_LIB" ]] || export LD_LIBRARY_PATH="$VIVI_LIB${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
done
exec "$VIVI_SERVER" -m "$VIVI_MODEL" --alias vivi-qwen3-1.7b \
  --host 127.0.0.1 --port "${VIVI_SLM_PORT:-1234}" \
  -c "${VIVI_SLM_CONTEXT:-4096}" -np 1 -t "${VIVI_SLM_THREADS:-4}" -tb "${VIVI_SLM_THREADS:-4}" \
  -ngl "${VIVI_SLM_GPU_LAYERS:-0}" --reasoning off "$@"

