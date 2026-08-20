# Bot Server

Run any number of independent bots at once — a Claude bot and a separate
Hermes Agent bot, each on Telegram, Discord, and/or Slack, all
simultaneously, each with its own fully separate chat history and job
queue. Every bot routes through one of five interchangeable backends
(Anthropic API, Claude Code CLI, best-effort UI automation of Claude
Desktop, or Hermes Agent via its CLI or its JSON-RPC gateway), and any
group of bots can be wired together into a **swarm** — a named group that
collaborates on one prompt via a pluggable strategy (parallel fan-out with
a synthesizer, a leader picking the best answer, a sequential
draft-then-refine pipeline, planner/worker task decomposition, or a fully
custom step graph). Everything — starting bots, watching them boot, and
controlling them afterward — happens inside one desktop app; there's no
separate script-then-browser step.

Companion design docs (published earlier in this project's chat):
architecture spec and dashboard mockup. This repo is the working
implementation of both, plus the desktop shell.

## What's here

```
bot/                   the Python server (multi-bot engine + dashboard API)
  main.py                entrypoint — boots every enabled bot instance + dashboard together
  bot_instances.py         DB-backed CRUD for bot instances (name/platform/backend/credentials/
                            allowed_user_ids), with JSON-snapshot backups mirroring envfile.py
  platform_supervisor.py   owns the instance_id -> asyncio.Task map; start/stop/restart per bot
  validators.py             shared token/ID format validators (setup_wizard.py + bot_instances.py)
  config.py               hot-reloadable config/backends.yaml loader
  db.py                    SQLite (WAL) storage: jobs, telemetry, audit, config history, messages,
                            bot_instances, swarms, swarm_runs
  auth.py                 legacy Telegram-only allowlist (superseded by per-instance allowed_user_ids)
  outbox.py                registry, keyed by bot instance id, that lets the dashboard's Chat tab
                            send through any connected bot
  envfile.py               resolves which .env to load core secrets from
  setup_wizard.py           validates + writes .env's core fields; legacy per-platform fields
                            (superseded by the Bots tab, kept read/write for transparency)
  mcp_server.py              stdio MCP server exposing the dashboard API as MCP tools
  router.py                backend resolution (per bot instance, falling back to global config)
                            + backup-chain retry
  desktop.py                Claude Desktop process control + MCP config management
  handlers.py                Telegram command handlers (instance-bound via application.bot_data)
  platforms/
    discord_platform.py     DiscordPlatformInstance — one per enabled Discord bot instance
    slack_platform.py        SlackPlatformInstance — one per enabled Slack bot instance (Socket Mode)
  backends/
    api_backend.py          Anthropic API
    cli_backend.py           Claude Code CLI (headless)
    ui_backend.py             pywinauto automation of the Desktop window
    hermes_cli_backend.py     Hermes Agent one-shot CLI (`hermes -z`), no persistent process
    hermes_gateway_backend.py Hermes Agent JSON-RPC/WebSocket gateway, session-based, async
  swarm/
    base.py                  SwarmStrategy interface + SwarmRunResult, mirrors backends/base.py
    engine.py                 dispatches a run to its strategy, owns the swarm_runs row lifecycle
    strategies.py              fanout_synthesize, leader_vote, sequential_relay,
                                decompose_delegate, custom
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
config/backends.yaml    global backend definitions + fallback routing — hot-reloaded on save
data/bot.db              created on first run
data/env_backups/        timestamped .env snapshots, one per save/restore — never pruned
data/bot_instances_backups/  timestamped JSON snapshots of the bot_instances table — never pruned
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
the other. Platform/bot credentials are separate: at least one enabled
**bot instance** (Bots tab) or, for the legacy single-bot-per-platform
fields, one configured **Platforms** entry has to exist before the wizard
reports "Ready" — see the Bots section below.

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

## Bots — running any number of independent bots

A **bot instance** is the unit of "one bot" — a name, a platform
(Telegram/Discord/Slack), a backend (which of the five below answers its
prompts), credentials, and an allowed-user list. There's no limit on how
many you run at once, on the same platform or different ones: a Claude bot
on Telegram, a separate Hermes bot on that same Telegram account's sibling
bot, a Claude bot on Discord, all simultaneously, each with completely
separate chat history and job queue (`jobs.instance_id` /
`messages.instance_id` tag every row).

Manage bots from the dashboard's **Bots** tab (always accessible, not just
during first-run setup) — add, edit, enable/disable, start/stop/restart
each one live without restarting the whole app, and delete (job/chat
history stays, tagged by the now-deleted id). Unlike the app's core
secrets (`.env`), bot credentials live in the SQLite `bot_instances` table
— arbitrary instance counts don't fit `.env`'s one-name-per-line shape —
with the same safety net: every create/update/delete snapshots the whole
table first into `data/bot_instances_backups/`, restorable from the same
tab, never auto-pruned.

Per-platform credential requirements (same regardless of how many
instances you create):

- **Telegram** — a bot token from [@BotFather](https://t.me/BotFather),
  plus numeric user ID(s) from [@userinfobot](https://t.me/userinfobot).
- **Discord** — a bot token + "Message Content Intent" enabled from the
  [Developer Portal](https://discord.com/developers/applications), the bot
  invited to a server you own, plus numeric Discord user ID(s).
- **Slack** — a Socket Mode app (no public webhook needed, so it works
  local-first): a bot token (`xoxb-...`) and an app-level token
  (`xapp-...`) from [api.slack.com/apps](https://api.slack.com/apps), plus
  Slack member ID(s).

The dashboard's **Chat** tab is bot-aware — pick a bot from its own
dropdown (not just a platform), see the real conversation for whichever
ones are connected, and send a message straight to the phone/laptop it's
logged in on, through `bot/outbox.py`'s registry keyed by instance id.

A legacy **Platforms** tab still exists, read/write, for the original
single-bot-per-platform `.env` fields this app started with
(`TELEGRAM_BOT_TOKEN` etc.) — nothing reads them at startup anymore
(a one-time migration copies whatever was there into a real bot instance
the first time you upgrade), kept only for transparency into what's still
sitting in your `.env` file.

## Backends

Every bot instance picks one backend as its own default (with optional
per-action overrides), independent of every other instance:

| Backend | What it is | Needs |
|---|---|---|
| `api` | Anthropic API, single-turn | `ANTHROPIC_API_KEY` |
| `cli` | Claude Code CLI, headless print mode | `claude` on PATH or bundled with Claude Desktop |
| `ui` | pywinauto automation of the Claude Desktop window | Claude Desktop installed |
| `hermes_cli` | Hermes Agent one-shot CLI (`hermes -z "<prompt>"`) | `hermes` on PATH |
| `hermes_gateway` | Hermes Agent's JSON-RPC/WebSocket gateway | `hermes` on PATH |

### Hermes Agent backends

Both talk to Hermes Agent if it's installed —
`hermes_cli` shells out per prompt (same shape as `cli`, no persistent
process, simplest to reason about); `hermes_gateway` spawns and owns a
dedicated `hermes serve --isolated` process on a fixed port
(`backends.hermes_gateway.port` in `config/backends.yaml`, default 8799),
connects over its WebSocket JSON-RPC API, and holds that connection for
the life of the app (torn down cleanly on shutdown). The gateway backend
is session-based per call but currently stateless across calls — no
conversation memory is threaded between prompts, matching how `api`/`cli`
already behave; per-instance conversation continuity is a documented
future upgrade, not built yet.

**Important:** Hermes Agent has its own built-in Telegram/Discord/Slack
adapters (`hermes gateway run`). Never configure the same platform bot
token in both Hermes's own gateway and a Bot Server instance — pick one
owner per token. Use Bot Server's bot instances as the sole platform
connection, and Hermes purely as a backend engine behind them.

Also worth knowing: `hermes serve --isolated` gives the gateway backend
its own dedicated web/API server process and port, but the underlying
agent runtime (session store, MCP connections, working directory) is
still Hermes's single machine-wide "gateway" — there's currently no flag
that fully sandboxes agent state per caller. A prompt sent through
`hermes_gateway` can see the same sessions/MCP servers as your own
interactive `hermes` usage.

## Swarms — multi-bot collaboration

A **swarm** is a named group of bot instances plus a strategy for how they
work together on one prompt. Manage swarms from the dashboard's **Swarms**
tab: create one, pick a strategy, reference member bot instances by id
(shown in a legend on the form), and run it with a prompt — each member's
individual answer still shows up as an ordinary row in the **Jobs** tab,
tagged with a shared `swarm_run_id` so you can see exactly what each bot
contributed. A run happens as a detached background task (it can take
minutes across several backend calls), so triggering one returns
immediately and the dashboard polls `GET /api/swarms/runs/{id}` for live
progress.

Built-in strategies (`bot/swarm/strategies.py`), each with a JSON `config`:

- **`fanout_synthesize`** — `{"members": [id, ...], "synthesizer": id|null}`.
  Every member answers independently and in parallel; if a synthesizer is
  set, it's fed all the answers and asked to produce one merged final
  answer, otherwise the answers are concatenated.
- **`leader_vote`** — `{"members": [...], "leader": id}`. Same parallel
  fan-out, but a designated leader always makes the final call — pick the
  best answer or synthesize a better one.
- **`sequential_relay`** — `{"members": [{"instance_id", "instruction"?}, ...]}`
  (ordered). A pipeline: each member's output becomes the next member's
  input, with an optional per-step instruction (draft → critique → refine).
- **`decompose_delegate`** — `{"planner": id, "members": [...], "aggregator": id}`.
  The planner is asked to break the prompt into subtasks (as JSON), each
  subtask round-robins to a member, and the aggregator merges the results.
- **`custom`** — `{"steps": [{"id", "instance_id", "depends_on": [...], "role"?}, ...]}`.
  A hand-built step graph: steps with satisfied dependencies run
  concurrently, a step's prompt includes every dependency's labeled
  output. Cycles are rejected before a run starts.

Every backend failure within a swarm is caught per-step (one member
failing doesn't necessarily fail the whole run — `fanout_synthesize` and
`leader_vote` tolerate partial failure as long as at least one member
succeeds); the run's final `status` is `success`, `partial`, `failed`, or
`cancelled`, visible in the run-history table alongside every past run's
full step breakdown.

## Using the bot

Plain text messages on any platform are routed through `/ask`. The slash
commands below are Telegram-specific (`bot/handlers.py`); Discord and Slack
currently only handle plain-text prompts.

- `/ask <text> [--backend=api|cli|ui|hermes_cli|hermes_gateway]` — send a
  prompt. Plain text messages (no leading `/`) are treated as `/ask` too.
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

Per message, in order: an explicit `--backend=` flag; then (if the message
came through a bot instance) that instance's own `action_overrides` for
the message's action type; then that instance's own default `backend`;
then falling back to `config/backends.yaml`'s global `action_overrides`
and `default_backend` for anything an instance didn't specify. If the
chosen backend raises, the router retries once against that entry's
`backup` list before giving up — every attempt is logged as a job row,
tagged with the bot instance that sent it, visible in the dashboard's Jobs
tab. Backend *definitions* (model, binary path, timeouts) stay global in
`config/backends.yaml` regardless of instance — only the routing *choice*
is per-instance, so two bot instances both on `cli` share one `CliBackend`.

The file is watched and hot-reloaded (`watchfiles`) — edit and save, and
the change is live within about a second, recorded in `config_history`
with a diff summary, no restart. The dashboard's Control Center writes to
the same file atomically (temp file + rename) rather than editing it
in place.

### Backends are independently optional

None of the five backends is mandatory — set up only the ones you
actually route to. Whether `ANTHROPIC_API_KEY` counts as "required" in the
setup wizard is computed from every active routing source: the global
`config/backends.yaml` chain *and* every enabled bot instance's own
`backend`/`action_overrides` (`setup_wizard.active_backends()`). Point
every bot instance at `cli` or `hermes_cli` and never touch
`ANTHROPIC_API_KEY` and the wizard stops asking for it.

Separately from routing, each backend has its own runtime readiness check
(`bot/setup_wizard.py`'s `backend_readiness()`) — `api` needs a valid key,
`cli`/`hermes_cli`/`hermes_gateway` need their respective binary
findable, `ui` needs Claude Desktop findable. The wizard's **Available
backends** panel shows every backend's status regardless of which are
routed to.

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
like a runtime failure would. `/status` in Telegram lists all five
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

- Every message is checked against its own bot instance's
  `allowed_user_ids` (set from the Bots tab, stored in `bot_instances`).
  Anything from an unlisted user is dropped and logged to `audit_log`,
  never answered. The legacy env-var allowlists (`ALLOWED_TELEGRAM_USER_IDS`
  etc.) and the dashboard's old Security card are no longer read at
  startup — folded into the migrated instance's `allowed_user_ids` once,
  on first upgrade.
- The dashboard has no login of its own — its security boundary is
  binding to `127.0.0.1` (see `DASHBOARD_HOST` in `.env`) plus the
  `DASHBOARD_TOKEN` header required on every state-changing request. Don't
  expose it past localhost without a real reverse proxy and auth in front.
- Bot instance credentials (platform tokens) live in `data/bot.db` in
  plain text, same trust model as `.env`'s plaintext secrets — both are
  local-only and gitignored, not a regression, just a second place on
  disk holding secrets now instead of one. `data/bot_instances_backups/`
  is gitignored for the same reason — never commit it.
- The `cli` backend defaults to `allowed_tools: []` — chat-originated
  prompts get no file/shell access unless you widen that per action type.
