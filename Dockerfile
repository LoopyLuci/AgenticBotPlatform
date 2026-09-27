# Headless server: the "api" backend plus the Telegram/Discord/Slack
# platform adapters and the dashboard API. The `cli` and `ui` backends
# need a real, locally-installed Claude Code CLI / Claude Desktop, which
# a Linux container can't provide — those stay Windows/macOS-desktop-only,
# same as documented in the README's Docker section.
#
# The base image is pinned by digest so a rebuild years from now produces the
# same OS layer. To move to a newer patch release on purpose, look up the new
# digest of python:3.11-slim and replace it here (and re-run the test suite).
FROM python:3.11-slim@sha256:e41613d42d4891e4930f79523f93f81bbc7632584ec65e36ab055f41a800b41e

WORKDIR /app

# libmagic-free Pillow/qrcode wheels cover everything requirements.lock
# needs; no compiler toolchain required beyond what pip's manylinux
# wheels already bring.
# requirements.lock pins every package (transitive ones included) to an exact
# version and hash, so the image is reproducible and tamper-evident.
COPY requirements.lock .
RUN pip install --no-cache-dir --require-hashes -r requirements.lock

COPY bot ./bot
COPY abp_cicd ./abp_cicd
# Kept twice: /app/config is where the app reads/writes its live config
# (and gets bind-mounted over for persistence), /app/config.default is
# the entrypoint's seed source for a fresh or emptied mount — see
# scripts/docker-entrypoint.sh.
# ONLY the tracked, keyless default. `COPY config ./config` also swept in the
# gitignored config/providers.yaml (provider API keys) from whatever checkout
# built the image, baking the builder's secrets into every layer.
COPY config/backends.yaml ./config/backends.yaml
COPY config/backends.yaml ./config.default/backends.yaml
COPY scripts/docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

# ABP never runs as root: the entrypoint starts as root only long enough to
# hand the writable paths (including a bind-mounted .env or data dir whose
# host ownership it can't predict) to the unprivileged `abp` user, then drops
# to it with setpriv. /app itself is handed over (not recursively) so .env's
# atomic temp-file write can land beside it; the code under /app/bot stays
# root-owned and read-only.
RUN useradd --system --uid 10001 --home-dir /app abp \
    && mkdir -p /app/data /app/logs \
    && chown abp:abp /app \
    && chown -R abp:abp /app/data /app/logs /app/config /app/config.default

ENV DASHBOARD_HOST=0.0.0.0
ENV PYTHONUNBUFFERED=1

VOLUME ["/app/data", "/app/config"]
EXPOSE 8787

# /healthz answers 503 when the database is unreachable; Docker then marks the
# container unhealthy and `restart: unless-stopped` + an orchestrator can act.
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8787/healthz', timeout=4).status == 200 else 1)"

ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["python", "-m", "bot.main"]
