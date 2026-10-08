#!/usr/bin/env bash
set -Eeuo pipefail
TASK_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$TASK_ROOT"
if [[ "$(uname -m)" == aarch64 ]]; then
  [[ $# == 0 ]] || { echo 'Trên Xavier, chọn port/device bằng JETSON_AI_* env.' >&2; exit 2; }
  [[ -f "${JETSON_AI_DATA_DIR:-/mnt/jetson-data/vivi-ai}/speech-setup-complete" ]] || \
    bash scripts/jetson_ai.sh setup --profile agx
  exec bash scripts/jetson_ai.sh run
fi
if [[ ! -f models/ai-speech/.setup-complete ]] || \
   ! .venv/bin/python -c 'import importlib.util; assert all(importlib.util.find_spec(n) for n in ("llama_cpp", "zerotts", "transformers", "multipart"))' >/dev/null 2>&1; then
  bash scripts/setup_ai_ui.sh
fi
exec .venv/bin/python -u scripts/test_ai_ui.py "$@"
