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
