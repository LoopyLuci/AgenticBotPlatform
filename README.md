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

Beyond routing prompts to bots, the app also ships a **Support Bot** — a
small, fully local, dependency-free AI model built into the desktop app
that understands plain-English server-management requests ("restart
Claude Desktop", "what's my default backend") and every slash command,
so you can manage the whole server from one chat panel without touching
config files. There's also a companion **Android app** with feature
parity for chat, bot management, and the Support Bot from your phone.

## Dashboard tour

The desktop app's sidebar has one tab per system. Everything below is
covered in depth further down this README (or in its own linked doc) —
this is just the map:

| Tab | What it's for |
|---|---|
| **Overview** | KPIs at a glance: job counts, desktop process state, DB size, config version, default backend. |
| **Jobs** | Every `/ask` attempt, filterable by status, with a completed/failed timeseries and per-backend breakdown charts. |
| **Connections & Telemetry** | Live desktop/backend/MCP status, per-backend latency, recent errors, connection event log. |
| **Database** | Storage size and per-table row counts, plus a **Vacuum** button (`POST /api/database/vacuum`) for reclaiming space. Control Center's **Data retention** card controls automatic daily pruning of old jobs/telemetry/connection-log/classification rows — see below. |
| **Control Center** | The router config editor (backends, action overrides, models, timeouts), agent-control mode, feature toggles, security settings, desktop process controls, the **Environment** card (`.env` editor + backups + setup wizard + MCP self-register), and the **MCP servers** card. |
| **Resilience** | Health checks, hot-reload status, and the full `config_history` change timeline with diffs. |
| **Live Logs** | Streaming tail of `logs/bot.log`, filterable by level. |
| **Chat** | A Telegram-style conversation view across every connected bot instance — send text, send files, see attachments/thumbnails inline. A mode toggle switches between Send from Server (real outbound, as the bot) and Chat with Bot (a real message to the bot, replied to for real) — see "Send from Server vs. Chat with Bot" below. |
| **Support Bot** | Plain-English or slash-command server management — see its own section below. |
| **Support Bot** | Chat with the local hybrid (TF-IDF + neural network) management assistant — plain English or slash commands, same engine the Android app's Support tab uses. See "Support Bot" below. |
| **Sessions** | Browse *past* conversations (grouped by 30-minute gaps, or the "legacy" pre-sessions bucket) — a history view, not to be confused with **session linking** (below), which is about which live chat a bot writes into. |
| **Bots** | Add/edit/enable/disable/start/stop/restart bot instances; per-instance backups; the **New Session** button for `ui`/`hermes_gateway` bots. |
| **Swarms** | Create/edit/enable/disable swarms; trigger runs; browse run history with per-step results. |
| **Training** | Add phrasings to teach the Support Bot's hybrid classifier (retrains both sub-models live), view its self-monitoring "Model health" panel, and give any bot instance persistent custom instructions/persona. |
| **Platforms** | Legacy single-bot-per-platform `.env` fields — superseded by the Bots tab, kept for transparency. |
| **Mobile** | Mobile pairing keys + QR codes, paired-device list with live online/offline status, and the Android one-click build/install/pair panel. |
| **Linked Servers** | Link another BotServer install (a laptop, a home PC, a VPS) to see and manage its bots from here — see "Linking servers" below. |

## Documentation

This README covers setup and every core feature end to end. Two topics
get their own deep-dive doc:

- **[docs/support-bot.md](docs/support-bot.md)** — how the local Support
  Bot model works, its intents, the confirm-before-destructive-action
  flow, and how to extend it.
- **[docs/connecting-claude-and-hermes.md](docs/connecting-claude-and-hermes.md)**
  — complete setup for Claude Desktop and Hermes Agent (CLI, gateway, and
  the separate Hermes Desktop app), including the one real gotcha (shared
  platform tokens between Hermes's own gateway and a Bot Server instance).
- **[docs/sessions.md](docs/sessions.md)** — how Bot Server links each
  bot instance to one specific real chat/session inside Claude Desktop or
  Hermes, so messages never land in the wrong (or an unlinked) window.
- **[docs/mobile-access.md](docs/mobile-access.md)** — pairing the Android
  app over Tailscale, push notifications, one-click build/install/pair.
- **[android-app/README.md](android-app/README.md)** — the Android app's
  own architecture and build instructions.

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
  agent_control.py         cross-bot ask_instance/run_swarm allowlist (trust_all vs allowlist mode)
  outbox.py                registry, keyed by bot instance id, that lets the dashboard's Chat tab
                            send through any connected bot
  attachments.py            safe on-disk attachment storage (UUID-prefixed names) + chunked uploads
  thumbnails.py             best-effort JPEG thumbnails for image attachments
  push.py                  Firebase Cloud Messaging push notifications to the Android app (optional)
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
    ui_backend.py             pywinauto automation of the Desktop window, session-linked (see docs/sessions.md)
    hermes_cli_backend.py     Hermes Agent one-shot CLI (`hermes -z`), no persistent process
    hermes_gateway_backend.py Hermes Agent JSON-RPC/WebSocket gateway, session-linked (see docs/sessions.md)
  support_bot/             local, dependency-free Support Bot — see docs/support-bot.md
    training_data.py          hand-authored (phrase, intent) examples, one set per management action
    model.py                   pure-Python TF-IDF + nearest-centroid intent classifier
    slots.py                    fuzzy argument extraction (bot/MCP-server/backend/model names)
    actions.py                   one handler per intent, thin wrappers over existing bot/* functions
    engine.py                    SupportBot.handle()/confirm() — classify, confirm-gate, execute
  commands.py              platform-agnostic slash commands, shared by Telegram/Discord/Slack/Support Bot
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

## Installer — one command, any machine

The fastest path on a fresh machine is the installer, which is aware of
*this specific machine's* hardware and software rather than assuming a
fixed checklist: it detects the OS, CPU architecture, and (on Linux) the
distro family and its package manager, then installs exactly what that
combination needs — Python itself if missing, Rust + Cargo, the Tauri CLI,
and (Linux only) the native WebKitGTK/GTK3/AppIndicator/librsvg/OpenSSL
dev packages Tauri's window needs to build — before creating the venv,
installing `requirements.txt`, and walking the same setup wizard described
below. It then offers to run a production `cargo tauri build` and to
register autostart at login.

```powershell
# Windows
.\scripts\install.ps1
```
```bash
# Linux / macOS
./scripts/install.sh
```

Both are thin bootstraps whose only job is guaranteeing a real Python
3.11+ exists (installing one via winget/Homebrew/apt/dnf/pacman/zypper/apk
if it doesn't) before handing off to `scripts/install.py`, which does the
actual detection and installation work and is safe to re-run any time —
every step checks what's already present/valid before changing anything,
same re-run safety as `scripts/setup.py`. Useful flags (pass to either
bootstrap, or straight to `python scripts/install.py`):

- `--check` (`-Check` on Windows) — report what's missing and exit, no changes
- `--yes` / `-y` (`-Yes`) — don't prompt for confirmation (unattended/CI use)
- `--no-system-deps` (`-NoSystemDeps`) — skip Rust/Tauri CLI/Linux native libs,
  assume they're already present
- `--no-build` (`-NoBuild`) — skip the offer to build the production app
- `--no-autostart` (`-NoAutostart`) — skip the offer to register login autostart
- `--dev` (`-Dev`) — dev-only, never offers a production build

On NixOS the installer detects it and skips the system-package step
entirely, pointing you at `nix develop` (see the NixOS section below)
instead — installing packages outside the Nix store wouldn't do anything
useful there. Login autostart is registered via Windows Task Scheduler,
launchd (macOS, `scripts/install_service_macos.sh`), or `systemd --user`
(Linux, `scripts/install_service.sh`) — the same mechanisms documented
under "Using the desktop app" below, just wired up automatically.

If you'd rather do each step yourself (or the installer can't fully
provision your setup), the manual steps it automates are documented next.

## Headless server deployment — Docker is optional, never required

Everything below runs just the bot/dashboard with no desktop at all — the
**`api` backend** (raw Claude API calls) plus the Telegram/Discord/Slack
adapters and the dashboard's REST API. (The `cli` and `ui` backends need a
real, locally-installed Claude Code CLI or Claude Desktop, so they're
Windows/macOS/Linux-desktop-only regardless of which path below you use.)
Docker is one convenient way to run this, not a dependency the app has —
if Docker is down, missing, or you'd rather not use it, the **bare metal**
path two sections down does exactly the same thing with the same
`.env`/`data`/`config` layout, using nothing but Python and the scripts
this repo already ships.

### Docker

```bash
cp .env.example .env
# edit .env: at minimum ANTHROPIC_API_KEY, DASHBOARD_TOKEN, and either
# TELEGRAM_BOT_TOKEN+ALLOWED_TELEGRAM_USER_IDS or a platform you'll add
# from the dashboard afterward (see "Bots" below)
docker compose up -d --build
```

`DASHBOARD_HOST` is already set to `0.0.0.0` inside the image — the
container boundary is what keeps this off the public internet, so put a
real reverse proxy in front of it (same as any other DASHBOARD_TOKEN
deployment) if you expose the port beyond your own network. `data/` and
`config/` are named volumes so upgrades (`docker compose up -d --build`
again) don't lose bot instances, sessions, or config edits made from the
dashboard; `.env` is bind-mounted from the host so secrets never end up
baked into the image.

### Bare metal (no Docker, no desktop app)

The exact same deployment without a container — same `.env`, same
`data/bot.db`, same dashboard, same real code path (`bot/main.py`), just
run directly by a plain Python interpreter instead of inside an image:

```bash
cp .env.example .env
# edit .env the same way as the Docker path above
python scripts/install.py --no-system-deps --no-build --yes
# creates the venv, installs requirements.txt, walks setup if needed —
# --no-system-deps skips Rust/Tauri/native GUI libs entirely, since a
# headless server never builds or runs the desktop shell
./scripts/run.sh        # Linux/macOS — or scripts\run.ps1 on Windows
```

For it to survive a reboot or an unhandled crash the way `docker compose`'s
`restart: unless-stopped` does, register it as a real OS service instead of
leaving it in a foreground terminal — same restart-on-failure guarantee,
different mechanism per OS, no elevation required for any of them:

```bash
./scripts/install_service.sh          # Linux — systemd --user, restarts on failure
./scripts/install_service_macos.sh    # macOS — a launchd LaunchAgent
```
```powershell
.\scripts\install_task.ps1            # Windows — a Task Scheduler task
```

(`scripts/install.py`'s own autostart offer runs one of these three for
you automatically, unless `--no-autostart` was passed.) On Linux, a
service registered this way only starts without an active login session
once you also run `sudo loginctl enable-linger $USER` — the script prints
this reminder itself.

A fresh install with zero bot instances configured refuses to even start
the dashboard, identically on both paths — either populate
`TELEGRAM_BOT_TOKEN`/`ALLOWED_TELEGRAM_USER_IDS` (or the Discord/Slack
equivalents — see `.env.example`) in `.env` before first boot so the
legacy-migration path creates the instance automatically, or start with
an already-populated `data/bot.db` from a prior install.

## Local CI/CD — no cloud runner involved

Every check (Python tests + `pip-audit`, Rust `fmt`/`clippy`/build, a
Docker image build) plus deployment runs entirely on your own machine via
`scripts/local_pipeline.py` — there is no GitHub Actions workflow or any
other cloud CI in this repo. Install the gate once:

```bash
./scripts/install_git_hooks.sh    # Linux/macOS/Git Bash
```
```powershell
.\scripts\install_git_hooks.ps1   # Windows PowerShell
```

From then on, every `git push` runs the full pipeline first (via a
`pre-push` hook) and only lets the push through if it's green — the same
trigger point (push to `main`) the retired GitHub Actions workflow used to
fire on, just local. Run it directly any time without pushing:

```bash
python scripts/local_pipeline.py             # checks + rebuild + redeploy
python scripts/local_pipeline.py --no-deploy # checks only
```

Tool-specific checks are best-effort: if `cargo` or `docker` aren't on
PATH (or the Docker daemon isn't running), that check is skipped with a
clear note rather than failing the whole pipeline — matching this
project's own "Docker is optional" stance above. Python checks always run.

The pipeline is also change-aware: it diffs the push against what's
already on `origin/main` and skips whatever a push doesn't touch — the
Rust check if nothing under `desktop-app/src-tauri/` changed, the Docker
build if no Docker-relevant file changed, and the entire stop/rebuild/
restart cycle if the push is docs/tests-only and has nothing new for the
running instance to pick up. A push whose scope can't be determined (or
a manual run with no `origin/main` to compare against) always falls back
to running everything, never to assuming nothing changed.

If a build of this app is already running when the pipeline starts (and
the push does touch deploy-relevant files), it's stopped first
(gracefully, then forcefully if needed) — Tauri's own build step re-copies
the bundled `.venv` into `target/release/` on every check, and that can
never succeed while a running instance still has its own copy of those
files loaded. On a fully green pipeline, the app is rebuilt and the same
instance is brought back — a registered OS service (see "Bare metal"
above) is restarted through its own service manager; otherwise the plain
executable that was running is relaunched directly.
If any check fails, whatever was running beforehand is restored
untouched, without deploying the broken build. Every run's full output is
also saved under `logs/local_pipeline/` for later inspection.

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
   (editable from Control Center -> Environment), then a hardcoded
   canonical install path (this maintainer's own machine — a Windows-only
   pin that has no effect anywhere else; on any other checkout, Windows or
   Linux, it's simply absent and this step is skipped), then this
   checkout's own root `.env` (`__file__`-relative, works identically on
   both platforms), then a global `~/.claude/.env` shared with other
   Claude tooling. Whichever is found first wins. A location change takes
   effect on the next restart.
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

### Linux

Every file needed to build and run Bot Server on Linux is already in this
repo — nothing above is Windows-only at the source level, just the
`.ps1` scripts, which have a `.sh` counterpart for every step.

1. **Prerequisites** — Python 3.11+, Rust + Cargo, the Tauri CLI
   (`cargo install tauri-cli --version "^2"`), and Tauri's own Linux
   system libraries (WebKitGTK for the window, GTK3, AppIndicator for the
   tray, etc.) via your distro's package manager:
   ```bash
   # Debian/Ubuntu
   sudo apt install libwebkit2gtk-4.1-dev libgtk-3-dev librsvg2-dev \
     libayatana-appindicator3-dev libssl-dev patchelf build-essential

   # Fedora
   sudo dnf install webkit2gtk4.1-devel gtk3-devel librsvg2-devel \
     libappindicator-gtk3-devel openssl-devel patchelf @development-tools

   # Arch
   sudo pacman -S webkit2gtk-4.1 gtk3 librsvg libappindicator-gtk3 \
     openssl patchelf base-devel
   ```
   (Package names/versions vary a little across Tauri releases — if
   `cargo tauri build` complains about a missing `.pc` file, search your
   distro's repos for that library name.)
2. **Set up the venv + config**, same three steps as `run.ps1`, just the
   `.sh` counterpart:
   ```bash
   ./scripts/run.sh
   ```
   Creates `.venv`, installs `requirements.txt` (`pywinauto`, the only
   Windows-only dependency, is skipped automatically via an environment
   marker), and runs the same interactive setup wizard as Windows. Ctrl+C
   out once it reports "Ready to run" and move to step 3.
3. **Development:**
   ```bash
   cd desktop-app/src-tauri
   cargo tauri dev
   ```
   **Production build** (produces a `bot-server` binary, with the venv
   and bot code bundled alongside it — build the venv with `./scripts/run.sh`
   *first*, since the bundle step packages whatever `.venv/` already
   exists; a Windows-built `.venv` isn't usable here and vice versa):
   ```bash
   cd desktop-app/src-tauri
   cargo tauri build
   ```
   `bundle.targets: "all"` in `tauri.conf.json` isn't Windows-specific —
   on Linux it produces the platform-appropriate packages automatically
   (`.deb`, `.rpm`, and an AppImage), landing in
   `desktop-app/src-tauri/target/release/bundle/`.

Everything else in this README — the Bots tab, backends, Support Bot,
swarms, slash commands, session linking — behaves identically on Linux;
the only backend that's inherently Windows/macOS-only is `ui` (it drives
a real Claude Desktop window via `pywinauto`, which itself only ships for
Windows), and it degrades to a clean error on Linux rather than crashing
anything else. Everything else (`api`, `cli`, `hermes_cli`,
`hermes_gateway`) works exactly the same.

#### NixOS

The `.deb`/`.rpm`/`apt`/`dnf`/`pacman` instructions above don't apply on
NixOS — there's no FHS and no system package manager in that sense.
`flake.nix` in the repo root provides a Nix-native path instead:

```bash
nix develop      # dev shell: Rust, cargo-tauri, Python 3.11, and every
                  # WebKitGTK/GTK3/AppIndicator/librsvg lib the Debian/Fedora
                  # sections above install manually — all pinned via nixpkgs
./scripts/run.sh # sets up .venv + config, same as any other Linux checkout
cd desktop-app/src-tauri && cargo tauri dev   # or `cargo tauri build`
```

`nix build` (using the flake's `packages.default`) is also available, but
it only produces the raw `bot-server` Rust binary — **not** the
self-contained Windows-style bundle with a `.venv` baked in. Bundling a
pip-installed venv into an immutable `/nix/store` path fights Nix's
model, so the Nix package expects to be run from a checkout with its own
`.venv` next to it (via `./scripts/run.sh`), the same as the "Development"
workflow above, not the standalone production bundle.

**A real, separate NixOS gotcha, not just a `cargo tauri` one:** several of
`requirements.txt`'s packages (`cryptography` in particular) ship
prebuilt `manylinux` wheels compiled against FHS paths (`/lib64/ld-linux...`)
that plain NixOS doesn't have, so a bare `pip install` inside
`./scripts/run.sh`'s venv can fail to import with a dynamic-linker error —
this is unrelated to and not fixed by the Tauri dev shell above. Two
common fixes, neither of which this repo automates yet: run the venv setup
under [`nix-ld`](https://github.com/nix-community/nix-ld) (patches the
dynamic linker search path system-wide), or wrap `./scripts/run.sh` in a
`pkgs.buildFHSEnv` shell. **This flake and these NixOS notes are
written but not build-verified on a real NixOS machine** — same caveat as
the rest of this Linux section (see "Known gaps" in the project's internal
notes); if you hit something not covered here, it's a genuine gap, not a
known-and-ignored issue.

#### Qubes OS

Qubes AppVMs run ordinary Fedora or Debian templates under the hood, so
the Fedora/Debian instructions above apply as-is inside the AppVM — there
is no Qubes-specific build step. A few things worth knowing about running
Bot Server *in* a Qubes AppVM specifically, though:

- **Networking is opt-in per VM.** Bot Server's dashboard binds to
  `127.0.0.1` only and never needs inbound connections from outside its
  own VM, so it works in a fully network-isolated AppVM for local-only use
  (`ui`/`api`/`cli` backends talking to a locally-installed model/CLI). Any
  bot instance that needs to reach a real chat platform (Telegram/Discord/
  Slack) or an external API (`hermes_gateway`, the `api` backend's
  Anthropic calls) needs that AppVM's NetVM configured normally, same as
  any other networked app in Qubes.
- **Pick one AppVM per trust boundary**, the same way you'd split any
  other app in Qubes — e.g. a bot instance handling an untrusted public
  Telegram bot in a different AppVM than one used for your own private
  Claude Desktop automation, rather than running every bot instance in one
  VM. Bot Server itself has no Qubes-awareness (no `qrexec` integration,
  no inter-VM policy) — this is a deployment-topology recommendation, not
  a built-in feature.
- **The MCP self-register flow** (`POST /api/mcp/self-register`, Control
  Center -> Environment) assumes Claude Desktop is reachable in the same
  VM it's registering into — if Claude Desktop runs in a different AppVM
  or in dom0 (unusual, and not a Qubes-recommended place to run GUI apps),
  self-register won't find it; use the manual MCP config steps instead.
- Nothing above is enforced or checked by the app — it's operational
  guidance for Qubes' compartmentalization model, not a code-level Qubes
  integration.

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
(Windows Task Scheduler; edit it to point at the built `.exe` instead of
`run.ps1` if you've switched over fully — see the script for the one line
to change). On Linux, the equivalent is a `systemd --user` service:
```bash
./scripts/install_service.sh
```
Same behavior (runs at login, restarts on failure) via the Linux-native
mechanism instead — see the script's own comments for what it registers.
On macOS, the equivalent is a launchd LaunchAgent:
```bash
./scripts/install_service_macos.sh
```
Registers `~/Library/LaunchAgents/com.botserver.app.plist` (no sudo
needed — a per-user agent); unregister with `launchctl unload` (the
script prints the exact command). All three are also offered
automatically at the end of `scripts/install.ps1` / `scripts/install.sh`.

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
tab, never auto-pruned. `ui`/`hermes_gateway` bots additionally show a
**New Session** button and their currently linked chat — see
[docs/sessions.md](docs/sessions.md).

The **Android app** card on the Mobile tab has one-click **Build,
install & pair**, **Build & install** (skip pairing), **Build only**,
**Install only**, and **Pair only** buttons for the Android project in
this repo — see [docs/mobile-access.md](docs/mobile-access.md).

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
Small files send in one request (`/api/chat/send-file`); larger ones use
a chunked-upload protocol (`/api/uploads/init` → repeated
`PUT .../chunk/{index}` → `/api/uploads/{id}/complete`) so a big attachment
doesn't have to fit in one HTTP body. Inbound image attachments get a
best-effort thumbnail generated automatically
(`/api/chat/attachments/{id}/thumbnail`); every attachment is stored under
a UUID-prefixed name (`bot/attachments.py`) so a client-supplied filename
can never be used to write outside the intended folder.

### Send from Server vs. Chat with Bot — operating both sides of the conversation

The Chat tab (dashboard and Android app both) has a **mode toggle** that's
always visibly on — the whole chat card's border, a banner line, and the
recipient controls all change with it, so which seat you're in is never
ambiguous. Both modes are **fully real** — nothing in either one is
simulated:

- **📤 Send from Server** (the default) — the dashboard/app operates *as
  the bot*, sending real outbound messages straight to the platform user
  picked in the dropdown, via `bot/outbox.py` and the live platform SDK
  connection (`POST /api/chat/send`). This is the original Chat tab
  feature; nothing about it changed except the name, clarifying what it
  actually does (push a message out from the server).
- **💬 Chat with Bot** — the dashboard/app itself becomes a genuine,
  first-class way to talk to a bot instance, exactly the way Telegram/
  Discord/Slack already are: what you type is a **real inbound message**
  (`POST /api/chat/send-to-bot`) through the exact same
  `CmdContext`/`dispatch_command`/`router.ask()` pipeline a real Telegram/
  Discord/Slack message goes through (see e.g. `discord_platform.py`'s
  `on_message`), and the bot's reply is a real reply, not a canned or
  simulated one. The sender's identity comes from the request's own
  auth — the dashboard token, or a specific paired mobile device's own API
  key — never a client-chosen value, so each device gets its own
  persistent conversation thread with the bot the same way each real
  Telegram user does. There's no recipient picker in this mode: you're
  just you. This path **never touches `outbox.py` or any platform
  SDK** — it's the Bot Server App's own real channel, logged with
  `platform="app"` rather than disguised as whichever platform the target
  instance happens to also use. File attachments aren't supported yet in
  this mode (the attach button disables itself).

Every bot instance any paired device can already see (through normal
pairing) can be chatted with this way — no separate per-instance
configuration needed, unlike Telegram/Discord/Slack's own credential
setup.

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
the life of the app (torn down cleanly on shutdown). For any bot instance
with a session link, `hermes_gateway` reuses that instance's persisted
`desktop_session_key` (Hermes's own real `session_id`) across calls, so
conversation memory is threaded between prompts — see
**[docs/sessions.md](docs/sessions.md)** for exactly how that link is
created and maintained. Only an instance-less ad-hoc call (nothing to
persist a link against) still gets a throwaway session per call, matching
how `api`/`cli` behave.

**Important:** Hermes Agent has its own built-in Telegram/Discord/Slack
adapters (`hermes gateway run`). Never configure the same platform bot
token in both Hermes's own gateway and a Bot Server instance — pick one
owner per token. Use Bot Server's bot instances as the sole platform
connection, and Hermes purely as a backend engine behind them. See
**[docs/connecting-claude-and-hermes.md](docs/connecting-claude-and-hermes.md)**
for the full setup walkthrough, including exactly how to check for and
fix this if you hit it.

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

## Agent-to-agent control

Beyond swarms, any bot instance (or Claude Desktop itself, via the MCP
tools above) can directly ask *another* bot instance a one-off question
and get its reply back — `POST /api/agent/ask` /
`mcp__bot-server__ask_instance` `{source_instance, target_instance,
prompt}` — without setting up a whole swarm for a single cross-bot query.
`source_instance` is self-declared (by name or id) rather than verified
against a live credential, so it isn't a security boundary on its own —
it drives the allowlist check and the audit-log entry.

Whether a given `source_instance` may target a given `target_instance` is
governed by `agent_control.mode` in `config/backends.yaml`:

- **`trust_all`** (default) — any instance may target any other.
- **`allowlist`** — an instance may only target the ids listed in its own
  `can_target` column (Bots tab, per-instance). A denied call returns a
  clear error, not a stack trace or a silent no-op.

The same allowlist gates `run_swarm`/`POST /api/swarms/{id}/run` when it's
called with a `source_instance` — every member the swarm references must
be a permitted target, checked before the run starts.

## Using the bot

Plain text messages on any platform are routed through `/ask`. Every slash
command below works identically on **Telegram, Discord, Slack, the
desktop app's Support Bot panel, and the Android app** — they all share
one implementation, `bot/commands.py`'s `dispatch_command()`, so there's
no "Telegram-only" command left. Telegram additionally gets a nicer
inline-keyboard confirm flow for destructive actions; every other
platform (and Discord/Slack, and Support Bot) uses the plain-text
"reply `confirm`" form shown below.

- `/ask <text> [--backend=api|cli|ui|hermes_cli|hermes_gateway]` — send a
  prompt. Plain text messages (no leading `/`) are treated as `/ask` too.
- `/status` — health/activity snapshot for this bot, including the real
  model actually in effect right now (not just "(backend default)") —
  resolved live wherever that's knowable; see "Notes on the `ui` backend"
  below for the one case it isn't.
- `/gateway` — backend readiness, scoped to this bot's own family (Claude:
  `api`/`cli`/`ui`, or Hermes: `hermes_cli`/`hermes_gateway`).
- `/backend show` / `/backend set <action|default> <api|cli|ui|hermes_cli|hermes_gateway>` —
  view or edit routing without touching the YAML.
- `/model show` / `/model set <api|hermes_cli|hermes_gateway> <model>` —
  view or change the model a backend uses.
- `/mcp list` / `/mcp enable <name>` / `/mcp disable <name>` / `/mcp logs <name>`
- `/start_desktop` / `/stop_desktop` / `/restart_desktop` — the latter two
  ask for a confirm tap when `security.confirm_destructive` is on.
- `/project open <path>` — sets a working directory and switches the next
  `/ask` to the `project_task` action type (routes to `cli` by default).
- `/new_session` — opens a brand-new, linked chat in the real Claude
  Desktop/Hermes app for this bot instance (`ui`/`hermes_gateway` only).
  See **[docs/sessions.md](docs/sessions.md)**.
- `/help` — lists all of the above.
- Send a file — saved to `data/inbox/`.

Typing `/` in any composer — desktop dashboard, Android app, the Support
Bot panel, or the Chat tab's outbound composer — pops up a Telegram-style
autocomplete list of every command above with its arguments and a
description, so you don't need to remember the exact syntax.

## Controlling this app over MCP

The messaging bot drives Claude Desktop (via the `ui` backend). The other
direction now exists too: `bot/mcp_server.py` is a stdio MCP server that
exposes the same control surface the dashboard GUI has, as MCP tools:
`get_status`, `list_jobs`, `get_config`, `set_backend`, `reload_config`,
`list_mcp_servers`, `enable_mcp_server`/`disable_mcp_server`,
`start_claude_desktop`/`stop_claude_desktop`/`restart_claude_desktop`,
`get_setup_status`, and two agent-to-agent tools — `ask_instance` (relay a
prompt to another registered bot instance and get its reply back) and
`run_swarm` (trigger a swarm run by name or id) — both subject to the
`agent_control` allowlist described below. It's a thin client: every tool
call is just an HTTP request to the already-running dashboard API using
the same `DASHBOARD_TOKEN`, so there's exactly one place
(`bot/dashboard/server.py`) that actually implements any of this.

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

## Support Bot — a local, hybrid AI assistant built into the desktop app

The dashboard has its own dedicated **Support Bot** panel — a chat window
where you can type plain English or slash commands to manage the entire
server, with no external AI service involved at all.

**It's a real hybrid: a dependency-free deterministic model plus a
genuinely trained neural network, running together.** Every message goes
through *both* `model.py`'s TF-IDF nearest-centroid classifier (stdlib
only — `math`/`re`/`collections`) and `nn_model.py`'s neural network (a
real backprop-trained multi-layer perceptron — one hidden ReLU layer,
softmax output, trained by plain gradient descent on cross-entropy loss,
implemented directly on top of `numpy`, the one numeric dependency this
feature needs) at once. `hybrid.py` combines them: if both agree, that's
the strongest possible signal; if they
disagree, it trusts whichever is more confident; if neither is confident,
it says so honestly ("not sure what you mean") instead of guessing.
`training_data.py` has ~190 hand-authored example phrasings across 40
management intents (restart the desktop app, show the default backend,
list MCP servers, enable/disable a bot, inspect jobs/swarms, run
diagnostics, manage backups and paired devices, check Claude Desktop/
Hermes Agent setup, and more) that both sub-models train on identically.

**Self-monitoring**: every classification — both sub-models' verdicts,
which one won, whether they agreed — is logged, and the dashboard's
**Training** tab shows it live as a "Model health" panel (agreement rate,
unknown rate, average confidence per model) computed from real traffic.

**What it can do:**
- Slash commands work exactly as documented above (typing `/` pops the
  same autocomplete menu) — routed straight through the shared
  `dispatch_command()`, no NLP involved.
- Plain English gets classified by the hybrid model into an intent, has
  its arguments extracted (`slots.py` — fuzzy-matches bot/swarm/MCP-
  server/device/backend/model names against what's actually configured
  right now, e.g. "restart the telegram bot" resolves to the real
  instance even with typos), and is executed by a thin handler in
  `actions.py` that calls the same functions the dashboard API itself
  calls — no separate business logic exists anywhere for "what the
  Support Bot can do" versus "what the dashboard can do".
- Destructive intents (deleting/disabling a bot, stopping/restarting
  Desktop, disabling an MCP server, vacuuming the database, restoring a
  backup, revoking a paired device) are confirm-gated exactly like
  `/stop_desktop` already is: the reply describes what it's about to do
  and shows a **Confirm** button instead of acting immediately, honoring
  the same `security.confirm_destructive` config flag. Confirm tokens
  live in memory only, expire after 5 minutes, and are never persisted.
- **Training tab**: add example phrasings for a misrecognized intent
  (retrains both sub-models immediately, no restart), or give any bot
  instance persistent custom instructions/persona — a system-prompt-style
  field prepended to every prompt that instance routes through
  `router.ask()`, independent of which backend it uses.

See **[docs/support-bot.md](docs/support-bot.md)** for the full intent
list, hybrid architecture, and how to extend it. The desktop panel and
the Android app's own **Support** tab both talk to the exact same
server-side engine (`POST /api/support-bot/ask` /
`/api/support-bot/confirm`) — there's no separate mobile implementation.

## Session linking — every bot talks into its own real chat

The `ui` and `hermes_gateway` backends each drive a *real* conversation in
a real desktop app (Claude Desktop's window, or a Hermes gateway session)
rather than a one-shot API call — which raises an obvious risk if you run
more than one bot instance against them: without tracking which chat
belongs to which bot, two instances could end up typing into the same
window, or a message could land in whatever chat happened to be open
rather than the one you meant.

Bot Server closes that gap by requiring every `ui`/`hermes_gateway` bot
instance to be **linked** to one specific, already-created chat/session
before it can send anything:

- The first message ever sent through such an instance automatically
  opens a fresh chat (clicks "New chat" in Claude Desktop, or calls
  Hermes's `session.create`) and links it — you don't have to do anything
  extra for this to work out of the box.
- From then on, every message re-selects that exact linked chat before
  typing — for Claude Desktop, by clicking the matching sidebar entry; if
  it can't find it (renamed or deleted from inside Desktop itself), the
  bot fails loudly rather than silently sending into the wrong window.
- Click **New Session** on a bot's row in the **Bots** tab (or run
  `/new_session` from that bot's own chat) any time you want to
  deliberately start a fresh conversation instead of continuing the
  linked one.

See **[docs/sessions.md](docs/sessions.md)** for the full mechanics,
config knobs for tuning Claude Desktop's UI automation, and
troubleshooting.

## Linking servers — manage several BotServer installs from one dashboard

If you run BotServer on more than one machine (a home PC and a laptop, say,
each with its own database and its own Telegram bot), the **Linked
Servers** tab lets either admin see and manage the other's bots from their
own dashboard — without merging databases, sharing one bot, or standing up
any new infrastructure.

**Two different tokens for two different jobs — your dashboard token never
leaves your machine, and the whole flow needs zero IP addresses typed by
anyone.** Linking uses a dedicated, short-lived **server pairing token**
for the one moment two servers actually talk to each other, and every
server auto-detects its own reachable address and bakes it into that
token — so nobody, on either side, ever has to look up or type an IP:

1. On server B's Linked Servers tab, step 1, click **Generate pairing
   token** — that's it, no fields to fill in. B auto-detects its own LAN
   address (the same "which interface has a route out" trick a browser or
   OS uses, not a guess from whatever URL the dashboard happens to be
   open at) and bakes it into a random code, valid for 10 minutes and
   usable exactly once — safe to paste into a chat message to whoever's
   linking in, unlike B's real dashboard token. (An "Advanced" disclosure
   holds a manual override for the one real edge case auto-detection
   can't cover — a reverse proxy or port forwarding.)
2. On server A's Linked Servers tab, step 2, fill in **Link a server**:
   just a name for B and that one pairing token — no address field at
   all. Click **Link server**. (Its own "Advanced" disclosure holds the
   one genuinely optional field, A's own reachable address — also
   auto-detected and pre-filled — for if you want B able to call A back
   too.)
3. That one call does the whole handshake: A decodes B's address straight
   out of the pairing token, mints a fresh, independently-revocable
   credential for B, and sends it to B along with the pairing token; B
   checks the pairing token (rejecting anything wrong, expired, or
   already used — including B's own real dashboard token, which was
   never a valid pairing token in the first place) and, only if it
   checks out, mints its own credential back for A. Both servers end up
   linked with their own working credential for the other — the pairing
   token is now spent and cannot be reused even if someone intercepted it.

From then on, either admin can see the other's bots (Manage bots) and
enable/disable/start/stop/restart them remotely, exactly as if browsing
that server's own Bots tab.

**Windows Firewall check, right where you generate the token.** Binding to
every interface (`DASHBOARD_HOST=0.0.0.0`) doesn't open the OS firewall —
Windows silently drops unsolicited inbound connections by default, which
shows up as a *timeout on the other machine*, not a clean error here,
making it genuinely hard to diagnose blind. The "Generate a pairing token"
card checks for an inbound rule on the dashboard's port and shows a clear
Firewall OK / Firewall blocking pill; if it's blocking, an **Open this
port** button adds the rule via a single Windows UAC prompt you approve —
no manual Firewall UI navigation required. Windows-only for now
(`bot/firewall.py`); hidden entirely on Linux/macOS.

**Built to stay working, not just to work once:**
- **Status is live, not stale.** A background check pings every linked
  server with a known address roughly once a minute and records whether it
  answered — Online/Unreachable in the table reflects real, current
  reachability, not just whatever the last manual click happened to see.
- **Re-linking is safe.** Running the link form again for the same address
  (after a restart, a database reset on one side, or just clicking twice)
  replaces the old credential in place instead of piling up duplicate,
  half-dead entries.
- **Transient network hiccups don't need a manual retry.** Read-only calls
  (status checks, fetching the other server's bot list) retry automatically
  through a couple of short backoffs before giving up — useful since two
  servers are often linked while one is still finishing booting, or talking
  over a home Wi-Fi network that occasionally drops a packet. Bot actions
  (start/stop/restart) deliberately do **not** auto-retry, since retrying a
  "restart" that actually landed the first time would restart it twice.

Unlink from either side any time — it revokes that side's credential
immediately, so the other side can no longer call back with it.

## Android app

A native Kotlin + Jetpack Compose companion app pairs with a running Bot
Server instance (over Tailscale — see
**[docs/mobile-access.md](docs/mobile-access.md)**) and mirrors the
desktop dashboard's core tabs from your phone: **Chat** (talk through any
connected bot, with the same `/` slash-command autocomplete as desktop),
**Bots** (add/edit/enable/disable/start/stop/restart, exactly like the
Bots tab), **Support** (the same local Support Bot, reachable from
anywhere your phone can reach the server), **Sessions**, and **Jobs**.
Pairing is a QR-code scan or a manual host/key entry; optional push
notifications alert you the moment a bot gets a new inbound message,
not just while the app happens to be open. See
**[android-app/README.md](android-app/README.md)** for build instructions
and architecture.

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
like a runtime failure would. `/gateway` in Telegram lists backend
readiness, scoped to whichever family (Claude or Hermes) the chat's own
bot is actually wired to — a Hermes-backed bot has no reason to see
Claude readiness info and vice versa.

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

It's also the one backend `/status`'s Model line can't resolve to an
actual model id: Claude Desktop's currently-selected model lives in its
own account-synced UI state, not any local file this process can read
(unlike `cli`, whose default this bot reads straight from Claude Code
CLI's own `~/.claude/settings.json`). `/status` says so plainly rather
than guessing.

## Data retention — keeping a long-running server light and fast

Left alone, `data/bot.db` only ever grows: every request appends a job
row, telemetry samples, connection-health events, and (if you use the
Support Bot) a classification log entry, and nothing ever removes one.
Control Center's **Data retention** card runs an automatic daily pass
that prunes rows older than a configurable window (`retention.days` in
`config/backends.yaml`, default 90) from exactly those four tables —
`jobs` (only finished ones; a queued/running/retrying job is never
touched regardless of age), `telemetry_events`, `connections_log`, and
`support_bot_classifications`. It deliberately never touches `audit_log`
(a security trail, kept by design), chat/session history, or
`config_history` — those have real long-term value at a fraction of the
row volume the pruned tables see. Turn it off entirely with the card's
toggle if you want to keep every row forever. Pruning only deletes rows;
run the Database tab's **Vacuum** afterward if you want to reclaim the
freed disk space immediately rather than waiting for SQLite's own
incremental reuse of that space.

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
