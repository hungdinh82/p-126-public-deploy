#!/usr/bin/env bash
# One command: prepare a real local model, start the Jetson gateway, then chat.
set -Eeuo pipefail
VIVI_TEST_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
[[ -x "$VIVI_TEST_ROOT/.venv/bin/python" ]] || {
  echo "Missing .venv; install the project's Python dependencies first." >&2; exit 1;
}
exec "$VIVI_TEST_ROOT/.venv/bin/python" -u "$VIVI_TEST_ROOT/scripts/test_pipeline.py" \
  --mode local --target llm "$@"
