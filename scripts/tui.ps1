# Launch the BotServer terminal UI — connects to an already-running
# BotServer's dashboard (local or remote/federated), the terminal
# equivalent of the browser dashboard. See scripts/run.ps1 for the process
# that actually starts a BotServer instance, which this assumes is already
# running somewhere.
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

if (-not (Test-Path ".venv")) {
    python -m venv .venv
}
& ".\.venv\Scripts\pip.exe" install -q -r requirements.txt

& ".\.venv\Scripts\python.exe" -m bot.tui
