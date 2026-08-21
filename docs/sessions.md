# Session linking — never sending into the wrong chat

`ui` (Claude Desktop UI automation) and `hermes_gateway` (Hermes Agent's
JSON-RPC gateway) are different from Bot Server's other three backends in
one important way: they each drive a **real, persistent conversation** in
a real desktop app, not a single stateless request/response call. That
raises a real risk once more than one bot instance can route to them —
without tracking which real chat belongs to which bot instance, messages
could land in whatever chat happens to be open, or two instances could
fight over the same window.

Bot Server closes that gap with an explicit **session link**: every
`ui`/`hermes_gateway` bot instance is tied to one specific,
already-created chat/session, stored as `bot_instances.desktop_session_key`,
and every message re-selects that exact chat before sending — never
"whatever's currently open."

## The rule

> A `ui`/`hermes_gateway` bot instance's messages only ever go to the one
> chat/session it is linked to. If it has no link yet, one is created
> automatically on first use. If its linked chat can no longer be found
> (renamed or deleted from inside the real app), sending fails loudly
> instead of falling back to guessing.

## How it works per backend

### `ui` (Claude Desktop)

Claude Desktop is one window with many chats in its sidebar. The session
key for this backend is **the linked chat's sidebar label**, captured at
creation time:

1. `create_session()` connects to the window, clicks the "New chat"
   button (`_find_new_chat_button` — matches on automation id if
   configured, otherwise a text heuristic: a button whose name contains
   "new chat" or is exactly "+"), and reads the resulting top sidebar
   entry's label as the session key.
2. Every `ask()` call re-selects the sidebar item with that exact label
   (`_select_session`) before typing into the input field. If no sidebar
   item matches, it raises rather than typing into whatever's currently
   selected.
3. A single `asyncio.Lock` on the shared `UiBackend` object serializes
   every call — there is only one real OS window, so two bot instances'
   chat-switch-then-type sequences must never interleave.

**Tuning for your Claude Desktop version** (`config/backends.yaml`,
`backends.ui`):

| Key | Default | When to set it |
|---|---|---|
| `new_chat_button_automation_id` | unset (heuristic) | The "New chat"/"+" text heuristic doesn't find your version's button. |
| `sidebar_item_control_type` | `ListItem` | Your version exposes sidebar chats as a different UIA control type (e.g. `TreeItem`). |
| `input_automation_id` / `send_button_automation_id` | unset (heuristic) | Same idea, for the message box/send button (pre-existing, unrelated to sessions). |

To find the right values for your install, inspect the live window's
control tree — from the project's own venv:

```powershell
.\.venv\Scripts\python.exe -c "from pywinauto import Desktop; win = Desktop(backend='uia').window(title_re='Claude.*'); win.wait('exists enabled visible ready', timeout=5); [print(repr(b.window_text()), b.automation_id()) for b in win.descendants(control_type='Button')]"
```

(Claude Desktop must be running. If nothing useful prints, try
`control_type='TreeItem'` or `'Text'` instead of `'Button'` to explore.)

### `hermes_gateway` (Hermes Agent)

The session key here is Hermes's own real `session_id`:

1. `create_session()` calls the gateway's `session.create` RPC and
   returns the `session_id` it hands back.
2. Every `ask()` call reuses that same `session_id` in its
   `prompt.background` call instead of creating (and discarding) a new
   one — so the conversation actually has memory across messages now,
   instead of the old behavior of a throwaway session per call.
3. A call with **no linked bot instance at all** (e.g. an ad-hoc `/ask`
   not tied to any instance) keeps the old stateless behavior — a fresh
   session every time — since there's nothing to persist a link against.

## Creating a new session on purpose

Three equivalent ways to deliberately start a fresh conversation instead
of continuing the currently-linked one:

- **Desktop dashboard** — the **Bots** tab shows a **New Session** button
  on every `ui`/`hermes_gateway` row, next to the currently-linked
  session (or "No session linked yet"). Confirms before acting, since it
  opens a real new chat in the real app.
- **Slash command** — `/new_session`, from that bot's own chat, on any
  platform (Telegram/Discord/Slack/Support Bot/Android).
- **API** — `POST /api/bots/{instance_id}/session/new` (same auth tier as
  the rest of the Bots API — desktop token or a paired mobile API key).

All three end up calling `Router.create_session(instance_id)`
(`bot/router.py`), which resolves the instance's own backend, calls that
backend's `create_session()`, and persists the returned key via
`bot_instances.set_desktop_session_key()` — the single place this link is
ever written.

## What happens on the very first message

`Router.ask()` (`bot/router.py`) looks up the instance's
`desktop_session_key` before calling the backend. If it's `None`, the
backend itself creates one on the fly (same `create_session()` logic) as
part of that first `ask()` call, and the router persists whatever key
comes back via `BackendResult.raw["desktop_session_key"]`. You don't have
to click "New Session" before ever using a freshly-created bot instance —
it links itself automatically the first time.

## Troubleshooting

- **"linked chat '...' is no longer in the Claude Desktop sidebar"** — the
  chat was renamed or deleted inside Claude Desktop itself. Click **New
  Session** to relink.
- **"no 'New chat' button found"** — the text heuristic didn't match your
  Claude Desktop version's button. Set
  `backends.ui.new_chat_button_automation_id` (see above).
- **Two bots seem to be fighting over the same chat** — check they aren't
  both pointed at the same `desktop_session_key` by coincidence (this
  shouldn't happen through normal use, since each gets its own key on
  creation, but is worth checking after a manual DB edit or a restored
  `bot_instances` backup from before this feature existed — those rows
  have `desktop_session_key = NULL` and will each link fresh on next use).
