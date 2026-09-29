# TransferDaemon in ABP

[TransferDaemon](https://github.com/LoopyLuci/TransferDaemon) sends end-to-end encrypted, post-quantum messages and
files between your own devices: desktop, Android, and relays in between. It stays its own program in its own repo;
ABP builds it, keeps it updated, runs it and drives it.

## What ABP gives you

- **The TransferDaemon page.** Tabs:
  - Overview: install and build, update, start and stop, open the window or the terminal UI, add its MCP server.
  - Messages: conversations, send text or files, add contacts.
  - Transfers: progress, pause, resume, cancel.
  - Window: a live screenshot and every widget on screen, clickable and fillable.
  - Terminal UI: its screen as text and a key pad.
  - Relays: start local relays and probe any relay.
  - All features: every operation, as a form built from its schema.
  - Audit.

  A **Machine** selector runs any of it on a linked server.
- **Agent tools:**

  | Tool | What it does |
  |---|---|
  | `td_status` | installed, built, running, identity, counts |
  | `td_operations` | search the operations |
  | `td_read` | run any operation that changes nothing |
  | `td_call` | run any operation (asks first) |
  | `td_send` | a message or file to a contact by name (asks first) |
  | `td_gui_look`, `td_gui_act` | see and drive the window |
  | `td_tui` | the terminal UI: headless or in a console window |
  | `td_setup` | install, update, start and stop the daemon, add its MCP server |

  All of them take `machine`: a linked server's name.
- **Its own MCP server.** `transferd-cli mcp` (one click on the page, or `td_setup register_mcp`), for any MCP client.
- **API:** `/api/transferdaemon/*`. `status`, `setup`, `update`, `jobs`, `daemon/start`, `daemon/stop`, `window`,
  `tui`, `mcp`, `operations`, `call`, `send` and `audit`.

## How it fits together

TransferDaemon's daemon (`transferd`) runs a **control hub** on `127.0.0.1:50060` and writes `control.json` in its
data folder (`%LOCALAPPDATA%\transferdaemon` on Windows). ABP reads that file, so there is nothing to configure. The
hub serves:

- the daemon's whole API: 47 calls covering account, contacts, groups, messages, transfers, connections, settings,
  calls, telemetry and updates;
- the window (`gui.*`) and the terminal UI (`tui.*`), which attach to it;
- local relays (`relay.*`).

That is about 80 operations. TransferDaemon's own [docs/CONTROL.md](https://github.com/LoopyLuci/TransferDaemon/blob/main/docs/CONTROL.md)
describes it. The window is driven the way a person would:

- widgets are read from its accessibility tree;
- the pointer moves, presses and releases;
- text is typed.

So "click send" means the same thing it means for a person.

## Where it is installed

The first match wins:

1. `$ABP_TRANSFERDAEMON_DIR`;
2. `transferdaemon.path` in `config/backends.yaml`;
3. a `TransferDaemon` folder next to ABP's (a developer's working copy, used as is);
4. `data/modules/TransferDaemon`, cloned by setup.

ABP builds it with `cargo build --release`: the daemon, both user interfaces, the CLI and the relays. Rust has to be
installed. Updating pulls from the repo and rebuilds, and it refuses when there is uncommitted work.

Optional settings:

```yaml
transferdaemon:
  relays: ["wss://transferd-relay.example.workers.dev"]   # TRANSFERD_RELAY_ADDR for the daemon ABP starts
  bind: 0.0.0.0             # accept direct peers on the LAN / tailnet (TRANSFERD_BIND_ADDR)
  dht_bootstrap: 1.2.3.4:7901
  profile: release          # or debug: which build to run
```

## On another machine

To reach TransferDaemon on a linked server (Peers page), that server's owner allows it once: tick **TransferDaemon**
under "What linked servers may control here" on its Power page, or add `transferdaemon` to its
`peers.remote_control`. Then pick the server in the page's Machine selector, or pass `machine` to the tools. See
[power-and-remote-control.md](power-and-remote-control.md).
