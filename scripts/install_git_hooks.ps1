# Installs the pre-push hook that runs BotServer's local CI/CD pipeline
# (scripts/local_pipeline.py) before every push. .git/hooks isn't
# version-controlled, so this copies the tracked source in
# scripts/git-hooks/ into place — re-run any time to pick up hook changes.
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Copy-Item (Join-Path $root "scripts\git-hooks\pre-push") (Join-Path $root ".git\hooks\pre-push") -Force

Write-Host "Installed pre-push hook — every 'git push' now runs the local CI/CD pipeline first." -ForegroundColor Green
Write-Host "Run it directly any time with: python scripts\local_pipeline.py"
