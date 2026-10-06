# Installs the git hooks that run AgenticBotPlatform's local CI/CD pipeline (pre-push) and the
# public-repo leak guard (pre-commit). .git/hooks isn't version-controlled, so this copies the
# tracked sources in scripts/git-hooks/ into place — re-run any time to pick up hook changes.
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
New-Item -ItemType Directory -Force -Path (Join-Path $root ".git\hooks") | Out-Null
foreach ($hook in @("pre-commit", "pre-push")) {
    Copy-Item (Join-Path $root "scripts\git-hooks\$hook") (Join-Path $root ".git\hooks\$hook") -Force
}

Write-Host "Installed pre-commit hook - every commit now passes the public-repo leak guard." -ForegroundColor Green
Write-Host "Installed pre-push hook - every 'git push' now runs the local CI/CD pipeline first." -ForegroundColor Green
Write-Host "Run the guard over the whole tree any time with: python scripts\leak_guard.py"
Write-Host "Run the pipeline directly any time with: python scripts\local_pipeline.py"