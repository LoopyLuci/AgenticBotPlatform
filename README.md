# Bot Server

A messaging bot — Telegram, Discord, and/or Slack, set up any of them any
time from the dashboard's Platforms tab — that controls Claude on this
machine through three interchangeable backends (Anthropic API, Claude Code
CLI, and best-effort UI automation of Claude Desktop). Everything —
starting the bot, watching it boot, and controlling it afterward — happens
inside one desktop app; there's no separate script-then-browser step.

Companion design docs (published earlier in this project's chat):
architecture spec and dashboard mockup. This repo is the working
implementation of both, plus the desktop shell.

## What's here

```
bot/                   the Python server (messaging bot + dashboard API)
  main.py                entrypoint — runs every configured platform + dashboard together
  config.py               hot-reloadable config/backends.yaml loader
  db.py                    SQLite (WAL) storage: jobs, telemetry, audit, config history, messages
  auth.py                 Telegram user allowlist
  outbox.py                registry that lets the dashboard's Chat tab send through any connected platform
  envfile.py               resolves which .env to load secrets from
  setup_wizard.py           validates + writes .env fields, incl. per-platform fields (shared by scripts/setup.py and /api/setup/*, /api/platforms/*)
  mcp_server.py              stdio MCP server exposing the dashboard API as MCP tools
  router.py                backend resolution + backup-chain retry
  desktop.py                Claude Desktop process control + MCP config management
  handlers.py                Telegram command handlers
  platforms/
    discord_platform.py     Discord bot (discord.py)
    slack_platform.py        Slack bot (slack_bolt, Socket Mode)
  backends/
    api_backend.py          Anthropic API
    cli_backend.py           Claude Code CLI (headless)
    ui_backend.py             pywinauto automation of the Desktop window
  dashboard/
    server.py                FastAPI REST API
    static/dashboard.html      standalone copy of the dashboard (plain-browser fallback)
desktop-app/            the Tauri desktop shell — this is what you actually run
  src-tauri/               Rust: spawns/supervises bot/main.py, streams its
                            log + resource usage to the GUI, exposes
                            start/stop/restart as commands
  ui/                       the dashboard UI (index.html + main.js), with a
                            boot-terminal overlay layered on top
scripts/setup.py        interactive first-run wizard (also: --check, --all)
config/backends.yaml    router config — hot-reloaded on save
data/bot.db              created on first run
data/env_backups/        timestamped .env snapshots, one per save/restore — never pruned
logs/bot.log              rotating log file, also tailed by the dashboard
```

## Setup

1. **Python 3.11+**, **Rust + Cargo**, and the **Tauri CLI**
   (`cargo install tauri-cli --version "^2"`) on PATH.
2. Create the venv (also installs everything in `requirements.txt`), then
   walk through setup:
   ```powershell
   .\scripts\run.ps1
   ```
   The first run has nothing configured yet, so this drops you into
   `scripts\setup.py` automatically — a short interactive wizard that asks
   only for what's actually missing, validates each value's *format*
   before accepting it (a Telegram token that doesn't look like
   `123456789:AAExample...` gets rejected on the spot, not three steps
   later), generates `DASHBOARD_TOKEN` for you, and offers to auto-detect
   Claude Desktop's install path. It writes through the same backed-up,
   atomic path as every other .env write in this app — re-running it later
   is always safe. Once it reports "Ready to run", Ctrl+C out and move to
   step 3 — from here on you'll use the desktop app day to day.

   Prefer to do this yourself: `python scripts\setup.py --check` prints
   status without prompting (exit code 1 if incomplete); `--all`
   reconfigures every field, even already-valid ones.

   **You don't need the CLI at all**, actually — skip straight to the
   desktop app (step 3). If setup isn't complete, it shows the identical
   wizard as its first screen, no terminal required.

   **Where `.env` lives:** `bot/envfile.py` auto-detects it, checked in
   order: an explicit override in `config/backends.yaml`'s `env_file` key
   (editable from Control Center -> Environment), then this project's own
   `/path/redacted`, then a global `~/.claude/.env`
   shared with other Claude tooling. Whichever is found first wins. A
   location change takes effect on the next restart.
3. **Development:**
   ```powershell
   cd desktop-app\src-tauri
   cargo tauri dev
   ```
   **Production build** (produces `bot-server.exe`, with the venv
   and bot code bundled alongside it so it runs standalone):
   ```powershell
   cd desktop-app\src-tauri
   cargo tauri build
   ```
   The installer/exe lands under `desktop-app/src-tauri/target/release/`
   (and `target/release/bundle/` for the MSI/NSIS installer). A build
   doesn't copy your `.env` into the bundle — put one next to the built
   exe (or point `env_file` at your real one) before running it standalone.

## Using the desktop app

Launch `bot-server.exe` (or `cargo tauri dev` while developing).
The window opens straight into a terminal-style boot screen — it spawns
`python -m bot.main` itself, streams every log line and a live CPU/RAM
reading for that process as it comes up, then automatically swaps over to
the dashboard once the server answers. From there:

- **Stop server / Restart server** (top bar) control the Python process
  itself — separate from the dashboard's own Desktop start/stop/restart,
  which controls Claude Desktop, a different process entirely.
- Closing the window shuts the whole process tree down cleanly — no
  process is left running in the background after you close it.
- If the process crashes or is stopped, the boot overlay reappears with
  the last log lines and a Restart button, instead of leaving you looking
  at a dead dashboard.
- **The dashboard token fills itself in.** The desktop app reads
  `DASHBOARD_TOKEN` straight out of the resolved `.env` on boot (via a
  Tauri command that shells out to `python -m bot.envfile --print-token`)
  and unlocks every control action automatically — no more pasting it in
  by hand. **Set token** (top bar) still exists as a manual override, and
  is the only option in the browser-fallback dashboard, a different trust
  boundary where auto-reading the token isn't appropriate.
- No console window ever flashes up behind the app — every process it
  spawns (`python.exe`, `taskkill.exe`) is launched with `CREATE_NO_WINDOW`.

`bot/dashboard/static/dashboard.html` still exists and still works if you
ever want to hit `http://127.0.0.1:8787` from a plain browser instead —
useful for checking the server from another device on the same machine's
network, though the desktop app is the intended day-to-day way to run this.

To run the desktop app automatically at login:
```powershell
.\scripts\install_task.ps1
```
(edit it to point at the built `.exe` instead of `run.ps1` if you've
switched over fully — see the script for the one line to change).

## The setup wizard

Both the terminal (`scripts\setup.py`) and the GUI (shown automatically by
the desktop app, or reopen it any time from Control Center -> Environment
-> "Open setup wizard") walk the same core fields — Anthropic API key,
dashboard token, plus the optional Claude Desktop path — and share one
validator (`bot/setup_wizard.py`) so a field that passes in one passes in
the other. Platform credentials (Telegram bot token, Discord bot token,
Slack tokens, and each platform's allowed user IDs) are separate: at least
one platform has to be configured before the wizard reports "Ready", from
its own **Platforms** tab — see below.

What "as easy as possible" means concretely here:
- **Format validation, not just presence.** A pasted value that doesn't
  look like a real token/key gets flagged immediately with what's wrong,
  rather than failing silently at runtime three steps later.
- **`DASHBOARD_TOKEN` generates itself** — a Generate button in the GUI,
  automatic in the CLI, either way you never need to run a separate
  `python -c "import secrets..."` command by hand.
- **Claude Desktop's path auto-detects** — Auto-detect in the GUI, tried
  automatically in the CLI, via the same `desktop.find_exe_path()` logic
  the rest of the app uses.
- **Already-valid fields are pre-recognized and skippable** — the wizard
  only makes you deal with what's actually missing or broken; re-running
  it after initial setup to fix one field doesn't make you redo the rest.
- **Nothing here is a special code path from the .env editor** — Save
  routes through `envfile.write_content()`, so it's backed up first like
  every other edit, and the GUI wizard is real dashboard API calls
  (`/api/setup/*`), not a one-off first-run script that diverges from how
  the app works afterward.

The setup endpoints use the same bootstrap-or-token rule as the `.env`
editor (see below) — open when no `DASHBOARD_TOKEN` exists yet, so the
wizard can set the very first one; token-gated the instant a real one is
saved.

## Editing .env from the dashboard

Control Center -> Environment has, below the path picker:

- **A textarea with the live contents of the resolved `.env` file.** Loads
  once you've set the dashboard token (Set token, top bar); "Reload from
  disk" re-fetches it if something else changed the file underneath you.
- **Save (backs up first)** — writes the textarea back to disk. Every save
  snapshots whatever was there beforehand into
  `data/env_backups/env-<timestamp>.env` first, via an atomic write (temp
  file + rename), so a bad edit can never corrupt the file mid-write and
  the previous version is never gone. Takes effect on the next server
  restart — secrets are only read once at startup.
- **A backups table** — every snapshot, newest first, each with a
  **Restore** button. Restoring backs up the current file too (so a
  restore is itself always undoable), then overwrites `.env` with the
  chosen snapshot. Backups are never auto-deleted; prune
  `data/env_backups/` by hand if it ever grows large enough to matter (in
  practice: essentially never, for a file this small).

This reads and writes actual secret values in plain text, so both
`/api/env/content` endpoints (unlike most of the read-only API) require
the dashboard token even for `GET`. `data/env_backups/` is gitignored for
the same reason — never commit it.

## Messaging platforms

Telegram, Discord, and Slack are each self-contained in `bot/platforms/`
(Telegram's wiring lives in `bot/handlers.py` + `bot/main.py` instead, since
it predates the others) and share one pipeline: every allowed message
becomes a `bot.router.ask()` call, and every message either direction is
logged via `bot.db.log_message()` — so the dashboard's **Chat** tab and
**Jobs** table don't need to know which platform any given message came
from. None of them is required; run with just Telegram, just Discord, just
Slack, or any combination — the app only requires *at least one*.

Set up, reconfigure, or add a platform any time from the dashboard's
**Platforms** tab (always accessible, not just during first-run setup) —
each has its own in-GUI step-by-step guide, and saving writes straight
through the same backed-up `.env` path as everything else. A platform
change takes effect on the next server restart. Per-platform requirements:

- **Telegram** — a bot token from [@BotFather](https://t.me/BotFather),
  plus your numeric user ID from [@userinfobot](https://t.me/userinfobot).
- **Discord** — a bot token + "Message Content Intent" enabled from the
  [Developer Portal](https://discord.com/developers/applications), the bot
  invited to a server you own, plus your numeric Discord user ID.
- **Slack** — a Socket Mode app (no public webhook needed, so it works
  local-first): a bot token (`xoxb-...`) and an app-level token
  (`xapp-...`) from [api.slack.com/apps](https://api.slack.com/apps), plus
  your Slack member ID.

The dashboard's **Chat** tab is platform-aware — pick a platform from its
own dropdown, see the real conversation across whichever ones are
connected, and send a message straight to the phone/laptop it's logged in
on, through `bot/outbox.py`'s registry of whichever platforms are actually
running.

## Using the bot

Plain text messages on any platform are routed through `/ask`. The slash
commands below are Telegram-specific (`bot/handlers.py`); Discord and Slack
currently only handle plain-text prompts.

- `/ask <text> [--backend=api|cli|ui]` — send a prompt. Plain text messages
  (no leading `/`) are treated as `/ask` too.
- `/status` — health snapshot.
- `/backend show` / `/backend set <action|default> <api|cli|ui>` — view or
  edit routing without touching the YAML.
- `/mcp list` / `/mcp enable <name>` / `/mcp disable <name>` / `/mcp logs <name>`
- `/start_desktop` / `/stop_desktop` / `/restart_desktop` — the latter two
  ask for a confirm tap when `security.confirm_destructive` is on.
- `/project open <path>` — sets a working directory and switches the next
  `/ask` to the `project_task` action type (routes to `cli` by default).
- Send a file — saved to `data/inbox/`.

## Controlling this app over MCP

The messaging bot drives Claude Desktop (via the `ui` backend). The other
direction now exists too: `bot/mcp_server.py` is a stdio MCP server that
exposes the same control surface the dashboard GUI has — `get_status`,
`list_jobs`, `get_config`, `set_backend`, `reload_config`,
`list_mcp_servers`, `enable_mcp_server`/`disable_mcp_server`,
`start_claude_desktop`/`stop_claude_desktop`/`restart_claude_desktop`, and
`get_setup_status` — as MCP tools. It's a thin client: every tool call is
just an HTTP request to the already-running dashboard API using the same
`DASHBOARD_TOKEN`, so there's exactly one place (`bot/dashboard/server.py`)
that actually implements any of this.

Fastest way to wire it up: Control Center -> Environment ->
**Register with Claude Desktop**, or `POST /api/mcp/self-register`. That
writes a `bot-server` entry into `claude_desktop_config.json`
pointing at this project's own venv python, so it shows up (and can be
toggled) right alongside every other MCP server in the **MCP servers**
card. It's idempotent — safe to click again after a token rotation or a
venv rebuild, since it always overwrites that one entry with current
values. For Claude Code instead:
```powershell
claude mcp add bot-server -- .\.venv\Scripts\python.exe -m bot.mcp_server
```
Either way the dashboard API itself has to actually be running first
(launch the desktop app, or `python -m bot.main`) — the MCP server has no
logic of its own to fall back on if it can't reach `127.0.0.1:8787`.

## How routing works

`config/backends.yaml` resolves, per message: an explicit `--backend=`
flag, then the message's action type's entry in `action_overrides`, then
`default_backend`. If the chosen backend raises, the router retries once
against that entry's `backup` list before giving up — every attempt is
logged as a job row, visible in the dashboard's Jobs tab.

The file is watched and hot-reloaded (`watchfiles`) — edit and save, and
the change is live within about a second, recorded in `config_history`
with a diff summary, no restart. The dashboard's Control Center writes to
the same file atomically (temp file + rename) rather than editing it
in place.

### Backends are independently optional

None of `api`/`cli`/`ui` is mandatory — set up only the ones you actually
route to. Whether `ANTHROPIC_API_KEY` counts as "required" in the setup
wizard is computed from `config/backends.yaml` itself: it's required only
if `default_backend`, some `action_overrides[*].backend`, or a `backup`
entry actually names `api`. Point everything at `cli` and never touch
`ANTHROPIC_API_KEY` and the wizard stops asking for it.

Separately from routing, each backend has its own runtime readiness check
(`bot/setup_wizard.py`'s `backend_readiness()`) — `api` needs a valid key,
`cli` needs a `claude` binary, `ui` needs Claude Desktop findable. The
wizard's **Available backends** panel shows all three's status regardless
of which are routed to.

`cli` auto-detects two ways: a real PATH install first
(`shutil.which`), then Claude Desktop's own bundled copy at
`%APPDATA%\Claude\claude-code\<version>\claude.exe` — Desktop ships one
for its own local-agent-mode use without ever putting it on PATH, so this
fallback (`desktop.find_cli_path()`) is usually why `cli` just works with
zero setup on a machine that already has Desktop installed. If neither
resolves, an **Install/update CLI** button next to that row in the
wizard runs `npm install -g @anthropic-ai/claude-code` (needs npm on
PATH) via `/api/setup/install-cli`.

The router checks the backend
it's about to call *before* attempting it — a message routed to a backend
that isn't ready fails immediately with what's missing and where to fix
it ("open the setup wizard"), rather than a raw exception three layers
down, and still falls through to that action's `backup` chain exactly
like a runtime failure would. `/status` in Telegram lists all three
backends' readiness too.

## Notes on the `ui` backend

There is no official automation API for Claude Desktop. `ui_backend.py`
drives the window with `pywinauto`'s UI Automation backend — it is the
only way to read or continue a conversation already open in Desktop, and
it is also the most likely piece to need tuning: if your installed Claude
Desktop version exposes different control names, set
`backends.ui.input_automation_id` / `send_button_automation_id` in
`config/backends.yaml` rather than editing the code. The router never
routes to `ui` as a silent default — only via an explicit `--backend=ui`
flag or an explicit `action_overrides` entry — by design.

## Security

- Every Telegram update is checked against `ALLOWED_TELEGRAM_USER_IDS`
  (plus anyone added via the dashboard's Security card); Discord and Slack
  have their own equivalent allowlists (`DISCORD_ALLOWED_USER_IDS`,
  `SLACK_ALLOWED_USER_IDS`, set from the Platforms tab). Anything from an
  unlisted user is dropped and logged to `audit_log`, never answered.
- The dashboard has no login of its own — its security boundary is
  binding to `127.0.0.1` (see `DASHBOARD_HOST` in `.env`) plus the
  `DASHBOARD_TOKEN` header required on every state-changing request. Don't
  expose it past localhost without a real reverse proxy and auth in front.
- The `cli` backend defaults to `allowed_tools: []` — chat-originated
  prompts get no file/shell access unless you widen that per action type.
