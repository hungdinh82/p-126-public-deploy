#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(git rev-parse --show-toplevel 2>/dev/null)" || {
  echo "Hãy chạy script bên trong repository ViVi." >&2
  exit 1
}
cd "$ROOT"

PYTHON="${VIVI_PYTHON:-.venv/bin/python}"
if [ ! -x "$PYTHON" ]; then
  echo "Thiếu $PYTHON. Chạy: bash scripts/setup.sh" >&2
  exit 1
fi

IFS=$'\t' read -r HOST PORT < <("$PYTHON" -c 'from src.vivi.config import settings; print(settings.host, settings.port, sep="\t")')
HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-8787}"

echo "ViVi Edge runtime"
echo "  URL: http://${HOST}:${PORT}"
echo "  Monitor: http://${HOST}:${PORT}/monitor"
if command -v nvidia-smi >/dev/null 2>&1; then
  echo "GPU: $(nvidia-smi --query-gpu=name --format=csv,noheader | head -1)"
else
  echo "GPU: không tìm thấy nvidia-smi; sẽ chạy CPU"
fi

exec "$PYTHON" run.py
