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

# Started as root (see the Dockerfile): give the unprivileged abp user the
# state it must write — mounts can arrive owned by any host uid — then drop
# privileges for the real process. Already non-root (e.g. `docker run --user`)?
# Just run it.
if [ "$(id -u)" = "0" ]; then
    for p in /app/data /app/logs /app/config /app/.env; do
        [ -e "$p" ] && chown -R abp:abp "$p" 2>/dev/null || true
    done
    exec setpriv --reuid=abp --regid=abp --init-groups "$@"
fi

exec "$@"
