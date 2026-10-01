#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(git rev-parse --show-toplevel 2>/dev/null)" || {
  echo "Run this script from inside the ViVi repository." >&2
  exit 1
}
cd "$ROOT"

if ! command -v docker >/dev/null 2>&1; then
  echo "Docker is not installed." >&2
  exit 1
fi
if ! docker info >/dev/null 2>&1; then
  echo "Docker is installed but its daemon is not running." >&2
  exit 1
fi

IMAGE="vivi-core:local-check"
CONTAINER="vivi-local-check-$$"

cleanup() {
  status=$?
  trap - EXIT INT TERM
  if docker container inspect "$CONTAINER" >/dev/null 2>&1; then
    if [ "$status" -ne 0 ]; then
      echo "Container logs:" >&2
      docker logs "$CONTAINER" >&2 || true
    fi
    docker rm --force "$CONTAINER" >/dev/null 2>&1 || true
  fi
  exit "$status"
}
trap cleanup EXIT INT TERM

echo "[1/4] Building the core image"
docker build --tag "$IMAGE" .

echo "[2/4] Starting the deterministic runtime"
test "$(docker run --rm "$IMAGE" id -u)" != "0"
docker run --detach --name "$CONTAINER" \
  --env APP_ENV=test \
  --env LLM_PROVIDER=rules \
  --env STT_PROVIDER=off \
  --env TTS_PROVIDER=off \
  --env VIVI_VEHICLE_PROVIDER=memory \
  --env RAG_RETRIEVAL_MODE=sqlite \
  "$IMAGE" >/dev/null

echo "[3/4] Waiting for health and running the API smoke test"
for attempt in $(seq 1 30); do
  if docker exec "$CONTAINER" python -c \
    "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8787/api/v1/health')" \
    >/dev/null 2>&1; then
    break
  fi
  if [ "$attempt" -eq 30 ]; then
    echo "ViVi container did not become healthy." >&2
    exit 1
  fi
  sleep 2
done
docker exec "$CONTAINER" python scripts/smoke_runtime.py --provider rules --timeout 30

echo "[4/4] Checking runtime isolation"
docker exec "$CONTAINER" sh -lc '
  test "$(id -u)" != "0"
  test ! -e /app/data/mqtt/credentials.env
  test ! -d /app/.ai-log
  test -f /app/data/handbooks/handbook.sqlite3
'

echo "ViVi Docker quality checks passed."
