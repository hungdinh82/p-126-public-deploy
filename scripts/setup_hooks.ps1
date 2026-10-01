# Enable the repository-managed quality and AI-log hooks (Windows PowerShell).
# Run once after cloning: powershell -ExecutionPolicy Bypass -File scripts\setup_hooks.ps1

$ErrorActionPreference = 'Stop'

git config core.hooksPath .githooks
if ($LASTEXITCODE -ne 0) { throw 'Could not configure core.hooksPath' }
Write-Host "[hooks] Local quality gate and AI-log submission enabled."

if (-not (Test-Path .ai-log)) { New-Item -ItemType Directory -Path .ai-log | Out-Null }
if (-not (Test-Path .ai-log/.gitkeep)) { New-Item -ItemType File -Path .ai-log/.gitkeep | Out-Null }

Write-Host "[hooks] Setup complete. Configure AI_LOG_SERVER in your .env file."
