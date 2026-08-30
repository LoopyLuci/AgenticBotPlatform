#!/usr/bin/env bash
# Launch the BotServer terminal UI (Linux/macOS) — connects to an already-
# running BotServer's dashboard (local or remote/federated), the terminal
# equivalent of the browser dashboard. Mirrors scripts/tui.ps1 exactly; see
# scripts/run.sh for the process that actually starts a BotServer instance,
# which this assumes is already running somewhere.
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$root"

if [ ! -d .venv ]; then
    python3 -m venv .venv
fi
.venv/bin/pip install -q -r requirements.txt

exec .venv/bin/python -m bot.tui
