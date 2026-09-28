# Hermes Manager from ABP

[Hermes Manager](https://github.com/LoopyLuci/Hermes-Manager) is the desktop control center for Hermes. It covers the
gateway, logs, sessions, chat, config, updates, backups and tools: MCP servers, skills, cron, plugins and models.

It stays a **separate program**, with its own checkout, build, Electron window and releases, worked on in its own
repo. ABP installs it, keeps it up to date from that repo, runs it, and drives it through its API: the page
**Hermes Manager**, the agent's `hm_*` tools, and `/api/hermes-manager/*`.

## How the two connect

Hermes Manager's window sits on top of a local API, **the bridge**, which runs on Hermes's own Python. A running bridge
writes `~/.hermes-manager/control.json` (its URL, token and pid), and ABP reads it. When the bridge is not running, ABP
starts it headless. When the window opens later, it reuses that bridge instead of starting another, so ABP, the window
and Hermes Manager's MCP server all share one.

Hermes Manager's own documentation of this is `docs/control.md` in its repo.

## Where ABP finds it

The first of these that is set or exists:

1. `$ABP_HERMES_MANAGER_DIR`;
2. `hermes_manager.path` in `config/backends.yaml`;
3. a `Hermes-Manager` folder next to ABP's own (a developer's working copy, used as it is);
4. `data/modules/Hermes-Manager`, where **Install** clones it.

**Install** clones the repo if needed, then runs `npm install`, downloads Electron and runs `npm run build`. **Update**
pulls and rebuilds, and refuses while the checkout has uncommitted changes. Hermes itself is found the way the app finds
it: `%LOCALAPPDATA%\hermes`, with its venv Python for the bridge.

## The page

| Tab | What it does |
|---|---|
| Overview | Installed, built, commit, updates waiting. Install, Update, start and stop a headless bridge, open the window, add its MCP server to ABP. Hermes's health |
| Gateway | The Hermes gateway's state; start, stop, restart, drain |
| Window | The Hermes Manager window, live: pick a section, a screenshot (refreshed every 1.5 s when *live* is on), every element on screen, which you can click, fill in, choose from or read |
| All features | Every operation (49), with forms built from their own schemas |

## The agent's tools

Reading is free. Anything that changes Hermes or the window asks for approval first.

| Tool | What it does |
|---|---|
| `hm_status` | Installed, built, bridge and window, Hermes's health |
| `hm_operations` | Search the operations, or one operation's arguments |
| `hm_read` | Run an operation that changes nothing (refuses the others) |
| `hm_call` | Run any operation: gateway lifecycle, config apply, backups, updates, skill toggles, cron... |
| `hm_gui_look` | The window without changing it: sections, elements, text, screenshot |
| `hm_gui_act` | Drive the window: open it, switch sections, click, fill, select, tick, keys, window state |
| `hm_setup` | Install, update, start or stop a headless bridge, register its MCP server |

## Checked on this machine (2026-09-28)

ABP found the working copy at `Z:\Projects\Hermes-Manager` and Hermes at `%LOCALAPPDATA%\hermes`. It then:

- used the running bridge: 49 operations, the gateway (running), 10,037 skills;
- opened the window through the bridge, switched to Config, read the widgets and took a screenshot.

The same run found and fixed a Config page bug: unset boolean settings were displayed as "true".
