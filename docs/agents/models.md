# Model knowledge and free-tier allowances

ABP knows what each model can do and how much of its allowance is left, and uses that to avoid
calls that would only be refused. Everything here is read-only to the model apart from your own
settings, and nothing is enforced on a limit nobody knows.

## What is known about a model

`bot/model_catalog.py`, one lookup per `provider/model`:

| Group | Fields |
| --- | --- |
| Identity | name, family, release date, last updated, status |
| Size | context window, maximum output tokens |
| Abilities | tool calling, reasoning (and its options), structured output, images / audio / video / PDF in, attachments, open weights |
| Knowledge | training-data cutoff |
| Price | input, output, cache read and cache write, dollars per million tokens; free or not |
| Limits | requests and tokens per minute and per day, simultaneous calls, when the day resets, scope (one model or a whole provider's free tier), and where and when each figure was checked |

Every group records **where it came from**: `override` (you), `curated` (published limits in
`config/model_limits.yaml`), `catalog` ([models.dev](https://models.dev), downloaded and cached for
a day by `model_pricing.py`) or `builtin` (a small family table for context windows). A missing
figure is `None` and means "not published", never "unlimited".

### Limits: what is and is not known

Providers change these numbers without notice, so ABP treats the table as a starting point:

* `config/model_limits.yaml` holds figures read from each provider's own documentation, with the
  URL and the date checked. As of 2026-09-20 that is **OpenRouter free models** (20 requests a
  minute, 50 a day, or 1000 a day once $10 of credit has ever been bought - set `rpd: 1000` if that is
  you), **Groq's free plan** for the models its page lists, and **Google Gemini**, whose limits are per
  project and published only in AI Studio, so only the reset time (midnight Pacific) is recorded. Those
  figures were read by an assistant summarising the pages, not copied by hand - check them before you
  depend on them.
* What a provider says in its response headers (`x-ratelimit-*`, `anthropic-ratelimit-*`,
  `retry-after`) is remembered and used while it is fresh. Note that some providers send them only
  with an error (OpenRouter) and some are per minute where others are per day (Groq's request figures
  are daily) - ABP records what was sent without assuming which.
* Your own figures replace both: `native_agent.models.limits` in `config/backends.yaml`, keyed by
  `provider/model` or a pattern (`openrouter/*:free`), or `PUT /api/models/limits`.

## What is counted, and what happens at a limit

Every model call from every transport (Anthropic, OpenAI-compatible, Responses; the main agent, its
sub-agents, swarms and the Support Bot's fallback) is counted in a small SQLite file under the agent
state directory, with its tokens and outcome. Counts survive restarts and are kept 45 days.

Before a call:

* a **per-minute** limit that would be crossed makes the call wait, up to `max_wait_s` (15 s);
* a **daily** limit that is used up, a longer wait, a provider-reported "none left", a full
  concurrency cap, or a recent 429 makes the call fail at once with a plain message - "openrouter/x
  has used 50 of its 50 requests/day; it frees up at 2026-09-21 00:00 EDT (in 5h12m)". A bot with a
  fallback model configured then answers from the fallback, without a wasted request;
* after a 429, that model is not called again until the time the provider gave (30 s if it gave none).

`native_agent.models.enforce: false` keeps the counting and reporting but never holds a call back.
Reset times are shown in `native_agent.models.timezone` (blank = this computer's zone).

## Automatic model routing — never Claude by default

`bot/model_router.py` (roadmap P8) classifies a task (trivial, coding, hard reasoning, long
context, vision, bulk) and ranks candidate models by quality, economy (free counts highest) and
headroom (how much of a model's allowance is left right now). It's the mechanism behind a bot whose
model is left blank or set to `auto`:

* **Set it up**: a bot on the **ABP Agent** backend with model **left blank, or set to `auto`**
  (the Add-bot form has an **Auto** button next to the model field for exactly this) gets a real
  model picked for it — the router's top-ranked pick, the first time that bot is actually used.
  That pick then stays in place for the rest of that bot's life, the same as if you had typed a
  specific model yourself; it isn't reclassified on every later message.
* **Never Claude by default.** `default_backend` and the `quick_question`/`project_task` action
  overrides in `config/backends.yaml` all point at `native_agent` with `model: null` (auto) — the
  operator's standing instruction is that Claude is never used unless a person explicitly chooses
  it. The router's automatic candidate list (when `native_agent.router.candidates` is empty) is
  every free model ABP's catalog knows of, **except Anthropic's** — an Anthropic model only ever
  becomes a candidate if you list it yourself under `native_agent.router.candidates` or `.also`.
  If nothing is configured and no free provider exists yet, a bot on `auto` fails with a clear,
  actionable error rather than silently falling back to Claude or hanging.
* **Auto-failover** (`native_agent.router.auto_failover`, off by default): when a bot's active
  model fails mid-turn, the existing one-hop retry against its own configured `fallback_model` (see
  the ABP Agent settings panel) is tried first, then — only if this is on — up to
  `native_agent.router.max_failover_hops` more router-ranked picks, each a provider/model not
  already tried this turn. Still bounded: if every hop fails, the *last* hop's own error is what
  you see, not a swallowed retry loop.
* **See it**: the ABP Agents page's Models tab has a **What ABP would pick right now** panel
  (`GET /api/agent/router/recommend`) showing the live ranking with quality/economy/headroom, and
  the `router.*` settings (`enabled`, `candidates`, `also`, `auto_failover`, `max_failover_hops`)
  right there as real form fields, not YAML-only. `/route <task>` and the `suggest_model` tool still
  work exactly as before for a plain recommendation without applying it.

Free models that are used up right now are also passed over when ABP picks a free model to run a
swarm on.

## Asking

* **The agent** has `model_info` (its own model by default, or any `provider/model`) and `find_models`
  (free only, minimum context, needs tools / vision / reasoning; used-up models sort last). Its prompt
  carries one stable line about its own model and any limit, so it does not plan a 200-call fan-out on
  a 20-per-minute model. Read-only sub-agents may use both.
* **Chat**: `/modelinfo` (this bot's model), `/modelinfo provider/model`, `/modelinfo usage` (last 24 hours),
  `/modelinfo refresh` (download the catalog again). Alias `/limits`.
* **Dashboard API**: `GET /api/models/info`, `/api/models/usage`, `/api/models/find`,
  `POST /api/models/refresh`, `PUT` / `DELETE /api/models/limits` (the last three need the dashboard token).
* **MCP**: `get_model_info`, `get_model_usage`, `find_models`.

## Context windows

`context_window.window_for()` uses, in order: `native_agent.context_windows` (yours), the catalog, the
built-in table, then 128 000. For Claude models the smaller of the catalog and the table wins, because a
larger Claude window is a separate opt-in and over-estimating it makes the provider refuse the call.

## Not done

* No dashboard or desktop screen yet; the API is what one would call.
* Tokens per minute are enforced from ABP's own estimate before a call and its counts after it; a
  provider that counts differently can still refuse a call ABP thought fine.
* Limits that depend on a plan or on your account's balance cannot be discovered; set them yourself.
* The catalog has no rate limits and only some providers publish them, so most models show no limit.
