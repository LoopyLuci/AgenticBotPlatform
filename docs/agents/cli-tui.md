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

**Both CLI and TUI:**

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
- **Swarms**: create (any strategy — `fanout_synthesize`, `leader_vote`, `sequential_relay`,
  `decompose_delegate`, `custom`), list, enable/disable, run, watch recent runs, delete. TUI:
  `SwarmsScreen` (press `w`). CLI: `abp_cli swarms list|show|create|delete|enable|disable|run|runs|run-show|run-cancel`.
- **Sessions**: list (with search), view a conversation's messages, delete. TUI: `SessionsScreen`
  (press `s`). CLI: `abp_cli sessions list|show|delete|new`.
- **SSH Toolkit** ([github.com/LoopyLuci/SSH_Toolkit](https://github.com/LoopyLuci/SSH_Toolkit)) — a
  separately maintained PowerShell tool for managing/visualizing SSH connections between machines,
  vendored here as a git submodule (`vendor/ssh_toolkit`) and reached only through
  `bot/ssh_toolkit.py`, which shells out to its own CLI: nothing here reimplements its logic, so ABP
  and the standalone toolkit never drift apart — a bug fixed upstream is fixed here the moment the
  submodule is bumped. Covers add/list/show/remove/test/run a remote command, reachability for every
  connection, the proxy-jump graph, and update check/apply. Also in the dashboard GUI and desktop
  app (their own "SSH Toolkit" page). TUI: `SshToolkitScreen` (press `h`). CLI: `abp_cli ssh
  status|list|show|add|remove|test|run|status-all|visualize|check-update|update|auto-update`. Updates
  are otherwise manual-only by default; `abp_cli ssh auto-update never|notify|auto` (or the GUI's own
  select) sets a machine-wide background check (every 6h) that either just logs an available update
  ("notify") or applies it automatically ("auto") — never touches this machine's own `~/.ssh/config`
  or connection registry, only the vendored submodule's own files.

**CLI only, no TUI screen yet** (all real, all tested against the live dashboard app — just not
given a Textual screen in this phase):

- **Terminal**: `abp_cli terminal <text>` — runs one ABP slash command (`bot/commands.py`'s
  dispatcher, the same one every platform handler uses), **not a raw shell**.
- **Hooks**: `abp_cli hooks list|add|enable|disable|remove`.
- **Plugins**: `abp_cli plugins list|install|create|enable|disable|remove`.
- **Skills**: `abp_cli skills list|create|remove|packs|fetch|quarantine|approve-quarantine|reject-quarantine|drafts|approve-draft|reject-draft`.
- **MCP** (internal, i.e. Claude Desktop's own config, and external/remote servers):
  `abp_cli mcp list|logs|enable|disable|pins|approve-pin|external-list|external-add|external-enable|external-disable|external-remove`.
- **Security & devices**: `abp_cli security allowed-users|allow-user|disallow-user|permissions|instance-permissions|set-instance-permissions|devices|mobile-keys|create-mobile-key|revoke-mobile-key`.
- **Snapshots**: `abp_cli snapshots list|create|restore|remove`.
- **Env / config / diagnostics**: `abp_cli env`, `abp_cli config get|reload|set`, `abp_cli
  diagnostics summary|crash-reports`.
- **Peers/federation**: `abp_cli peers list|self-address|pairing-token|link|remove|overview|bots`.
  Verified live against two real, separate machines over Tailscale. `peers link` also sets up
  SSH between the two machines automatically by default (`--no-ssh-setup` to skip): each side
  generates its own ed25519 keypair (SSH Toolkit's `New-SshLinkKeypair`), exchanges public keys
  and OS usernames as part of the same handshake, trusts the other's key
  (`Install-SshLinkTrustedKey` — no SSH session, no password prompt, since the handshake itself
  is already the authenticated channel), and registers a working SSH Toolkit connection back to
  it (`Add-SshLinkConnection`) — no manual key generation, copying, or `authorized_keys` editing
  on either side. Best-effort: SSH Toolkit being unavailable (no PowerShell, submodule not
  checked out) never fails the underlying peer link itself, only skips the SSH half of it.
- **Kanban**: `abp_cli kanban boards|cards|add|move|remove`.
- **Tailscale / containers / VMs / infra rules**: `abp_cli tailscale|docker|vm|rules get|post|put|patch|delete
  <path> [--data JSON]` maps one-to-one onto `/api/tailscale/*`, `/api/docker/*`, `/api/vms/*` and
  `/api/infra/rules` (for example `abp_cli docker get containers`, `abp_cli docker post
  containers/web/action --data '{"action":"restart"}'`); it is a thin route passthrough, not hand-written
  subcommands. The TUI has a screen for each, opened from the bot list with `t` (Tailscale), `d` (Containers),
  `v` (VMs) and `o` (Infra automation): a table, a view picker (containers/images/volumes/networks/stacks/templates/
  system; peers/Serve & Funnel/preferences/devices), action buttons, an output pane and the same host picker for
  linked servers. Interactive terminals: on this machine the Containers screen's **Shell** suspends the TUI and runs
  `docker exec -it` in your own terminal; VM serial consoles and shells on linked servers are desktop-app/dashboard
  only, and the VMs screen instead has a QEMU monitor prompt. Verified live: Tailscale settings, QEMU and Hyper-V,
  and the TUI screens against the real API. The Docker daemon was not responding on the dev machine, so container
  operations are covered by tests with a faked `docker` only.

## What's still dashboard/desktop-app-only

The terminal panel, hooks, plugins, skills, MCP, security/devices, snapshots/env/config/diagnostics,
peers and kanban have no TUI screen yet (CLI-only, see above) — a later pass, not attempted
half-way here. Also still GUI-only: Android push/mobile-key QR pairing UI, and the Models page's
own usage/limit screens beyond plain provider/model browsing.

**SSH session monitor + recorder** (dashboard GUI only, no TUI/CLI surface yet) — the SSH Toolkit
page's "Session monitor" card: run a command over a registered connection and watch every action
happen live — each output line, a CPU/memory read from the remote machine every few seconds, and
its exit code, streamed the instant they occur over the dashboard's existing `/api/ws` socket
(`bot/ssh_session_monitor.py`, message type `ssh_session_event`) — structured events, never video or
screen-share. A Record/Pause/Resume/Stop control persists the exact event sequence to
`ssh_session_recordings`/`ssh_session_events` (`bot/db.py`) for exact playback later, with a
scrubber and speed control. `POST/GET /api/ssh-toolkit/session*` and `/api/ssh-toolkit/recordings*`;
`DashboardClient.ssh_session_*`/`ssh_recording*`. Verified live against a real remote machine over a
real SSH connection, not simulated.

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
