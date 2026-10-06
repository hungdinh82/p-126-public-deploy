#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(git rev-parse --show-toplevel 2>/dev/null)" || {
  echo "Hãy chạy script bên trong repository ViVi." >&2
  exit 1
}
cd "$ROOT"

PYTHON="${VIVI_PYTHON:-.venv/bin/python}"
[ -x "$PYTHON" ] || PYTHON="python3"

prompt="${1:-}"
result="${2:-}"
if [ -z "$prompt" ]; then
  read -r -p "Codex đã làm gì? Mô tả ngắn: " prompt
fi
[ -n "$prompt" ] || { echo "Prompt không được để trống." >&2; exit 1; }
if [ -z "$result" ]; then
  read -r -p "Kết quả (tuỳ chọn): " result
fi

"$PYTHON" scripts/log_manual.py \
  --tool codex \
  --model "${CODEX_MODEL:-GPT-5}" \
  --prompt "$prompt" \
  --result "$result"
