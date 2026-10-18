#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(git rev-parse --show-toplevel 2>/dev/null)" || {
  echo "Run this script from inside the ViVi repository." >&2
  exit 1
}
cd "$ROOT"

if [ -x ".venv/bin/python" ]; then
  PYTHON=".venv/bin/python"
elif [ -x ".venv/Scripts/python.exe" ]; then
  PYTHON=".venv/Scripts/python.exe"
else
  echo "Missing .venv. Create it and install requirements/requirements-dev.txt first." >&2
  exit 1
fi

if ! command -v node >/dev/null 2>&1; then
  echo "Node.js is required for the frontend syntax check." >&2
  exit 1
fi

export APP_ENV=test
export LLM_PROVIDER=google
export STT_PROVIDER=off
export TTS_PROVIDER=off
export VIVI_VEHICLE_PROVIDER=memory
export RAG_RETRIEVAL_MODE=sqlite
export PHOWHISPER_PRELOAD=false
export ZEROTTS_PRELOAD=false
export OPENAI_API_KEY=""
export GOOGLE_API_KEY=""
export LOCAL_LLM_MODEL=""

echo "[1/5] Checking whitespace"
git diff --check
git show --check --format= HEAD

echo "[2/5] Linting Python"
"$PYTHON" -m ruff check src vehicle_simulator tests fixtures eval/workflow_benchmark.py eval/navigation_controls_benchmark.py eval/semantic_benchmark.py

echo "[3/5] Checking JavaScript syntax"
for source in web/*.js; do node --check "$source"; done

echo "[4/5] Running JavaScript behavior tests"
node --test tests/frontend/*.cjs

echo "[5/5] Running isolated Python tests"
"$PYTHON" -m pytest tests -q --tb=short "$@"

echo "ViVi local quality checks passed."
