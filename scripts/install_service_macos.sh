#!/usr/bin/env bash
# Registers a launchd LaunchAgent that launches Bot Server at login and
# restarts it automatically if it exits. macOS equivalent of
# scripts/install_task.ps1 (Windows Task Scheduler) and
# scripts/install_service.sh (Linux systemd --user) — same two behaviors
# (run at login, restart on failure), different OS mechanism, since macOS
# has neither of those.
#
# Run this once, as your normal user (no sudo — a LaunchAgent in
# ~/Library/LaunchAgents is scoped to your own login session, matching the
# per-user, no-elevation scope the other two platforms' scripts use).
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
exe="$root/desktop-app/src-tauri/target/release/bot-server"
run_script="$root/scripts/run.sh"
agents_dir="$HOME/Library/LaunchAgents"
plist="$agents_dir/com.botserver.app.plist"

mkdir -p "$agents_dir"

if [ -x "$exe" ]; then
    echo "Using built app: $exe"
    program_args="<string>$exe</string>"
else
    echo "No release build found at $exe — falling back to run.sh (headless, no GUI)."
    echo "Run 'cargo tauri build' in desktop-app/src-tauri first, then re-run this script, to launch the GUI at login instead."
    program_args="<string>/bin/bash</string>
        <string>$run_script</string>"
fi

cat > "$plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.botserver.app</string>
    <key>ProgramArguments</key>
    <array>
        $program_args
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <dict>
        <key>SuccessfulExit</key>
        <false/>
    </dict>
    <key>ThrottleInterval</key>
    <integer>60</integer>
    <key>StandardOutPath</key>
    <string>$root/logs/launchd.out.log</string>
    <key>StandardErrorPath</key>
    <string>$root/logs/launchd.err.log</string>
</dict>
</plist>
EOF

launchctl unload "$plist" >/dev/null 2>&1 || true
launchctl load "$plist"

echo "Registered LaunchAgent 'com.botserver.app' — runs at login, restarts on failure (unless it exits cleanly)."
echo "Start it now with: launchctl start com.botserver.app"
echo "Unregister later with: launchctl unload $plist && rm $plist"
