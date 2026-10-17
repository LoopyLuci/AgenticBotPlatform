# The manager channel (LF5)

Claude supervises the local factory cheaply: a capped digest, one escalation queue, and optional check-ins that run only while cloud
capacity exists. Code: `bot/manager`. It only reads (work board, conductor records, capacity governor, agent traces); deciding stays
with `abp conductor approve|unblock` and `abp capacity`.

## Escalation queue
`abp manager escalations`, `GET /api/manager/escalations`, MCP `manager_escalations`. One list over:

| kind | source |
|---|---|
| `rejections`, `blocked` (conductor kinds) | the conductor's open escalations (two review rejections, a blocked task) |
| `blocked`, `review` | other work-board tasks in those states |
| `repeated_failure` | work-board tasks that failed 2 or more times and are not already escalated |
| `push_approval` | push-queue entries awaiting approval |
| `capacity_override` | a manual capacity override in effect |

Each item has only what is needed to decide: `question`, `failing_check`, `where` (file:line), `diff_excerpt` (at most 1500
characters), `age_s`, and a `decide` hint. Never raw logs or full diffs.

## Digest
`abp manager digest [--since <epoch|2h|1d|'YYYY-MM-DD HH:MM'>] [--peek] [--json]`, `GET /api/manager/digest?since=&mark=&text=`, MCP
`manager_digest`. Contents: capacity state and why, board counts, what finished and what failed since the last digest (with the
reason), integrations, capacity events, the escalations above, factory and manager token accounting.

- **Cap:** `manager.digest.cap_tokens` (default 3000, about 4 characters a token) is a hard ceiling. Over it, the digest drops diff
  excerpts, then shortens every list, then clips strings, and lists what it cut in `truncated`. Counts always survive.
- **Marker:** the time of the last delivered digest. `since` defaults to it. The CLI and MCP move it by default (`--peek` / `mark=false`
  to look without consuming); the REST GET moves it only with `mark=1`.
- **Cache:** the same request inside `manager.digest.cache_s` (30 s) returns the cached document (`"cached": true`).
- Machine-readable JSON, plus a short text form (about 3500 characters at most).

## Check-ins
Off by default (`manager.checkins.enabled: false`). When on, a periodic task (`bot/main.py`, like the capacity probe) builds a digest
every `interval_s`, marks it, keeps the text as `latest` (`abp manager checkin`) and publishes `manager.checkin` on the work board feed.
The capacity governor decides: `cloud-led` and `hybrid` run; `local-only` builds nothing and queues the check-in, which runs on the
first poll after capacity returns. A check-in with nothing new is skipped and costs no tokens. `abp manager checkin --now [--force]`
runs one by hand (still gated).

## Token accounting
Each digest carries its own estimated size. Delivered digests (marked, or checked in) add to a running manager total, shown per
check-in and cumulatively, next to the factory's own work since the last digest: LLM calls and tokens from the agent traces, with the
local-model share split out. State: `data/agent/manager/state.json`.

## Config
```yaml
manager:
  digest:   {cap_tokens: 3000, cache_s: 30, diff_chars: 400}
  checkins: {enabled: false, interval_s: 3600}
```
