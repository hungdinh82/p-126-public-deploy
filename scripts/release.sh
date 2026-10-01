#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(git rev-parse --show-toplevel 2>/dev/null)" || {
  echo "Run this script from inside the ViVi repository." >&2
  exit 1
}
cd "$ROOT"

VERSION="${1:-}"
if [[ ! "$VERSION" =~ ^v[0-9]+\.[0-9]+\.[0-9]+([.-][0-9A-Za-z.-]+)?$ ]]; then
  echo "Usage: bash scripts/release.sh v0.1.0" >&2
  exit 1
fi
if [ "$(git branch --show-current)" != "main" ]; then
  echo "Releases must be created from main." >&2
  exit 1
fi
if [ -n "$(git status --porcelain)" ]; then
  echo "Working tree must be clean before release." >&2
  exit 1
fi
if ! command -v gh >/dev/null 2>&1; then
  echo "GitHub CLI is required: https://cli.github.com/" >&2
  exit 1
fi
gh auth status >/dev/null
if [ -x ".venv/bin/python" ]; then
  PYTHON=".venv/bin/python"
elif [ -x ".venv/Scripts/python.exe" ]; then
  PYTHON=".venv/Scripts/python.exe"
else
  echo "Missing .venv. Create it and install requirements-dev.txt first." >&2
  exit 1
fi

git fetch origin main --tags
if [ "$(git rev-parse HEAD)" != "$(git rev-parse origin/main)" ]; then
  echo "Local main must exactly match origin/main." >&2
  exit 1
fi
if gh release view "$VERSION" >/dev/null 2>&1; then
  echo "GitHub Release $VERSION already exists." >&2
  exit 1
fi

echo "Running release quality gates..."
bash scripts/check_local.sh
bash scripts/check_docker.sh

echo "Complete docs/RELEASE_CHECKLIST.md before publishing $VERSION."
read -r -p "Publish $VERSION from $(git rev-parse --short HEAD)? [y/N] " answer
case "$answer" in
  y|Y|yes|YES) ;;
  *) echo "Release cancelled."; exit 1 ;;
esac

EVIDENCE_DIR="$(mktemp -d "${TMPDIR:-/tmp}/vivi-release.XXXXXX")"
trap 'rm -rf "$EVIDENCE_DIR"' EXIT

RELEASE_VERSION="$VERSION" RELEASE_SHA="$(git rev-parse HEAD)" \
EVIDENCE_DIR="$EVIDENCE_DIR" "$PYTHON" - <<'PY'
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path

root = Path.cwd()
destination = Path(os.environ["EVIDENCE_DIR"])
files = [
    "Dockerfile",
    "requirements.txt",
    "requirements-ai.txt",
    "requirements-dev.txt",
    "data/handbooks/handbook.sqlite3",
    "voices/VIVI.zip",
]
manifest = {
    "schema_version": 1,
    "release": os.environ["RELEASE_VERSION"],
    "commit_sha": os.environ["RELEASE_SHA"],
    "created_at": datetime.now(timezone.utc).isoformat(),
    "python_target": "3.11",
    "runtime": "src.vivi.api.app:app",
    "validated_profile": {
        "llm": "rules",
        "stt": "off",
        "tts": "off",
        "vehicle": "memory",
        "retrieval": "sqlite",
    },
    "manual_gate": "docs/RELEASE_CHECKLIST.md",
}
(destination / "release-manifest.json").write_text(
    json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
    encoding="utf-8",
)
checksums = []
for relative in files:
    path = root / relative
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    checksums.append(f"{digest}  {relative}")
(destination / "SHA256SUMS").write_text("\n".join(checksums) + "\n", encoding="utf-8")
PY

if git rev-parse "$VERSION^{commit}" >/dev/null 2>&1; then
  if [ "$(git rev-parse "$VERSION^{commit}")" != "$(git rev-parse HEAD)" ]; then
    echo "Existing tag $VERSION points to another commit." >&2
    exit 1
  fi
else
  git tag -a "$VERSION" -m "ViVi $VERSION"
fi

if ! git ls-remote --exit-code --tags origin "refs/tags/$VERSION" >/dev/null 2>&1; then
  VIVI_SKIP_LOCAL_CHECKS=1 git push origin "$VERSION"
fi

gh release create "$VERSION" \
  "$EVIDENCE_DIR/release-manifest.json" \
  "$EVIDENCE_DIR/SHA256SUMS" \
  --title "ViVi $VERSION" \
  --generate-notes \
  --verify-tag

echo "Published ViVi $VERSION."
