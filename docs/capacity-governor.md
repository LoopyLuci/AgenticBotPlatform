# Capacity governor (LF3, phase 1)

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

## Handoff packs

`bot/capacity/handoff.py`: one JSON file per task in `data/agent/capacity/handoff/` with the goal, plan, files touched, diff so
far (capped), test status, notes and last error. `write`, `update`, `read`, `list_packs`, `delete`; `resume_prompt(pack)` renders
the opening message for the model taking over; `attach_to_board(board, pack, agent, fence)` stores a copy as the work-board
task's `result` (`{"handoff_pack": ...}`, via `set_state(..., "running")`, since the board client has no attachment call) with a
note, and `from_board_task(task)` reads it back. Phase 1 provides the helpers; writing packs automatically as a task runs is
the next step.

## Surfaces

* Config: the `capacity:` section of `config/backends.yaml` (defaults are safe: `mode: auto`).
* CLI: `abp capacity status`, `abp capacity set <cloud-led|hybrid|local-only|auto> [--for 2h] [--reason ...]`,
  `abp capacity clear`, `abp capacity history`.
* REST: `GET /api/capacity/state`, `/history`, `/route?task=`, `/handoff`, `/handoff/{task_id}`; `POST /api/capacity/override`,
  `/clear`, `/report` (the POSTs need the dashboard token).
* MCP: `capacity_status`, `capacity_history`, `capacity_set`, `capacity_clear`, `capacity_report`, `capacity_handoff`.
* Dashboard: a Capacity card at the top of the Coordination page (state, why, pin / back to automatic).

## Not in phase 1

The takeover drill (cutting API access in the middle of a multi-task run), an active recovery probe (today recovery is the reset
time, or a working call during hybrid probation), the LF5 digest when cloud capacity returns, and the confined dev swarm and
the conductor's own queue reading the state. The hooks are marked `TODO(LF3 phase 2)` in `bot/capacity/governor.py`.
