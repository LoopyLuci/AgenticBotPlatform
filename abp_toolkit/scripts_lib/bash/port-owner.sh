#!/usr/bin/env bash
# name: port-owner
# description: Which process is listening on a TCP port (Linux: ss, macOS: lsof)
# params: PORT
# safety: read
set -euo pipefail
port="${1:?usage: port-owner.sh PORT}"
if command -v ss >/dev/null; then ss -ltnp "sport = :$port" | tail -n +2 || true
elif command -v lsof >/dev/null; then lsof -nP -iTCP:"$port" -sTCP:LISTEN || echo "Nothing is listening on $port."
else netstat -ltnp 2>/dev/null | grep ":$port " || echo "Nothing is listening on $port."; fi
