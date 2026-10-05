# Keyboard shortcuts, the command palette and right-click menus

Every action ABP's two UIs offer is one entry in a single registry, with an id, a label, a default
shortcut and the screen it belongs to. The web dashboard and the desktop app load the *same* two
files, so the two UIs cannot drift into offering different things:

| File | What it is |
| --- | --- |
| `bot/dashboard/static/action-registry.js` (= `desktop-app/ui/action-registry.js`) | the registry: every action, its id, its default chord, its screen, and the code that runs it |
| `bot/dashboard/static/shortcuts.js` (= `desktop-app/ui/shortcuts.js`) | the engine: chord matching, the help overlay, the palette, the menus, saved bindings, the Tauri bridge |
| `bot/dashboard/shortcuts_api.py` | where a person's own bindings are kept (`GET`/`PUT`/`POST /api/shortcuts/check`/`DELETE /api/shortcuts`) |

`tests/test_shortcuts.py` fails if the two copies of either JS file differ, if an id is duplicated,
if two actions claim the same chord where both could be live, if a default is a chord the browser
or the OS already owns, if an action id referenced by the UI, the tests or this file is missing
from the registry, or if this table and the registry fall out of step.

## Using them

- **`Ctrl+K` (`Cmd+K`)** opens the command palette: fuzzy search over every action, the eight you
  used most recently first, entirely keyboard-operable (`↑`/`↓`/`Home`/`End`, `Tab` also moves,
  `Enter` runs, `Esc` closes).
- **`?`** (or `Ctrl+/`) opens the help overlay: every shortcut for the screen you are looking at,
  plus the customiser. **Customise** turns every row into a "change" button; press it, press the
  keys you want, and the binding is saved. A chord that is already taken - or that belongs to the
  browser or the OS - is refused on the spot, and any conflict still in the set is listed under the
  list. **Reset all to defaults** clears the lot.
- **Right-click** a bot card, a chat message, a file, a module card, a process row, a log line, a
  routine, a session, an activity entry or a kanban card for its actions. `Shift+F10` or the
  `Menu` key opens the same menu on whatever has keyboard focus, `↑`/`↓` move through it, `Esc`
  closes it and puts focus back. Inside a text field nothing is taken over: the browser's own
  copy-and-paste menu still appears there.
- Shortcuts never fire while you are typing, except the ones meant to (`Ctrl+Enter` to send).

### Chord syntax

A chord is up to three keys in sequence, each written as its modifiers joined by `+` and the key
itself: `mod+k`, `mod+alt+shift+c`, `g` then `vi` (written `g vi`). `mod` is `Ctrl` on
Windows/Linux and `Cmd` on macOS; `cmd` and `ctrl` are stored as `mod` and `opt`/`option` as `alt`,
so one saved binding means the same thing on every machine. The help overlay shows the real keys
for the machine you are on.

## Your own bindings

Bindings are a preference about this ABP install rather than about one browser, so they are stored
server-side under `shortcuts.bindings` in `config/backends.yaml` - which means the same keys work
in another browser, on a paired phone, and in the desktop app.

- `GET /api/shortcuts` - the saved set. Reading follows the dashboard's normal auth, so a paired
  device can see what is bound.
- `PUT /api/shortcuts` with `{"bindings": {"ui.palette": "mod+p", "bot.toggle": ""}}` - replaces
  the whole set. Needs the dashboard token: a binding is a standing instruction to act without
  being asked again. Checked before anything is written, so a rejected request changes nothing.
  A 400 names the problem: a chord ABP cannot read, a chord the browser or OS owns, or two actions
  that would end up on one chord.
- `POST /api/shortcuts/check` with `{"chord": "mod+p", "bindings": {...}}` - "is this chord free
  right now?", so the customiser can answer before you save.
- `DELETE /api/shortcuts` - forget every custom binding.

An empty string means "this action deliberately has no shortcut", and is kept as such (distinct
from "not mentioned", which keeps the default).

## The contract with the desktop app's global shortcuts

The desktop app's Rust side (`desktop-app/src-tauri`) registers **global** (OS-level) shortcuts and
emits one event:

```
abp://action   {"action": "<action id>"}
```

Both UIs subscribe to it only when running inside Tauri (`window.__TAURI__` is present), and run
the action **through the registry** - so a global shortcut gets the same screen-scoped resolution,
the same confirmation dialogs and the same "the row you meant" logic as a key pressed in the
window. An id the registry does not have is reported in a toast rather than swallowed.

So any of the ids in the table below is a valid payload. The ones that make sense as OS-level
global shortcuts are the screen-independent, non-destructive ones; the rest still work, but a
global shortcut for "kill the cell under my cursor" has no cursor when ABP is in the background,
which is why the row-scoped actions below deliberately have no default chord.

`tests/test_shortcuts.py` also asserts that every id in this file exists in the registry, so a
typo here fails the build rather than a person's keyboard.

## The action ids

Generated from the registry; `tests/test_shortcuts.py` checks this table against it.

<!-- BEGIN GENERATED TABLE -->
### General

| Action id | What it does | Default | Applies on |
| --- | --- | --- | --- |
| `ui.palette` | Command palette | `mod+k` | anywhere |
| `ui.help` | Keyboard shortcuts | `?`, `mod+/` | anywhere |
| `ui.refresh` | Refresh everything on screen | `mod+alt+r` | anywhere |
| `ui.theme` | Switch between light and dark | `mod+alt+t` | anywhere |
| `ui.sidebar` | Collapse or expand the sidebar | `mod+alt+s` | anywhere |
| `ui.terminal` | Show or hide Terminal and Activity | `mod+alt+j` | anywhere |
| `ui.search` | Search this screen | `/` | anywhere |
| `copy.selection` | Copy the selected text | `mod+alt+shift+c` | anywhere |
| `config.reload` | Re-read the config from disk | `mod+alt+shift+r` | anywhere |

### Go to

| Action id | What it does | Default | Applies on |
| --- | --- | --- | --- |
| `nav.overview` | Go to Overview | `g o` | anywhere |
| `nav.jobs` | Go to Jobs | `g j` | anywhere |
| `nav.telemetry` | Go to Connections & Telemetry | `g te` | anywhere |
| `nav.database` | Go to Database | `g db` | anywhere |
| `nav.control` | Go to Control Center | `g c` | anywhere |
| `nav.resilience` | Go to Resilience | `g re` | anywhere |
| `nav.diagnostics` | Go to Diagnostics | `g dg` | anywhere |
| `nav.logs` | Go to Live Logs | `g l` | anywhere |
| `nav.chat` | Go to Chat | `g h` | anywhere |
| `nav.server-chat` | Go to Server Chat | `g sc` | anywhere |
| `nav.support` | Go to Support Bot | `g sp` | anywhere |
| `nav.sessions` | Go to Sessions | `g se` | anywhere |
| `nav.bots` | Go to Bots | `g b` | anywhere |
| `nav.models` | Go to Models | `g m` | anywhere |
| `nav.router` | Go to Model Router | `g ro` | anywhere |
| `nav.unsloth` | Go to Unsloth | `g un` | anywhere |
| `nav.cluster` | Go to Cluster | `g cl` | anywhere |
| `nav.octopus` | Go to Octopus | `g oc` | anywhere |
| `nav.vision` | Go to Vision | `g vi` | anywhere |
| `nav.hosting` | Go to Hosting | `g ho` | anywhere |
| `nav.storage` | Go to Storage | `g st` | anywhere |
| `nav.localai` | Go to Local AI | `g lo` | anywhere |
| `nav.lab` | Go to Neural Lab | `g lb` | anywhere |
| `nav.studio` | Go to Studio | `g sd` | anywhere |
| `nav.modules` | Go to Modules | `g mo` | anywhere |
| `nav.transferdaemon` | Go to TransferDaemon | `g td` | anywhere |
| `nav.power` | Go to Power | `g pw` | anywhere |
| `nav.vm-harness` | Go to VM-Harness | `g vm` | anywhere |
| `nav.hermes-manager` | Go to Hermes Manager | `g hm` | anywhere |
| `nav.ollama` | Go to Ollama | `g ol` | anywhere |
| `nav.kanban` | Go to Kanban | `g kb` | anywhere |
| `nav.agents` | Go to ABP Agents | `g ag` | anywhere |
| `nav.swarms` | Go to Swarms | `g sw` | anywhere |
| `nav.tailscale` | Go to Tailscale | `g ts` | anywhere |
| `nav.containers` | Go to Containers | `g co` | anywhere |
| `nav.vms` | Go to Virtual Machines | `g vs` | anywhere |
| `nav.infra-rules` | Go to Infra Automation | `g ir` | anywhere |
| `nav.browser` | Go to Browser | `g br` | anywhere |
| `nav.ssh-toolkit` | Go to SSH Toolkit | `g sk` | anywhere |
| `nav.automation` | Go to Automation | `g au` | anywhere |
| `nav.routines` | Go to Routines | `g rt` | anywhere |
| `nav.customize` | Go to Customize UI | `g cu` | anywhere |
| `nav.training` | Go to Training | `g tr` | anywhere |
| `nav.platforms` | Go to Platforms | `g pf` | anywhere |
| `nav.mobile` | Go to Mobile | `g mb` | anywhere |
| `nav.files` | Go to Files | `g fi` | anywhere |
| `nav.servers` | Go to Linked Servers | `g sv` | anywhere |
| `nav.updates` | Go to Updates | `g up` | anywhere |

### Chat

| Action id | What it does | Default | Applies on |
| --- | --- | --- | --- |
| `chat.focus` | Jump to the message box | `mod+alt+i` | `chat` |
| `chat.send` | Send the message | `mod+enter` | `chat` |
| `chat.new` | Start a new session with this bot | `mod+alt+n` | `chat` |
| `chat.copy` | Copy this conversation as text | `mod+alt+c` | `chat` |
| `chat.clear` | Delete this bot's chat history | `mod+alt+delete` | `chat` |
| `chat.mode` | Switch between Chat with Bot and Send from Server | `mod+alt+m` | `chat` |
| `chat.instance.next` | Next bot in this chat | `mod+alt+]` | `chat` |
| `chat.instance.prev` | Previous bot in this chat | `mod+alt+[` | `chat` |
| `chat.message.copy` | Copy this message | — | `chat` |
| `chat.message.quote` | Quote this message into the box | — | `chat` |
| `chat.message.retry` | Ask the bot again with this message | — | `chat` |
| `chat.message.delete` | Delete this message | — | `chat` |
| `serverchat.send` | Send from the server | `mod+enter` | `server-chat` |
| `serverchat.message.copy` | Copy this message | — | `server-chat` |
| `serverchat.message.delete` | Delete this message | — | `server-chat` |
| `support.send` | Send this to the Support Bot | `mod+enter` | `support` |

### Bots

| Action id | What it does | Default | Applies on |
| --- | --- | --- | --- |
| `bot.toggle` | Start or stop this bot | `mod+alt+b` | `bots` |
| `bot.restart` | Restart this bot | `mod+alt+shift+b` | `bots` |
| `bot.edit` | Edit this bot | `mod+alt+e` | `bots` |
| `bot.agent` | Agent settings for this bot | — | `bots` |
| `bot.chat` | Chat with this bot | — | `bots` |
| `bot.delete` | Delete this bot | `mod+alt+shift+e` | `bots` |

### Processes

| Action id | What it does | Default | Applies on |
| --- | --- | --- | --- |
| `processes.open` | Open the Processes panel | `mod+alt+p` | anywhere |
| `process.kill` | Kill this cell | — | `diagnostics` |
| `estop.emergency` | Emergency stop: kill every cell | `mod+alt+shift+x` | anywhere |
| `estop.agent` | Engage or release the agent emergency stop | — | anywhere |

### Routines

| Action id | What it does | Default | Applies on |
| --- | --- | --- | --- |
| `routine.run` | Run this routine now | `mod+alt+g` | `routines` |
| `routine.refresh` | Go to Routines and reload the list | — | `routines` |

### Files

| Action id | What it does | Default | Applies on |
| --- | --- | --- | --- |
| `files.refresh` | Re-read this folder | `mod+alt+f` | `files` |
| `file.download` | Download this file | — | `files` |
| `file.copyPath` | Copy this file's path | — | `files` |

### Logs

| Action id | What it does | Default | Applies on |
| --- | --- | --- | --- |
| `logs.copyLine` | Copy this log line | — | `logs` |
| `logs.copyAll` | Copy the whole log view | `mod+alt+shift+l` | `logs` |

### Activity

| Action id | What it does | Default | Applies on |
| --- | --- | --- | --- |
| `activity.clear` | Clear the Activity list (the log itself is untouched) | `mod+alt+shift+a` | anywhere |
| `activity.copy` | Copy this activity entry | — | anywhere |

### Sessions

| Action id | What it does | Default | Applies on |
| --- | --- | --- | --- |
| `sessions.export` | Export this session as JSON | `mod+alt+shift+j` | `sessions` |

### Modules

| Action id | What it does | Default | Applies on |
| --- | --- | --- | --- |
| `modules.refresh` | Go to Modules and reload the list | `mod+alt+o` | `modules` |
| `module.open` | Open this module's page | — | `modules` |
| `module.act` | Run this module's action (install, update, hub) | — | `modules` |

### Kanban

| Action id | What it does | Default | Applies on |
| --- | --- | --- | --- |
| `kanban.refresh` | Refresh the board | `mod+alt+shift+k` | `kanban` |
| `kanban.card.copy` | Copy this card's text | — | `kanban` |
| `kanban.card.delete` | Delete this card | — | `kanban` |

<!-- END GENERATED TABLE -->