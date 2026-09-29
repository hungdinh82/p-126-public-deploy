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
  echo "Missing .venv. Create it and install requirements-dev.txt first." >&2
  exit 1
fi

if ! command -v node >/dev/null 2>&1; then
  echo "Node.js is required for the frontend syntax check." >&2
  exit 1
fi

if ! command -v mosquitto >/dev/null 2>&1 && \
   [ ! -x /opt/homebrew/opt/mosquitto/sbin/mosquitto ] && \
   [ ! -x /usr/local/sbin/mosquitto ]; then
  echo "Mosquitto is required for MQTT integration tests." >&2
  echo "macOS: brew install mosquitto" >&2
  echo "Ubuntu: sudo apt-get install mosquitto mosquitto-clients" >&2
  exit 1
fi

if ! command -v mosquitto_passwd >/dev/null 2>&1; then
  echo "mosquitto_passwd is required for MQTT integration tests." >&2
  exit 1
fi

export APP_ENV=test
export LLM_PROVIDER=rules
export STT_PROVIDER=off
export TTS_PROVIDER=off
export VIVI_VEHICLE_PROVIDER=memory
export RAG_RETRIEVAL_MODE=sqlite
export PHOWHISPER_PRELOAD=false
export ZEROTTS_PRELOAD=false
export OPENAI_API_KEY=""
export GOOGLE_API_KEY=""
export LOCAL_LLM_MODEL=""

echo "[1/4] Checking whitespace"
git diff --check
git show --check --format= HEAD

echo "[2/4] Linting Python"
"$PYTHON" -m ruff check src vehicle_simulator tests

echo "[3/4] Checking frontend syntax"
node --check app.js

echo "[4/4] Running core and MQTT tests"
"$PYTHON" -m pytest tests -q --tb=short

echo "ViVi local quality checks passed."
