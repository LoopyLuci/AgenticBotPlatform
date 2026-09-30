#!/usr/bin/env bash
# name: backup-rotate
# description: Tar+gzip a folder into a timestamped archive and keep only the newest N
# params: SOURCE DEST [KEEP=10]
# safety: changes
set -euo pipefail
src="${1:?usage: backup-rotate.sh SOURCE DEST [KEEP]}"
dest="${2:?usage: backup-rotate.sh SOURCE DEST [KEEP]}"
keep="${3:-10}"
mkdir -p "$dest"
name="$(basename "$src")-$(date +%Y%m%d-%H%M%S).tar.gz"
tar -czf "$dest/$name" -C "$(dirname "$src")" "$(basename "$src")"
echo "Created $dest/$name ($(du -h "$dest/$name" | cut -f1))"
ls -1t "$dest/$(basename "$src")"-*.tar.gz 2>/dev/null | tail -n +"$((keep + 1))" | while read -r old; do rm -f -- "$old"; echo "Removed $old"; done
