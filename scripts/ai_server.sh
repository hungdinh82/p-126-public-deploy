#!/usr/bin/env bash
# Normal AI service lifecycle for PC or Jetson.
set -Eeuo pipefail
AI_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$AI_ROOT"
action="${1:-run}"
if [[ $# -gt 0 ]]; then shift; fi
if [[ "$(uname -m)" == aarch64 ]]; then
  case "$action" in
    setup) exec bash scripts/jetson_ai.sh setup --profile agx "$@" ;;
    run) exec bash scripts/jetson_ai.sh run "$@" ;;
    *) echo "Usage: bash scripts/ai_server.sh {setup|run}" >&2; exit 2 ;;
  esac
fi
case "$action" in
  setup)
    [[ $# == 0 ]] || { echo 'PC setup does not accept arguments.' >&2; exit 2; }
    bash scripts/setup_ai_ui.sh
    uv pip install --python .venv/bin/python -r requirements.txt
    ;;
  run)
    [[ -x .venv/bin/python ]] || { echo 'Run: bash scripts/ai_server.sh setup' >&2; exit 1; }
    exec .venv/bin/python -u scripts/run_ai_server.py "$@"
    ;;
  *) echo "Usage: bash scripts/ai_server.sh {setup|run}" >&2; exit 2 ;;
esac
