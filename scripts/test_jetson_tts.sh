#!/usr/bin/env bash
# Serve the existing test UI with only the TTS model loaded.
set -Eeuo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/jetson_profile.sh"
jetson_activate_sdk
[[ -x "$VIVI_VENV/bin/python" ]] || { echo 'Missing Jetson venv' >&2; exit 1; }
if [[ -r "$VIVI_DATA/service.env" ]]; then
  set -a
  source "$VIVI_DATA/service.env"
  set +a
fi
export JETSON_AI_ENABLE_SPEECH=1 JETSON_AI_DEVICE=cuda JETSON_AI_TTS_PRECISION=int8-gpu
export JETSON_AI_TTS_MODEL="$VIVI_DATA/models/speech/zerotts"
export JETSON_AI_TRT_INSPECTOR="$VIVI_DATA/build/trt-inspect" HF_HUB_OFFLINE=1
# An empty STT path leaves that capability unavailable without loading a model.
export JETSON_AI_STT_ENGINE=whisper_cpp JETSON_AI_STT_MODEL=""
cd "$VIVI_REPO"
echo 'Open http://localhost:8789/test through SSH forwarding; wait for TTS ready.'
exec "$VIVI_VENV/bin/python" -m uvicorn jetson_ai.app:app --host 127.0.0.1 --port 8789 --workers 1
