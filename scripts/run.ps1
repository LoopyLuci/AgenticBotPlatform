# Run the bot + dashboard. Creates a venv on first run, and walks through
# setup interactively if required .env fields are missing or invalid.
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

if (-not (Test-Path ".venv")) {
    python -m venv .venv
}
& ".\.venv\Scripts\pip.exe" install -q -r requirements.txt

& ".\.venv\Scripts\python.exe" scripts\setup.py --check
if ($LASTEXITCODE -ne 0) {
    Write-Host ""
    Write-Host "Setup isn't complete yet — let's fix that." -ForegroundColor Yellow
    & ".\.venv\Scripts\python.exe" scripts\setup.py
    Write-Host ""
}

& ".\.venv\Scripts\python.exe" -m bot.main
