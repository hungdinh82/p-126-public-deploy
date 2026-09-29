#!/usr/bin/env bash
# Enable the repository-managed quality and AI-log hooks (POSIX / Git Bash).
# Run once after cloning: bash scripts/setup_hooks.sh
set -e

git config core.hooksPath .githooks
chmod +x .githooks/pre-push scripts/check_local.sh scripts/check_docker.sh scripts/release.sh
chmod +x scripts/_pyrun.sh 2>/dev/null || true
echo "[hooks] Local quality gate and AI-log submission enabled."

mkdir -p .ai-log
touch .ai-log/.gitkeep

echo "[hooks] Setup complete. Configure AI_LOG_SERVER in your .env file."
