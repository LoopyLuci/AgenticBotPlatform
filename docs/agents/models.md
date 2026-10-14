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
context, vision, bulk) and ranks candidate models by quality, economy (free counts highest),
headroom (how much of a model's allowance is left right now), and the reliability and speed it has
learned. It's the mechanism behind a bot whose model is left blank or set to `auto`. It learns from
every call and from your feedback, records every decision with its reasoning, and follows an
editable, versioned policy. See [router.md](router.md) and the dashboard's **Model Router** page.

* **Set it up**: a bot on the **ABP Agent** backend with model **left blank, or set to `auto`**
  (the Add-bot form has an **Auto** button next to the model field for exactly this) gets a real
  model picked for it — the router's pick, the first time that bot is actually used. It keeps that
  model on later turns, and chooses again when the model starts failing (it is resting after a 429,
  403 or 404) or is blocked by a rule. When its model fails mid-turn it tries the next pick and keeps
  the one that works.
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
* **Dashboard API**: `GET /api/models/info`, `/api/models/usage`, `/api/models/usage-limits`, `/api/models/find`,
  `POST /api/models/refresh`, `PUT` / `DELETE /api/models/limits` (the last three need the dashboard token).
* **MCP**: `get_model_info`, `get_model_usage`, `find_models`.
* **Screen**: **Usage & limits** in the dashboard and the desktop app (one `usage-panel.js`, fed by
  `GET /api/models/usage-limits`). A table per model for the last 1 or 7 days (calls, tokens, 429s, errors, last used),
  a bar for each known limit window (requests and tokens per minute and per day) with what is left and when it frees
  up, a "blocked until" badge, an empty state, and a refresh every 30 seconds while the screen is open. Times are in
  the viewer's local time zone. Read-only; limits are still edited with `PUT /api/models/limits` or `native_agent.models.limits`.

## Context windows

`context_window.window_for()` uses, in order: `native_agent.context_windows` (yours), the catalog, the
built-in table, then 128 000. For Claude models the smaller of the catalog and the table wins, because a
larger Claude window is a separate opt-in and over-estimating it makes the provider refuse the call.

A model served by ModelMistress (`modelmistress/<model>`) is different: its window is the one it was **loaded** with,
read from the hub, never the family table. In order: the hub's loaded-model list (`model.loaded`), an `n_ctx` /
`ctx_size` field on its `/v1/models`, llama-server's `/props` through the hub; then `native_agent.context_windows`;
then 8 192. A configured figure above what the hub reports is ignored (the smaller wins). The answer is cached for 30
seconds (10 for "the hub did not say"). The `context_length` on `/v1/models` is the model's trained maximum and is not
used. Other local engines (Ollama, LM Studio, vLLM...) get 8 192 instead of 128 000 when nothing knows the model.

## Small-model mode

A model with a small window does worse with a long prompt and dozens of tool definitions. `native_agent.small_model`
(`bot/agent_runtime/small_model.py`, YAML only) trims what is **sent** to such a model; nothing is removed from ABP.

| setting | default | meaning |
|---|---|---|
| `enabled` | `auto` | `auto`: on for a local engine (ModelMistress, Ollama, LM Studio, vLLM...) whose window is at most `max_window`; `true` / `false` force it for every model |
| `max_window` | 32768 | the largest window `auto` treats as small |
| `tools` | `[]` | the tools to send; empty means the core set: read/list/grep/glob/edit/write (the groups in `agent_defs.py`), `run_shell`, `shell_output`, and a test/verify tool when one exists |
| `compact_prompt` | `true` | skip the optional prompt sections (skills, skill packs, the sub-agent list, the profile, the code-navigation bullet) |
| `keep_tool_results` / `compact_at` | 3 / 0.6 | replace `context.keep_tool_results` / `context.compact_at` while the mode is on |

`auto` also counts a transport whose base URL points at this machine (`localhost`, `127.x`, `::1`) as local, so a bare
`--base-url http://127.0.0.1:...` OpenAI-compatible endpoint turns it on; a remote base URL, or a private LAN address,
does not (a router there may forward to a paid provider). Cloud providers are classified as before.

The other tools still exist. An agent that lists its tools (`allowed_tools`) is sent the intersection of that list and the
core set, plus the listed edit/write tools, `run_tests` / `verify` and `spawn_subagent` even when the configured `tools` omit
them, so it can still finish; if the intersection would be empty, the whole list is sent. Cloud models are never
affected unless you force `enabled: true`. A model with no `tool_call_modes` entry is switched to `text` only when its
transport says it cannot do native tool calls (`supports_native_tools = False`); otherwise it stays `native`.
The decision is shown once per session in the run's trace ("small-model mode on for ...").

## Not done

* Tokens per minute are enforced from ABP's own estimate before a call and its counts after it; a
  provider that counts differently can still refuse a call ABP thought fine.
* Limits that depend on a plan or on your account's balance cannot be discovered; set them yourself.
* The catalog has no rate limits and only some providers publish them, so most models show no limit.

## A money limit for one turn

`native_agent.budget` (Agents page, Runtime tab, "Limits") stops a turn once its model calls have cost
`max_usd_per_turn` dollars (0 = off). It ends the turn the way the token limit does: the agent summarises what is
done, and "continue" picks up. Sub-agents started during the turn spend from the same budget.

Nothing is looked up while a turn runs. A model's price comes from, in order: your `budget.prices` table
(`"provider/model"` or a pattern, dollars per million tokens: `input`, `output`, optional `cache_read` and
`cache_write`, or `free: true`); the model running on this computer or your own network (ModelMistress, Ollama,
LocalAI, LM Studio, vLLM, llama.cpp — by provider, not by address: a proxy on your network may forward to a paid API, so mark it `free: true` in `prices` if it is free), which costs 0; then the catalog above (including
`native_agent.models.overrides` `price_input` / `price_output`). `unknown_price: allow` (default) carries on with a
one-time note in the run's trace; `stop` ends the turn after a call to a model with no known price. Cached tokens are
charged at the cache price when one is known, else at the input price. The cost of each call and run is in the trace
(`usd`) whenever the price is known.

## Tool calls from local models

Local models often write the call as text instead of using the API's `tool_calls`. For a reply with **no native
call**, ABP looks for one (`bot/agent_runtime/text_toolcalls.py`, used by the OpenAI-compatible transport):

* `<tool_call>{"name": ..., "arguments": {...}}</tool_call>` (Qwen, Hermes), also with `<function=name>` and
  `<parameter=key>value</parameter>` inside; a bare `<function=name>{json}</function>`;
* a bare JSON object or array (`name` + `arguments` / `parameters` / `args` / `input`, the OpenAI `function` wrapper,
  a `tool_calls` list, Mistral's `[TOOL_CALLS]`);
* a fenced ```` ```json ```` block of the same.

Near-miss JSON is repaired where the fix is unambiguous (trailing commas, single quotes, raw newlines in strings,
`True`/`None`, unquoted keys, `\d`-style escapes), and arguments get light coercion (`"5"` becomes 5). A call is run
only if its name is a tool offered this turn and its arguments fit that tool's schema. If any call in the text is
unusable, none runs; the model is told why once, in a short message, and if the second try is also unusable the
reply is returned as it is (no loop). The call text is removed from the visible reply; the stored assistant turn
carries a normal `tool_calls` entry. Native calls are never touched.

`native_agent.tool_call_modes` picks how a model is asked to call tools (keys: `provider/model`, a model name, a
name prefix, or `*`; the longest match wins):

| mode | what is sent |
|---|---|
| `native` (default) | the API's `tools` field |
| `text` | no `tools` field; the tools and the `<tool_call>` format are described in the system prompt |
| `grammar` | as `text`, plus a JSON-schema `response_format` (one tool call, or `final_answer`) that llama-server turns into a grammar; if the server answers with a client error, ABP stops sending it to that server and carries on as `text`. The reply is taken whole, not streamed |

Counts per model (native / parsed from text / repaired / failed) are in `tool_calls` on each row of the router's
model board (`GET /api/router/models`'s rows); they are in-process and restart with ABP.
