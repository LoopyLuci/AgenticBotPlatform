# Headless server: the "api" backend plus the Telegram/Discord/Slack
# platform adapters and the dashboard API. The `cli` and `ui` backends
# need a real, locally-installed Claude Code CLI / Claude Desktop, which
# a Linux container can't provide — those stay Windows/macOS-desktop-only,
# same as documented in the README's Docker section.
FROM python:3.11-slim

WORKDIR /app

# libmagic-free Pillow/qrcode wheels cover everything requirements.txt
# needs; no compiler toolchain required beyond what pip's manylinux
# wheels already bring.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot ./bot
# Kept twice: /app/config is where the app reads/writes its live config
# (and gets bind-mounted over for persistence), /app/config.default is
# the entrypoint's seed source for a fresh or emptied mount — see
# scripts/docker-entrypoint.sh.
COPY config ./config
COPY config ./config.default
COPY scripts/docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

ENV DASHBOARD_HOST=0.0.0.0
ENV PYTHONUNBUFFERED=1

VOLUME ["/app/data", "/app/config"]
EXPOSE 8787

ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["python", "-m", "bot.main"]
