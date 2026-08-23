# Support Bot — a local, hybrid AI assistant

The Support Bot is a chat panel — in the desktop dashboard's **Support
Bot** tab, and the Android app's equivalent **Support** tab, both talking
to the exact same server-side engine over `POST /api/support-bot/ask` and
`/api/support-bot/confirm` (there is no separate "mobile" intelligence) —
that understands both plain English and every slash command, and can
carry out any server-management task Bot Server exposes: restart Claude
Desktop, show or change the default backend, list/enable/disable MCP
servers, enable/disable/restart a bot instance, inspect jobs and swarms,
run diagnostics, manage backups and paired devices, browse sessions, and
check Claude Desktop/Hermes Agent setup — all without ever opening a
config file or remembering exact command syntax.

## The hybrid classifier — deterministic + a real trained neural network

Two independently-trained models classify every message, and a third
tier of logic decides which one to trust:

1. **`model.py`'s `TfidfCentroidModel`** — the original, deliberately
   dependency-free design: TF-IDF vectors + nearest-centroid classification,
   pure Python stdlib (`math`, `re`, `collections`), sub-millisecond,
   fully explainable. See "Why it's dependency-free" below.
2. **`nn_model.py`'s `NeuralIntentClassifier`** — a genuine neural
   network: a one-hidden-layer, 64-unit ReLU multi-layer perceptron
   (scikit-learn's `MLPClassifier`), trained by real backpropagation over
   TF-IDF unigram+bigram features. This is the one place in this project
   that *does* pull in an ML dependency (`scikit-learn`, see
   `requirements.txt`) — deliberately isolated to this module so nothing
   else in the codebase depends on it.
3. **`hybrid.py`'s `classify()`** — runs both, every time, and combines
   them:
   - Both agree on a real intent → **`"ensemble"`**, the strongest
     possible signal (two differently-biased models independently landed
     on the same answer).
   - Both confident but disagree → trust whichever is more confident
     (ties favor TF-IDF, the explainable one).
   - Only one is confident → use that one.
   - Neither → `"unknown"`, same honest "not sure what you mean" as
     before.

Both models train on the **identical** corpus (`training_data.py`'s
`EXAMPLES` plus whatever the Training tab has added) — only the algorithm
differs, which is exactly what makes agreement/disagreement a meaningful
signal rather than two coin flips.

### Self-monitoring

Every single classification — both sub-models' raw verdicts, which one
won, whether they agreed — is logged to the `support_bot_classifications`
table (`bot/db.py`'s `log_support_bot_classification()`). The dashboard's
**Training** tab renders this as a live "Model health" panel: total
classifications, agreement rate, unknown rate, and average confidence per
model — computed from real logged traffic (`GET
/api/support-bot/health`), not a static claim.

### Modularity / scalability

`hybrid.py`'s `CLASSIFIERS` is a plain `list[(name, predict_fn)]` — adding
a third sub-model later (an embedding-based classifier, an LLM-backed
one, whatever) means appending one entry and extending the voting rule;
nothing in `engine.py` or the dashboard needs to know about the change,
since they only ever see a `HybridResult`.

## Why the original model is dependency-free

This was a deliberate design constraint for `model.py` specifically, not
a limitation of the whole system: **no Ollama, no external inference API,
no new dependency** for the base classifier. It's built entirely from
Python's standard library as a genuine, trainable text-classification
model:

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

## Architecture (`bot/support_bot/`)

| File | Role |
|---|---|
| `training_data.py` | `EXAMPLES: list[tuple[phrase, intent]]` — ~190 hand-written phrasings across 40 intents, plus `DESTRUCTIVE_INTENTS` (the subset that needs confirmation). |
| `model.py` | `TfidfCentroidModel` — the deterministic sub-model; `predict(text) -> (intent, confidence)`. Module-level singleton `model`; `retrain()` rebuilds it in place. |
| `nn_model.py` | `NeuralIntentClassifier` — the trained-NN sub-model, same `predict()`/`retrain()` contract as `model.py`. |
| `hybrid.py` | `classify(text) -> HybridResult` — runs both sub-models, decides which to trust, logs the outcome. `retrain_all()` and `health()` for the Training tab. |
| `slots.py` | Fuzzy/regex argument extraction: bot/swarm/MCP/device names (`difflib.get_close_matches` against whatever's actually configured right now), backend/model names, job/session numbers, settings paths, booleans, backup filenames. |
| `actions.py` | `INTENT_HANDLERS` / `ASYNC_INTENT_HANDLERS` — one function per intent, each a thin wrapper over an existing `bot/*` function. No business logic lives here that doesn't already exist elsewhere in the app. |
| `engine.py` | `SupportBot` — ties it together: `hybrid.classify()` → confirm-gate if destructive → execute → reply. Module-level singleton `support_bot`. |

### The 40 intents

Server/bot management (original set): `status`, `list_bots`,
`bot_create`, `bot_edit`, `bot_delete`, `bot_enable`, `bot_disable`,
`bot_restart`, `backend_show`, `backend_set`, `model_show`, `model_set`,
`mcp_list`, `mcp_enable`, `mcp_disable`, `mcp_logs`, `desktop_start`,
`desktop_stop`, `desktop_restart`, `config_reload`, `allowed_users_list`.

Jobs & swarms: `jobs_list`, `job_status`, `swarms_list`, `swarm_run`,
`swarm_run_status`.

Diagnostics, backups & toggles: `diagnostics`, `db_status`, `db_vacuum`,
`backups_list`, `backup_restore`, `settings_show`, `settings_set`.

Mobile & sessions: `devices_list`, `device_revoke`, `mobile_key_create`,
`sessions_list`, `session_show`.

Claude/Hermes connection setup: `claude_setup_check`,
`hermes_setup_check` — see
[docs/connecting-claude-and-hermes.md](connecting-claude-and-hermes.md)
for the full manual walkthrough these two intents automate the checks
from.

`help`.

`bot_create`/`bot_edit` deliberately reply with "use the Bots tab" rather
than accepting credentials via free text — the model never becomes a path
for typing a platform token into a chat message.

### Request flow (`SupportBot.handle(text, actor)`)

```
text starts with "/"?
  └─ yes → dispatch_command() (bot/commands.py) — same slash-command
           core Telegram/Discord/Slack use. No NLP involved at all.
  └─ no  → hybrid.classify(text) → HybridResult(intent, confidence, ...)
           intent == "unknown"?
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
`mcp_disable`, `bot_restart`, `db_vacuum`, `backup_restore`,
`device_revoke` — the same category of action `/stop_desktop` already
gates behind a confirm step in every other chat platform, honoring the
same `security.confirm_destructive` config flag (`config/backends.yaml`).
Turn that off and every intent (destructive or not) executes immediately
— same trade-off as turning it off for the slash-command confirm flow
elsewhere in the app.

## The Training tab — teaching the classifier without editing code

The dashboard's **Training** tab (and the model-health panel described
above) is the runtime front-end for this file's advice. Adding a phrase
there does exactly steps 1 and 5 below for you, automatically, against
**both** sub-models, immediately:

- `POST /api/support-bot/training {phrase, intent}` → `db.add_support_bot_phrase()`
  → `hybrid.retrain_all()` (rebuilds both `model.py`'s centroids and
  `nn_model.py`'s MLP from the baseline + every stored phrase).
- `DELETE /api/support-bot/training/{id}` does the same in reverse.
- `GET /api/support-bot/training` returns the current custom phrases and
  the full list of known intent names (for the tab's dropdown).

Phrases added this way live in the `support_bot_phrases` table — additive
only, never edits to `training_data.py` itself, so upgrading the app never
clobbers anything you've taught it.

## Extending it — adding a brand new intent (requires a code change)

Teaching an *existing* intent a new phrasing is a Training-tab action (see
above) — no code, no restart. Adding a genuinely *new* intent (a new
management action that doesn't exist yet) still requires editing code:

1. Add 10–20 example phrasings to `training_data.py`'s `EXAMPLES` for your
   new intent name. More variety (different phrasings, word order,
   synonyms) makes both sub-models more robust than more *volume* of
   near-identical examples.
2. If it needs arguments (a bot name, a path, a number, etc.), add an
   extractor function to `slots.py`.
3. Add a handler to `actions.py`'s `INTENT_HANDLERS` (or
   `ASYNC_INTENT_HANDLERS` if it needs `await`) that calls the existing
   `bot/*` function for that action — don't write new business logic here
   if an equivalent already exists for the dashboard API.
4. If it's destructive, add its name to `training_data.py`'s
   `DESTRUCTIVE_INTENTS`.
5. Sanity-check offline before touching the server, e.g.:
   ```powershell
   .\.venv\Scripts\python.exe -c "from bot.support_bot.hybrid import classify; print(classify('your new example phrase', log=False))"
   ```
   Confirm it picks your intent with reasonable confidence from **both**
   sub-models (a `HybridResult` with `source == "ensemble"` is the
   strongest signal you got it right), and that it didn't accidentally
   steal confidence from a similar existing intent (if two intents'
   phrasings overlap too much, add more distinguishing examples to each).

## Custom bot instructions — a different kind of "training"

The Training tab's second panel, **Custom bot instructions**, is
unrelated to the classifier above — it's a per-bot-instance persona/
system-prompt field (`bot_instances.custom_instructions`), prepended to
every prompt that specific instance routes through `router.ask()`
(`bot/router.py`), regardless of which backend it uses. Unlike the
Support Bot's phrase training, this doesn't affect intent recognition at
all — it shapes how a regular bot instance (Telegram/Discord/Slack)
actually answers. Leave it blank for default, unmodified behavior.

## Where the confirm token lives

`SupportBot._pending: dict[str, tuple[intent, args_json, actor, expires_at]]`
— a plain in-memory dict on the `SupportBot` singleton, `CONFIRM_TTL_S =
300`. It is not written to the database and does not survive a server
restart — a pending confirmation from before a restart simply expires
silently rather than resurrecting into an unexpected action.
