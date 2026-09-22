# The terminal TUI and the CLI

Two terminal-native ways to manage AgenticBotPlatform, alongside the web dashboard and the desktop
app: `python -m bot.tui` (a Textual full-screen TUI) and `python -m abp_cli <command>` (a scriptable
command-line client, for scripts, CI and quick one-liners). Both talk to the same dashboard REST API
the web GUI and the desktop app use, through one shared client, `bot/dashboard_client.py`'s
`DashboardClient` — neither imports `bot.*` business-logic modules directly, so both work against a
remote/federated AgenticBotPlatform exactly like the desktop app already does, not just a local one.

This is an honest ledger, like `docs/agents/ROADMAP.md`'s own tables: what's covered today, and
what's still dashboard/desktop-app-only.

## What's covered

- **Bots**: full CRUD (list, show, create, edit, delete), lifecycle (start, stop, restart, enable,
  disable), and schedules (the TUI's `BotDetailScreen`; the CLI doesn't have a `schedules`
  subcommand yet — `DashboardClient` already has the methods, `abp_cli`'s argument parser doesn't
  expose them, see below).
- **Chat**: a real turn to any bot instance, through `POST /api/chat/send-to-bot` — the same route
  the Android app uses, and the only way to reach an `app`-platform bot at all (see
  `channels-and-devices.md`). TUI: `ChatScreen` (press `c` on a selected bot). CLI: `abp_cli chat
  <instance-id> <text...>`.
- **Agent settings** (a bot's own permission mode, sub-agent limits, worker/fallback model, plan
  approval): TUI's `AgentSettingsScreen` (opened with a bot selected shows that bot's own section
  above the global schema; opened with none selected shows only the global defaults). CLI:
  `abp_cli agent-settings get|set`.
- **Agent config** (the ~65 `native_agent.*` settings the ABP Agents page edits — sandbox backend,
  permissions, web/browser, routing, limits, and everything else `bot/agent_runtime/settings_schema.py`
  describes): **schema-driven**, not hand-ported field by field — both surfaces fetch
  `GET /api/agent/config/schema` and build their form from it, so a setting added to the schema
  later shows up here automatically. TUI: `AgentSettingsScreen`, one long scrollable form grouped by
  tab (not the GUI's separate tab pages — a real, disclosed scope difference). CLI: `abp_cli
  agent-config schema|get|set`.
- **Providers** (`config/providers.yaml`): list, add, remove, the deleted-providers store with
  restore, and browsing one provider's models (with which are marked free). TUI: `ProvidersScreen`
  (press `p` from the bot list). CLI: `abp_cli providers list|add|remove|catalog|models|toggle|restore`.

## What's still dashboard/desktop-app-only

Everything else `bot/dashboard/server.py`'s ~245 routes cover: **swarms** (define, run, spending
limits), **sessions** (list/export), the **terminal panel**, **hooks**, **plugins**, **skills**
(review/install/quarantine), **MCP** (internal and external server management), **security &
allowed-users**, **snapshots/env/config/diagnostics**, **peers/federation**, **kanban**, Android
push/mobile-key pairing, and the Models page's own usage/limit screens beyond plain provider/model
browsing. None of this has TUI screens or CLI subcommands yet — a later pass, not attempted half-way
here.

## Extending either one

Add a method to `bot/dashboard_client.py`'s `DashboardClient` (a thin one-to-one wrapper on a
dashboard route, matching the ~30 already there) and it's usable from both surfaces immediately.
From there:

- **TUI**: a new `Screen` subclass in `bot/tui/screens/`, reachable from `BotListScreen`'s toolbar
  (or another screen's) the same way `chat.py`/`providers.py`/`agent_settings.py` are — see those
  for the established compose/on_mount/refresh pattern. `agent_settings.py` in particular is worth
  reading first: it's schema-driven rather than hand-built, and that pattern is the right one to
  reuse for anything else that already has a dashboard schema/route pair (a settings-table shape),
  rather than porting fields by hand again.
- **CLI**: a new subcommand in `abp_cli/__main__.py`'s `_parser()`/`_dispatch()` — each existing
  command group (`bots`, `providers`, ...) is a small, self-contained block to copy the shape of.

## Verification

`tests/test_tui.py` (Textual's own `App.run_test()` harness, against a real in-process dashboard app
over `httpx.ASGITransport` — real request/response handling, not a mock) and `tests/test_abp_cli.py`
(the same real-app pattern, driving `abp_cli`'s argument parser and dispatch directly).
