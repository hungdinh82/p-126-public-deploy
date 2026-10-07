#!/usr/bin/env bash
set -Eeuo pipefail
cd "$(git rev-parse --show-toplevel)"
export RAG_RETRIEVAL_MODE=sqlite_local
export VIVI_RAG_LOCAL_ONLY=true
if [ -z "${LLM_PROVIDER:-}" ]; then
  LLM_PROVIDER="$(.venv/bin/python -c 'from src.vivi.config import settings; print("local" if settings.local_llm_model else "rules")')"
  export LLM_PROVIDER
fi
exec .venv/bin/python run.py
