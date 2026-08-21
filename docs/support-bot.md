# Support Bot — a local, dependency-free AI assistant

The Support Bot is a chat panel built into the desktop dashboard (sidebar,
below Chat) that understands both plain English and every slash command,
and can carry out any server-management task Bot Server exposes — restart
Claude Desktop, show or change the default backend, list/enable/disable
MCP servers, enable/disable/restart a bot instance, and more — without
you ever needing to open a config file or remember exact command syntax.

The Android app has an equivalent **Support** tab, talking to the exact
same server-side engine over `POST /api/support-bot/ask` and
`/api/support-bot/confirm` — there is no separate "mobile" intelligence.

## Why it's a real, custom model — not a wrapper

This is a deliberate design constraint, not a shortcut: **no Ollama, no
external inference API, no new dependency.** `requirements.txt` didn't
change to add this. It's built entirely from Python's standard library
(`math`, `re`, `collections`) as a genuine, trainable text-classification
model — just intentionally small, matching "route simple management
requests using the local machine's own resources," not "run a chatbot."

The model is a classic, well-understood technique:

1. **TF-IDF vectorization** — each training phrase becomes a vector of
   term-frequency × inverse-document-frequency weights (smoothed IDF:
   `log((1 + n_docs) / (1 + doc_count)) + 1.0`), computed once at import
   time from `training_data.py`.
2. **Nearest-centroid classification** — every intent's training examples
   are averaged into one centroid vector. A new message is vectorized the
   same way and classified by cosine similarity to the closest centroid.
3. **A confidence floor** (`CONFIDENCE_THRESHOLD = 0.22`) — below it, the
   reply is an honest "not sure what you mean," never a guess dressed up
   as a real answer.

This is fast (sub-millisecond per classification), needs no GPU, no
network call, and no training step beyond writing example phrases — which
is also how you extend it (see below).

## Architecture (`bot/support_bot/`)

| File | Role |
|---|---|
| `training_data.py` | `EXAMPLES: list[tuple[phrase, intent]]` — ~150 hand-written phrasings across 21 intents, plus `DESTRUCTIVE_INTENTS` (the subset that needs confirmation). |
| `model.py` | `TfidfCentroidModel` — builds centroids from `EXAMPLES` at import time; `predict(text) -> (intent, confidence)`. Module-level singleton `model`. |
| `slots.py` | Fuzzy argument extraction: `find_bot_name`/`find_mcp_server_name` (`difflib.get_close_matches` against whatever's actually configured right now, so "restart the telegrma bot" still resolves), `find_backend`, `find_model`. |
| `actions.py` | `INTENT_HANDLERS` / `ASYNC_INTENT_HANDLERS` — one function per intent, each a thin wrapper over an existing `bot/*` function. No business logic lives here that doesn't already exist elsewhere in the app. |
| `engine.py` | `SupportBot` — ties it together: classify → confirm-gate if destructive → execute → reply. Module-level singleton `support_bot`. |

### The 21 intents

`status`, `list_bots`, `bot_create`, `bot_edit`, `bot_delete`,
`bot_enable`, `bot_disable`, `bot_restart`, `backend_show`,
`backend_set`, `model_show`, `model_set`, `mcp_list`, `mcp_enable`,
`mcp_disable`, `mcp_logs`, `desktop_start`, `desktop_stop`,
`desktop_restart`, `config_reload`, `allowed_users_list`, `help`.

`bot_create`/`bot_edit` deliberately reply with "use the Bots tab" rather
than accepting credentials via free text — the model never becomes a path
for typing a platform token into a chat message.

### Request flow (`SupportBot.handle(text, actor)`)

```
text starts with "/"?
  └─ yes → dispatch_command() (bot/commands.py) — same slash-command
           core Telegram/Discord/Slack use. No NLP involved at all.
  └─ no  → model.predict(text) → (intent, confidence)
           confidence < 0.22?
             └─ yes → "not sure what you mean" reply
             └─ no  → slots.extract(intent, text) → arguments
                       intent in DESTRUCTIVE_INTENTS
                       and security.confirm_destructive is on?
                         └─ yes → reply describing the action,
                                  needs_confirm=True, a confirm_token
                                  (in-memory, 5-minute TTL, never persisted)
                         └─ no  → run INTENT_HANDLERS[intent] now,
                                  return its result as the reply
```

`SupportBot.confirm(token, actor)` looks up the pending action by token
and runs it — the only way a destructive intent from natural language
ever actually executes.

### Destructive intents

`bot_delete`, `bot_disable`, `desktop_stop`, `desktop_restart`,
`mcp_disable`, `bot_restart` — the same category of action `/stop_desktop`
already gates behind a confirm step in every other chat platform, honoring
the same `security.confirm_destructive` config flag
(`config/backends.yaml`). Turn that off and every intent (destructive or
not) executes immediately — same trade-off as turning it off for the
slash-command confirm flow elsewhere in the app.

## Extending it — adding a new intent

1. Add 10–20 example phrasings to `training_data.py`'s `EXAMPLES` for your
   new intent name. More variety (different phrasings, word order,
   synonyms) makes the classifier more robust than more *volume* of near-
   identical examples.
2. If it needs arguments (a bot name, a path, etc.), add an extractor
   function to `slots.py`.
3. Add a handler to `actions.py`'s `INTENT_HANDLERS` (or
   `ASYNC_INTENT_HANDLERS` if it needs `await`) that calls the existing
   `bot/*` function for that action — don't write new business logic here
   if an equivalent already exists for the dashboard API.
4. If it's destructive, add its name to `training_data.py`'s
   `DESTRUCTIVE_INTENTS`.
5. Sanity-check offline before touching the server, e.g.:
   ```powershell
   .\.venv\Scripts\python.exe -c "from bot.support_bot.model import model; print(model.predict('your new example phrase'))"
   ```
   Confirm it picks your intent with reasonable confidence, and that it
   didn't accidentally steal confidence from a similar existing intent
   (if two intents' phrasings overlap too much, add more distinguishing
   examples to each).

## Where the confirm token lives

`SupportBot._pending: dict[str, tuple[intent, args_json, actor, expires_at]]`
— a plain in-memory dict on the `SupportBot` singleton, `CONFIRM_TTL_S =
300`. It is not written to the database and does not survive a server
restart — a pending confirmation from before a restart simply expires
silently rather than resurrecting into an unexpected action.
