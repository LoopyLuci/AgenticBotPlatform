#!/usr/bin/env bash
# Run the bot + dashboard on Linux/macOS. Creates a venv on first run, and
# walks through setup interactively if required .env fields are missing or
# invalid. Mirrors scripts/run.ps1 exactly (same three steps, same layout)
# so both platforms behave identically — see that file for the Windows
# equivalent.
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$root"

if [ ! -d .venv ]; then
    python3 -m venv .venv
fi
.venv/bin/pip install -q -r requirements.txt

if ! .venv/bin/python scripts/setup.py --check; then
    echo
    echo "Setup isn't complete yet — let's fix that."
    .venv/bin/python scripts/setup.py
    echo
fi

exec .venv/bin/python -m bot.main
