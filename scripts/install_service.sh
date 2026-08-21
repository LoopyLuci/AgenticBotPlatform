#!/usr/bin/env bash
# Registers a systemd --user service that launches Bot Server at login and
# restarts it automatically if it exits. Linux equivalent of
# scripts/install_task.ps1 (Windows Task Scheduler) — same two behaviors
# (run at login, restart on failure), different OS mechanism, since
# Windows has no systemd to target and Linux has no Task Scheduler.
#
# Run this once, as your normal user (no sudo — this is a --user unit,
# scoped to your own login session, matching the per-user, no-elevation
# scope install_task.ps1 uses on Windows).
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
exe="$root/desktop-app/src-tauri/target/release/bot-server"
run_script="$root/scripts/run.sh"
unit_dir="$HOME/.config/systemd/user"
unit_file="$unit_dir/bot-server.service"

mkdir -p "$unit_dir"

if [ -x "$exe" ]; then
    echo "Using built app: $exe"
    exec_start="$exe"
else
    echo "No release build found at $exe — falling back to run.sh (headless, no GUI)."
    echo "Run 'cargo tauri build' in desktop-app/src-tauri first, then re-run this script, to launch the GUI at login instead."
    exec_start="$run_script"
fi

cat > "$unit_file" <<EOF
[Unit]
Description=Bot Server

[Service]
Type=simple
ExecStart=$exec_start
Restart=on-failure
RestartSec=60
StartLimitIntervalSec=300
StartLimitBurst=5

[Install]
WantedBy=default.target
EOF

systemctl --user daemon-reload
systemctl --user enable bot-server.service

echo "Registered systemd --user service 'bot-server' — runs at login, restarts on failure."
echo "Start it now with: systemctl --user start bot-server"
echo "Note: for this to run at login without an active graphical session, you may need:"
echo "  sudo loginctl enable-linger \$USER"
