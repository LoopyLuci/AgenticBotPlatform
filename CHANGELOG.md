# Changelog

All notable changes to BotServer are documented here. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versioning
follows [Semantic Versioning](https://semver.org/) for the desktop
app's own version (the Android app versions independently — see its own
`versionName`/`versionCode` in `android-app/app/build.gradle.kts`).

## [Unreleased]

### Added
- A real `pytest` suite (`tests/`) and GitHub Actions CI — byte-compiles
  every source file, runs the test suite, audits dependencies, and
  checks the Rust side (format/clippy/compile) on every push.
- A per-instance circuit breaker: a bot instance whose backend fails 5
  times in a row now pauses for 5 minutes instead of retrying forever,
  with a "retry now" action in the dashboard.
- `/healthz` (unauthenticated liveness probe) and `/metrics`
  (Prometheus-format gauges/counters) for real deployment monitoring.
- This changelog, and an [architecture decision record log](docs/adr/).

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

[Unreleased]: https://github.com/LoopyLuci/BotServer/compare/v0.3.0...HEAD
[0.3.0]: https://github.com/LoopyLuci/BotServer/compare/v0.2.2...v0.3.0
[0.2.2]: https://github.com/LoopyLuci/BotServer/compare/v0.2.1...v0.2.2
[0.2.1]: https://github.com/LoopyLuci/BotServer/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/LoopyLuci/BotServer/compare/v0.1.1...v0.2.0
[0.1.1]: https://github.com/LoopyLuci/BotServer/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/LoopyLuci/BotServer/releases/tag/v0.1.0
