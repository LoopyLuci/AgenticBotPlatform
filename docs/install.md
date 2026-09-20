# Installing and integrating ABP

Pick the row that matches what you are doing, then follow the linked steps.
Everything marked **works today** is in the current release; anything **planned**
is specified in [cicd/](cicd/README.md) but not built.

| I want to… | Use | State |
|---|---|---|
| Run ABP on my own Windows/macOS/Linux desktop | The installer from the [latest release](https://github.com/LoopyLuci/AgenticBotPlatform/releases/latest) (Windows), or `scripts/install.ps1` / `scripts/install.sh` from a checkout | works today |
| Run it on a headless server, no desktop | Bare metal: `python scripts/install.py --no-system-deps --no-build --yes` then `scripts/run.sh`, or Docker: `docker compose up -d --build` | works today |
| Embed it in another server (git submodule or container) | [embedding.md](embedding.md): set `ABP_HOME`, `DASHBOARD_PORT`, `DASHBOARD_TOKEN` in the *process environment* | works today |
| Keep it alive across reboots and crashes | `scripts/install_service.sh` (Linux), `scripts/install_service_macos.sh`, `scripts/install_task.ps1` (Windows) | works today |
| Use it from my phone | [mobile-access.md](mobile-access.md) — pair the Android app | works today |
| Control when and whether it updates itself | [cicd/configuration.md](cicd/configuration.md) | **planned** |
| A standby instance with automatic failover | [cicd/README.md](cicd/README.md) | **planned** |

## For an agent or a script

Everything below is non-interactive and reports its result in the exit code, so
an automated installer can check its own work.

```bash
# 1. Report what is missing, change nothing. Exit code 1 means incomplete.
python scripts/install.py --check
python scripts/install.py --check --json     # one JSON event per line, for parsing

# 2. Install without prompts. Safe to re-run: every step checks before it changes anything.
python scripts/install.py --yes --no-system-deps --no-build --no-autostart   # headless server
python scripts/install.py --yes                                              # desktop machine

# 3. Start it and verify it answers.
./scripts/run.sh &                            # or scripts\run.ps1 on Windows
curl -fsS http://127.0.0.1:8787/healthz       # {"status":"ok","db_ok":true,"server_id":"..."}
```

Installer flags: `--check`, `--yes`/`-y`, `--no-system-deps`, `--no-build`,
`--no-autostart`, `--dev`, `--json`. On Windows the bootstrap takes the
PowerShell spellings (`-Check`, `-Yes`, `-NoSystemDeps`, `-NoBuild`,
`-NoAutostart`, `-Dev`). NixOS is detected automatically; use `nix develop`.

`/healthz` is unauthenticated. It answers whether the database is reachable and
returns a random, non-secret `server_id` that identifies this install (a phone
uses it to tell its own server from another ABP on the same network).

## Embedding in your architecture

The settings that matter for an embedded ABP, and where each lives today:

| You want to control | Today | Planned |
|---|---|---|
| Where state is stored | `ABP_HOME` (process environment) | same |
| Address and port | `DASHBOARD_HOST`, `DASHBOARD_PORT` | `integration.api_bind` / `api_socket` |
| Authentication | `DASHBOARD_TOKEN` (supply your own; it is used as-is) | scoped tokens |
| Hot reload | off when `ABP_HOME` is set; `hot_reload_enabled` in `config/backends.yaml` | policy-controlled |
| LAN announcement | `ABP_DISABLE_MDNS=1` | `integration.*` |
| Updating ABP | update your submodule and restart | `update.*` with a locked host policy; `abp update --check --json` |
| Redundancy | run a second instance with its own `ABP_HOME` and `DASHBOARD_PORT` yourself | `standby.*` |

Rules that avoid the common mistakes:

- Set `ABP_HOME` **in the environment of the process that starts ABP**, not in
  ABP's `.env` (which lives inside it).
- Two instances must never share an `ABP_HOME`; give each its own home *and* port.
- Treat `DASHBOARD_TOKEN` as root on the host. Keep the dashboard on loopback
  or behind an authenticating proxy.
- Back up `ABP_HOME/data/` before updating; the database upgrades itself on start.

Full details: [embedding.md](embedding.md). The design for host-owned, locked
update and standby policy is in [cicd/configuration.md](cicd/configuration.md).

## Verifying an install

```bash
python scripts/install.py --check          # exit 0 when everything is present
curl -fsS http://127.0.0.1:8787/healthz    # ABP is up and its database answers
```
