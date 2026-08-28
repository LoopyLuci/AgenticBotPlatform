#!/bin/sh
# Mounting an empty host directory over /app/config (so config edits made
# from the dashboard survive a container recreate) shadows the default
# backends.yaml baked into the image at build time. Seed it back in on
# first run — same idea as scripts/setup.py's re-run safety: only touch
# what's actually missing.
set -e

if [ ! -f /app/config/backends.yaml ] && [ -f /app/config.default/backends.yaml ]; then
    cp /app/config.default/backends.yaml /app/config/backends.yaml
fi

exec "$@"
