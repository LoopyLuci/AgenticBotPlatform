# Capacity governor (LF3, phases 1 and 2)

One system-wide switch for "is there cloud capacity right now?". Code: `bot/capacity/`. When a cloud provider says it is out
of allowance, new work moves to local models (ModelMistress first, Ollama after) without anyone touching a setting; when
the allowance comes back, work returns to cloud models. Work that is already running is never interrupted.

## States

| State | Meaning |
|---|---|
| `cloud-led` | Normal. Nothing changes. |
| `hybrid` | Cloud models only for the hard task classes (`capacity.hybrid.cloud_classes`, default `hard_reasoning`, `long_context`); everything else goes to local models. If no local model fits a task, the cloud list is kept: hybrid prefers local, it never leaves a task without a model. |
| `local-only` | Local models only. If the configured candidate list has none, the local models ABP can see are used. If there are none at all, nothing is offered and the turn fails with the usual "no model available" message; it never falls back to a cloud model. |

The effective state is the owner's override while it has not expired, else the config pin (`capacity.mode`, when not `auto`),
else the automatic state. The state and its history (who or what changed it, when, why) are kept in
`data/agent/capacity/capacity.db`, so they survive a restart.

## What moves the automatic state

Every failure of a cloud provider is classified (`bot/capacity/classify.py`, a pure function over status, body or message
and headers):

* **usage cap** - a quota, credit balance, spend limit or plan window that is used up: OpenAI `insufficient_quota`,
  Anthropic "credit balance is too low" / "reached your specified API usage limits", OpenRouter 402 and its free-models-per-day
  429, a daily request limit, a rejected overage, `anthropic-ratelimit-unified-status: rejected`, Claude Code's
  "usage limit reached|<epoch>" and "5-hour / weekly limit reached - resets 3pm (zone)". Goes to `local-only` until the reset
  time the message gives. Without one: `cap_hold_s` (an hour, doubling on a repeat, at most `max_hold_s`), then `hybrid` for
  `probation_s`; a cloud call that works in that time returns to `cloud-led`, another cap goes back to `local-only`.
* **short rate limit** - HTTP 429 per-minute throttles ("try again in 6s", `Retry-After`). Stay as is; `rate_limit_threshold`
  of them inside `rate_limit_window_s` turn `hybrid` on for `hybrid_hold_s`. A "retry after" longer than `long_retry_s` counts
  as a usage cap that resets then.
* Not capacity: overloaded (529), 5xx, auth errors, bad requests. A local server's own 429 is ignored.

Where ABP sees these failures (both are one-line calls that never raise):

* `bot/agent_runtime/usage_limits.guarded()` - every API transport call (OpenAI-compatible, Anthropic, Responses): status,
  message and rate-limit headers; a success there ends a probation.
* `bot/router.py` - the text of a failed CLI or external backend (Claude Code's limit line), unless an API transport already
  reported the same failure.

A manual report works too: `POST /api/capacity/report` or the MCP tool `capacity_report` (for an agent that sees a limit line
somewhere ABP does not).

## Routing

`bot/model_router.recommend()` filters its candidates through `bot/capacity/routing.apply()`, so everything that uses the
router follows the state: bots on `auto`, failover hops (while the state is not `cloud-led`, a failed cloud call always gets the
router's local alternatives), re-routing of a sticky cloud pick (`reroute_reason`), and swarm members. A bot with a **fixed**
cloud model runs each new turn on a local model the router picks while the state asks for it, and gets its own model back
afterwards (`capacity.reroute_fixed_models: false` turns that off). Claude is never added: the local list is built from local
providers (`capacity.local_providers` or a loopback base URL) only.

## Recovery probe (phase 2)

`bot/capacity/probe.py`. Without it, recovery waits for the provider's stated reset time or for a real call during hybrid
probation. While the automatic state is `local-only` or `hybrid` after a cap, the app asks the capped provider the smallest
possible question now and then: one user message ("ping"), no tools, no system prompt, `max_tokens=1`, through the provider's own
transport. Settings (`capacity.probe`):

| Setting | Default | Meaning |
|---|---|---|
| `enabled` | true | switch the probe off |
| `interval_s` | 300 | the first probe is one interval after the cap; later ones this far apart |
| `backoff`, `max_interval_s` | 2, 3600 | the interval doubles after each failed probe, up to the maximum |
| `max_per_hour` | 6 | never more than this many probe rounds per hour (kept in the database, so a restart does not reset it) |
| `reset_fraction` | 0.5 | when the provider stated a reset time, nothing is sent before this share of the hold has passed |
| `targets` | [] | `provider/model` to probe instead of the ones that were capped (needed for a cap seen only as CLI text) |
| `timeout_s` | 20 | per request |

A probe that works moves `local-only` to hybrid probation, and a working probe (or real call) in hybrid returns to `cloud-led`:
the same two steps phase 1 defines. A probe answered with a limit backs off, waits for a `Retry-After` or reset time the answer
gives, and extends the hold when the answer names a later reset. A failed probe is not reported to the governor a second time
(no extra strike). Work that is running is never touched; the state only decides where new work goes. A cap that was seen only as
CLI text (Claude Code's limit line) names no API provider, so it is reported as "unprobeable" and recovery waits for the reset
time. The probe loop is started with the app; `POST /api/capacity/probe`, `abp capacity probe` and the Probe now button on the
Capacity card send one now (the hourly limit still holds).

## Handoff packs

`bot/capacity/handoff.py`: one JSON file per task in `data/agent/capacity/handoff/` with the goal, plan, files touched, diff so
far (capped), test status, notes, last error, the model that wrote it, its status (`running`, `finished`, `failed`,
`interrupted`) and the capacity state when it was written. `write`, `update`, `read`, `list_packs`, `delete`;
`resume_prompt(pack)` renders the opening message for the model taking over; `attach_to_board(board, pack, agent, fence)` stores a
copy as the work-board task's `result` (`{"handoff_pack": ...}`) with a note, and `from_board_task(task)` reads it back.

### Written automatically (phase 2)

`bot/capacity/autopack.py`. A native-agent turn that belongs to a task - it runs inside a work-board task (every swarm step and
board-fed worker) or its caller passes `context["handoff_task_id"]` - writes and updates its pack itself. Ordinary chat turns have
no task and cost nothing. After each tool round it records the files touched (write/edit/patch tools), the test status (`run_tests`,
or a test command run through `run_shell`) and the last error; writes are throttled (`capacity.handoff.min_interval_s`, 20 s)
except right after a test run or an error, and always at the end (status `finished`, `failed`, or `interrupted` when the turn
was cancelled). When the task's workspace is a git worktree of its own, the pack also gets `git status` files and the diff so far;
above `capacity.handoff.max_diff_chars` (12000) only the `--stat` summary is kept. A copy goes to the board task at most once a
minute. Packs are small by construction: text, lists and diffs are capped (`handoff.py`).

When a turn starts on a task whose pack was written by a different model and is not finished - a re-route, capacity moving, a
restart on another model - the resume prompt from the pack is put in front of the task's own prompt, and a note records the
hand-over. A finished pack is not resumed; a turn started inside another turn (a delegate, a sub-agent) never overwrites its
parent's pack. `capacity.handoff.auto: false` turns all of this off.

## Work queues follow the state (phase 2)

`bot/capacity/follow.py`. Board tasks may carry a capacity hint as a `needs` entry, so it works on ABP's local board and
AgentSentinel's: no hint = any model (the router decides from the state), `capacity:local` = routine work, safe on a local
model, `capacity:cloud` = wants a cloud model. A worker advertises `follow.worker_capabilities(base)`: always `capacity:local`,
and `capacity:cloud` unless the state is `local-only`. So in local-only a cloud-marked task stays open (never failed, dropped or
run on a local model by surprise), everything else is picked up and run on local models, and when capacity returns the next ask
for work sees the waiting task. Nothing running is interrupted: the state is read when a worker asks for work.

* **ABP swarms** (`bot/swarm/board_run.py`): workers re-read the capabilities on every ask; `Step(capacity="cloud"|"local")` sets
  the hint; a step that fails because its provider ran out is handed back to the board (at most `MAX_REQUEUES` = 3 times) and
  runs again, resuming from its pack, on whatever the state allows. Other failures fail the step as before.
* **The conductor's queue and the confined dev swarm** (they pull from the work board and live outside this repository): call
  `GET /api/capacity/policy` / `abp capacity policy` / MCP `capacity_policy` for the capabilities to advertise and the local
  models to use, put `capacity:cloud` / `capacity:local` in the `needs` of the tasks they create, and read a task's own pack with
  `capacity_handoff` when picking it up on another model.
* `follow.placement(task)` says whether a task should run on a local or a cloud model right now.

## Change feed (the LF5 hook)

`bot/capacity/events.py`: every committed state change and probe outcome is published as `capacity.changed`,
`capacity.recovered` (back to cloud-led) or `capacity.probe`, in a ring (`GET /api/capacity/events?since=`), to subscribers
(`events.subscribe`), and on the local work board's event feed next to the `board.*` events. `events.digest_lines(since)` renders
them as short lines for a digest. The durable record is the history table.

## Surfaces

* Config: the `capacity:` section of `config/backends.yaml` (defaults are safe: `mode: auto`).
* CLI: `abp capacity status`, `abp capacity set <cloud-led|hybrid|local-only|auto> [--for 2h] [--reason ...]`,
  `abp capacity clear`, `abp capacity history`, `abp capacity probe`, `abp capacity policy`, `abp capacity packs`.
* REST: `GET /api/capacity/state`, `/history`, `/route?task=`, `/handoff`, `/handoff/{task_id}`; `POST /api/capacity/override`,
  `/clear`, `/report`, `/probe` (the POSTs need the dashboard token); `GET /probe`, `/policy`, `/events?since=`.
* MCP: `capacity_status`, `capacity_history`, `capacity_set`, `capacity_clear`, `capacity_report`, `capacity_handoff`,
  `capacity_probe`, `capacity_policy`.
* Dashboard: a Capacity card at the top of the Coordination page (state, why, pin / back to automatic, the probe's last answer
  and next time with a Probe now button, the newest handoff packs).

## Still open

* The takeover drill against a real cut-off (the automated drill, `tests/test_capacity_phase2.py`, uses fakes only).
* The LF5 digest itself (the feed above is what it reads), and the dev swarm / conductor reading the policy: they are outside this
  repository and need the small client change described under "Work queues follow the state".
* A probe for a cap seen only as CLI text (Claude Code's plan limit) - it names no API key to ask with.
