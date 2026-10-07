#!/bin/bash
# Setup môi trường phát triển tối thiểu cho ViVi trên PC.

set -e

echo "=== AI20K Project Setup ==="

# Có thể override, ví dụ: PYTHON_BIN=/usr/bin/python3.11 bash scripts/setup.sh
if [ -z "${PYTHON_BIN:-}" ]; then
    if command -v python3.12 >/dev/null 2>&1; then
        PYTHON_BIN=python3.12
    elif command -v python3.11 >/dev/null 2>&1; then
        PYTHON_BIN=python3.11
    else
        PYTHON_BIN=python3
    fi
fi

"$PYTHON_BIN" -c "import sys; assert sys.version_info >= (3, 11), 'Python 3.11+ required'"
echo "Python version OK: $($PYTHON_BIN --version)"

if ! command -v ffmpeg >/dev/null 2>&1; then
    echo "WARNING: ffmpeg is missing; text flow works but microphone/STT will return 503."
    echo "Install it with: sudo apt install ffmpeg  # Ubuntu/Debian"
    echo "                 brew install ffmpeg      # macOS"
fi

if [ ! -f data/handbooks/handbook.sqlite3 ]; then
    echo "ERROR: missing data/handbooks/handbook.sqlite3"
    echo "Pull the shared handbook artifact before starting ViVi."
    exit 1
fi

"$PYTHON_BIN" - <<'PY'
import sqlite3

path = "data/handbooks/handbook.sqlite3"
with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as connection:
    result = connection.execute("PRAGMA integrity_check").fetchone()[0]
    chunks = connection.execute("SELECT COUNT(*) FROM handbook_chunks").fetchone()[0]
if result != "ok":
    raise SystemExit(f"ERROR: invalid handbook database: {result}")
print(f"Handbook OK: {chunks} chunks")
PY

# Create virtual environment
"$PYTHON_BIN" -m venv .venv
source .venv/bin/activate

# Install local RAG and development tooling; retain an existing speech GPU runtime.
python -m pip install --upgrade pip
python -m pip install -r requirements-dev.txt
if ! python -c 'import onnxruntime' >/dev/null 2>&1; then
    python -m pip install -r requirements-rag.txt
fi
if [ ! -f models/multilingual-e5-small-int8/manifest.json ]; then
    python -m src.vivi.cli.prepare_embeddings
fi

# Create local secrets file if it does not exist. Shared runtime defaults live in config.toml.
if [ ! -f .env ]; then
    cp .env.example .env
    echo "Created .env with offline rules + memory simulator defaults"
fi

# Create data directories
mkdir -p data/handbooks

echo "Setup complete! Run: .venv/bin/python run.py"
echo "For STT/TTS/model setup, continue with docs/setup_pc.md"
