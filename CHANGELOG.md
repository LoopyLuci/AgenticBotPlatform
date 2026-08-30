# Changelog

All notable changes to BotServer are documented here. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versioning
follows [Semantic Versioning](https://semver.org/) for the desktop
app's own version (the Android app versions independently — see its own
`versionName`/`versionCode` in `android-app/app/build.gradle.kts`).

## [Unreleased]

## [0.4.0] — 2026-08-30

### Fixed
- `Router._invalidate()` (fired on every config hot-reload) dropped its
  cached backend dict with no shutdown call, silently leaking any
  backend holding a live external process — `HermesGatewayBackend`'s
  spawned `hermes serve`, in practice, on every `backends.yaml` edit.
  Now schedules a proper `shutdown()` on the old backend set (flagged
  during this session's hot-reload work, fixed as its own follow-up).
- **Critical**: `bot/main.py` crashed on startup (`SystemExit`) whenever
  zero bot instances were configured, and it did this *before* the
  dashboard/API server was even built — a fresh install could never
  reach the "Add a bot" UI needed to fix itself. BotServer now starts
  and the dashboard/desktop UI is fully usable with zero bots; adding
  the first one is just a normal Bots-tab action, not a precondition.
  The setup wizard's own "Ready" gate no longer requires a bot/platform
  to already exist either, for the same reason.
- `scripts/local_pipeline.py`'s deploy step could fail with a Windows
  "file in use" error even after correctly stopping `bot-server.exe`,
  because a separate `python -m bot.mcp_server` process (spawned by an
  MCP client from the same bundled `target/release/.venv` a Rust
  check/build needs to overwrite) could independently hold the same
  compiled extension modules memory-mapped. The pipeline now finds and
  stops any such process before a Rust check or deploy, the same way it
  already handles `bot-server.exe` itself.

### Added
- A Textual-based terminal UI (`bot/tui/`, launch via `scripts/tui.sh`/
  `scripts/tui.ps1` or `python -m bot.tui`): add/edit/delete bots across
  all 5 platforms, start/stop/restart/enable/disable, live per-field
  validation and setup help, and a schedules panel — talking to an
  already-running BotServer's dashboard HTTP API, so it manages a
  remote/federated install exactly like the desktop app does. A third
  way to manage bots alongside the browser dashboard and desktop app,
  for headless machines, SSH sessions, or terminal-first workflows.
- Completed the Add-a-bot form: inline help and step-by-step setup
  guidance for all 5 platforms (previously only Matrix/WhatsApp had
  any), live green/red field validation as you type (`GET
  /api/platform-guides`, `POST /api/validate-field`) instead of only
  failing after submit, `custom_instructions`/`enabled` settable at
  creation time, and an "Advanced" editor for per-instance
  `action_overrides` (accepted by the API since Matrix/WhatsApp shipped
  but previously had no UI anywhere).
- Scheduled commands (`bot/scheduler.py`, previously chat-only via
  `/cron`/`/loop`/`/heartbeat`) now have a dashboard API
  (`/api/bots/{id}/schedules`) and a "Schedules" card in the Bots tab,
  mirrored into the desktop app.
- Python code hot-reload (`bot/hotreload.py`): most edits to `bot/*.py`
  now apply to the already-running process instead of needing the full
  local-CI/CD stop/rebuild/relaunch cycle — business logic and backends
  apply on the very next call, Discord/Slack/Matrix code gets a brief
  automatic reconnect, and a documented set of core files (routing, the
  DB connection, the dashboard, Telegram's handler registration, and a
  few others confirmed to hold live singleton/subprocess/socket state)
  still require the existing full restart, reported as "restart
  required" rather than silently skipped or half-applied. A failed
  reload enters a degraded state that blocks further cycles until an
  actual restart, rather than risk compounding a broken module. New
  dashboard "Hot Reload" card (status, recent events, manual "reload
  now"), mirrored into the desktop app, plus `hot_reload_status`/
  `trigger_hot_reload` MCP tools. Toggle: `hot_reload_enabled` in
  `config/backends.yaml`. See `bot/hotreload.py`'s module docstring for
  the full reasoning; the classification is guarded by a test that
  parses every file's real imports so it can't silently rot as the
  codebase grows. Second half of an earlier request (the first half —
  config hot-reload hardening + snapshot/restore — shipped separately).
- WhatsApp Cloud API as a fifth chat platform
  (`bot/platforms/whatsapp_platform.py`) — architecturally different from
  the others: messages arrive via a webhook Meta calls
  (`POST /webhooks/whatsapp` on the dashboard's own FastAPI app, verified
  with a real X-Hub-Signature-256 HMAC check since that route can't
  require a dashboard token), not an outbound-connecting client. Full
  two-way messaging including media (images, documents, audio, video) via
  the Graph API's upload/download endpoints, and the same slash-command/
  allowlist/dashboard-Chat-tab integration every other platform gets.
  Requires a real Meta Business/WhatsApp Cloud API app and a public HTTPS
  URL — see the module's docstring. Phase D of the multi-provider/
  plugin/platforms roadmap — completes it.
- Matrix as a fourth chat platform (`bot/platforms/matrix_platform.py`,
  via matrix-nio): a bot instance can now be Telegram, Discord, Slack, or
  Matrix. Full messaging (text + incoming/outgoing images, files, audio,
  video), automatic room-invite acceptance, and the same slash-command/
  allowlist/dashboard-Chat-tab integration every other platform gets.
  Encrypted rooms aren't supported (no Olm/Megolm store) — use an
  unencrypted room. Phase C of the multi-provider/plugin/platforms
  roadmap.
- A plugin API: a single local `plugin.py` file can register new agent
  tools and/or slash commands (`bot/plugins.py`) without touching core
  code — they show up in every backend's tool list and in `/help`/
  `/commands`/Telegram's native menu exactly like built-in ones. Managed
  from a new dashboard "Plugins" card (install/enable/disable/remove),
  mirrored into the desktop app. Local-install only, deliberately not a
  networked marketplace — see
  [ADR-0007](docs/adr/0007-plugins-are-trusted-local-code.md) for the
  trust model (a plugin is trusted local code with full process
  privileges, the same boundary `run_shell` already accepts). Phase B of
  the multi-provider/plugin/platforms roadmap.
- Appearance settings (Control Center tab): theme (System/Light/Dark) and
  a whole-UI text/scale control (85%-150%), per-browser via localStorage,
  applied instantly with no server round trip or flash-of-wrong-theme on
  load. Mirrored across the dashboard and desktop app.
- A live-development safety net: `bot/config.py`'s hot-reload is now
  hardened against a config file that parses but has the wrong shape
  (rejected and logged, same as a syntax error, instead of getting
  swapped in to crash later); a new snapshot/restore system
  (`bot/snapshots.py`, a "Snapshots" dashboard card, and
  `create_snapshot`/`list_snapshots`/`restore_snapshot` MCP tools) takes
  a zero-downtime point-in-time copy of config + the database and can
  restore it later, so an agent (or you) editing this codebase can
  recover from a bad change without a full backup/rebuild.
- Multi-provider model routing: a new `custom_model` backend that talks
  to any OpenAI-compatible endpoint (a local Ollama/LM Studio/vLLM/
  llama.cpp server, OpenRouter, or real OpenAI) via a named provider
  registry (`config/providers.yaml`, managed from the dashboard's new
  "Model providers" card) — runs Bot Server's own shell/file/git tool
  loop against it, the same one the `api` backend already uses for
  Anthropic. `/gateway`, `/model`, and the dashboard's model picker all
  treat it as its own family. Phase A of a larger roadmap (plugin API,
  WhatsApp/Matrix platforms) — see `docs/adr/` and the project's plan log.
- A real `pytest` suite (`tests/`).
- A 100%-local CI/CD pipeline (`scripts/local_pipeline.py`) — byte-compiles
  every source file, runs the test suite, audits dependencies, checks the
  Rust side (format/clippy/compile), and builds the Docker image, all on
  your own machine, then rebuilds and redeploys the running instance on a
  green result. Installed as a `pre-push` git hook via
  `scripts/install_git_hooks.sh` / `.ps1`. Replaces an earlier GitHub
  Actions workflow, which this project no longer uses at all. Change-aware:
  a push that doesn't touch `desktop-app/`, Docker files, or `bot/`/`config/`
  skips the corresponding check (or the whole stop/rebuild/restart cycle)
  instead of always paying the full multi-minute cost.
- A per-instance circuit breaker: a bot instance whose backend fails 5
  times in a row now pauses for 5 minutes instead of retrying forever,
  with a "retry now" action in the dashboard.
- `/healthz` (unauthenticated liveness probe) and `/metrics`
  (Prometheus-format gauges/counters) for real deployment monitoring.
- Per-table data export (`/api/export/{table}`, JSON or CSV) from the
  dashboard's Database panel.
- A `/gateway` command (Telegram/Discord/Slack) showing backend readiness
  scoped to a bot's own model family (Claude or Hermes); `/status`'s
  Model line now resolves the real live model in effect instead of a
  generic placeholder.
- A Dockerfile/`docker-compose.yml` for headless server-only deployment,
  and a documented, equally-capable bare-metal path for machines without
  Docker (`scripts/run.sh`/`run.ps1` plus the existing
  `install_service*`/`install_task.ps1` autostart scripts).
- This changelog, and an [architecture decision record log](docs/adr/).

### Changed
- Removed the separate "Custom bot instructions" card now that
  `custom_instructions` is editable inline in the Add-a-bot/edit form;
  its one genuinely useful feature (inserting a persona's default
  instructions) moved into that form as an "Insert `<persona>` preset"
  button.

### Fixed
- `router.resolve_chain()`'s "ui never gets a silent default" guard was
  a no-op if `default_backend` was itself set to `"ui"` (a one-click
  dashboard option) — it re-read the same config value it was meant to
  override. Now hardcoded to fall back to `"api"`.
- `slots.find_bool()` false-positived on ordinary words containing
  "on"/"off"/"no" as a substring (e.g. "turn off notifications" matched
  "on" inside "notifications" and returned `True`). Now uses
  word-boundary matching.

## [0.3.0] — 2026-08-27

### Added
- A visual, hardware-aware GUI installer (`scripts/install_gui.py`) that
  shows live detection and install progress; the text installer remains
  as an automatic fallback for headless machines and scripted use.
- Real, live download progress in the in-app self-updater (previously a
  silent multi-minute wait).
- Opt-in automatic `VACUUM` for the data-retention pass.

### Changed
- Dashboard auto-refresh timers now pause while the tab/window isn't
  visible instead of polling continuously in the background.

### Fixed (post-release, same day)
- The installer crashed with an unhandled `FileNotFoundError` if it
  offered a production build without Rust/Tauri actually being
  available; it also auto-launched the fully-interactive setup wizard
  under an unattended run with no console attached, producing a
  confusing "Aborted." Both now fail/skip cleanly with a clear message.

## [0.2.2] — 2026-08-27

### Changed
- Replaced scikit-learn with a from-scratch NumPy implementation of the
  Support Bot's neural classifier — Windows installer 136MB → 72.7MB
  (MSI), 80MB → 42.6MB (NSIS).

### Added
- Automatic daily data retention: prunes old rows from jobs, telemetry,
  connection-log, and Support Bot classification tables.

### Fixed
- Two orphaned scheduled commands (pointing at long-deleted bot
  instances) had been firing every 5 seconds for days, always failing.
  Deleting an instance now cascades to its scheduled commands, and the
  scheduler auto-disables any schedule whose instance no longer exists.

## [0.2.1] — 2026-08-27

### Added
- Windows Firewall detection and one-click "Open this port" fix for the
  most common reason server-to-server linking silently failed.
- MCP server stability: a shared long-lived HTTP client, retry on
  transient connection failures, and rotating file logging.

## [0.2.0] — 2026-08-27

### Added
- Native Telegram command/menu system with a real agent-loop engine for
  the `api` backend: `/queue`, `/steer`, `/pause`, `/approve`/`/deny`,
  and git-backed checkpoints (`/rollback`, `/undo`, `/branch`,
  `/compress`, `/worktree`).
- Cross-backend `/model` picker grouped by provider, with live model
  lists.
- Cross-network WebRTC fallback and real TURN support for mesh APK
  transfers between devices that can't reach each other directly.
- Server-to-server linking (federation): link multiple BotServer
  installs and manage every one's bots from a single dashboard, using a
  short-lived, single-use, self-describing pairing token rather than
  ever pasting a real dashboard token into another server's UI.

## [0.1.1] — 2026-08-23

### Added
- Session linking: `ui`/`hermes_gateway` bot instances write into one
  real, persistent chat/session instead of a fresh one per call.
- One-click Android build/install/pair from the desktop app's Mobile tab.
- Agent-to-agent control: `ask_instance`/`run_swarm` MCP tools and an
  `agent_control` allowlist for cross-bot queries.
- Chat attachments with chunked uploads and inline thumbnails.
- Linux support (Debian/Fedora/Arch, NixOS flake, Qubes AppVM notes).

## [0.1.0] — 2026-08-21

First public release. One desktop app running any number of independent
Claude/Hermes Agent bots across Telegram, Discord, and Slack at once,
plus a native Android companion.

[Unreleased]: https://github.com/LoopyLuci/BotServer/compare/v0.4.0...HEAD
[0.4.0]: https://github.com/LoopyLuci/BotServer/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/LoopyLuci/BotServer/compare/v0.2.2...v0.3.0
[0.2.2]: https://github.com/LoopyLuci/BotServer/compare/v0.2.1...v0.2.2
[0.2.1]: https://github.com/LoopyLuci/BotServer/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/LoopyLuci/BotServer/compare/v0.1.1...v0.2.0
[0.1.1]: https://github.com/LoopyLuci/BotServer/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/LoopyLuci/BotServer/releases/tag/v0.1.0
