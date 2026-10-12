# Coordination: one work board for ABP's own multi-agent work

Sub-agents, swarms and the agents of other programs on the same machine used to work without knowing about each other.
That allowed two children to edit one file at once, the same spawn to run twice after a retry, workers to sit idle
while steps waited, and nobody to be able to say who was doing what. `bot/coordination/` puts all of it on one board.

## The model

A **task** has a board name, an idempotency `key`, a title, `deps` (task ids that must be `done` first), `needs`
(capabilities the worker must have), a `scope` of resources it locks while it runs, a priority, a lease and a fence.
A **lock** is a resource string held exclusively or shared by an agent, with a lease. **Presence** says which agents are
idle or busy and when they were last heard from (gone after three 15-second intervals).

Guarantees (the same on both backends):

- Every change is one transaction; claiming a task and taking locks is all-or-nothing.
- **Leases.** A claimed task and its locks expire unless the owner heartbeats (every lease/3). An expired task goes back
  to `open` with `attempts + 1`, or to `failed` ("lease expired N times") once `max_attempts` is reached. The check runs
  lazily on every read and claim.
- **Fencing.** Every claim gets a number from a board-wide counter. State changes and heartbeats must carry it; a stale
  one (the task was taken over after an expiry) is refused `STALE_FENCE`, so a slow agent can never overwrite the new
  owner's work. A non-owner gets `NOT_OWNER`.
- **No duplicates.** Creating a task whose `key` matches an unfinished task on the same board returns that task
  (`deduped`). `dedupe_done` also matches a finished one and returns its result.
- **No idle workers.** `next` claims the best runnable task for the caller in one step: open, all deps done, needs
  satisfied, all scope resources lockable; highest priority first, then oldest.
- **Resources.** `repo:`, `branch:`, `worktree:`, `path:<repo>:<relative>`, `model:`, `port:`, `custom:`. Two `path:`
  resources of one repo conflict when one equals or is a folder prefix of the other (`path:abp:bot` and
  `path:abp:bot/db.py` conflict; `path:abp:bot/db` and `path:abp:bot/db.py` do not). Anything else conflicts only when
  equal. Shared locks coexist; exclusive conflicts with anything overlapping. A refusal is `LOCKED` and names the holders
  (agent, task id, task title, lease end).

## Two boards, one interface

`bot.coordination.get_board()` returns a `Board` (`base.py`). Both implementations have the same methods, shapes and
error codes (`BoardError.code`):

| | |
|---|---|
| `LocalBoard` (`local.py`) | ABP's own SQLite file (`data/coordination/board.db`, or `ABP_COORDINATION_DB`), `BEGIN IMMEDIATE` transactions, a connection per thread. Needs nothing else, so ABP stays standalone. |
| `SentinelBoard` (`sentinel.py`) | AgentSentinel's `/api/v1/board/*` over HTTP (protocol v1). Every agent on the machine then shares one board. |

`coordination.backend` in `config/backends.yaml` (or the `ABP_COORDINATION_BACKEND` environment variable):

- `auto` (default): AgentSentinel's board when its API answers (probed with a 1.5 s timeout, the answer cached for 15 s),
  otherwise the local one.
- `sentinel`: always AgentSentinel; calls fail with `UNAVAILABLE` if it is down.
- `local`: always ABP's own.

AgentSentinel's address is `coordination.sentinel_url` or `AGENT_SENTINEL_URL` (default `http://127.0.0.1:8080`); the
token is read from `AGENT_SENTINEL_TOKEN` only (never from a config file) and sent as `Authorization: Bearer`.
`coordination.enabled: false` switches off the sub-agent tasks and edit locks; swarm runs always use a board.

## `WorkSession`

`async with await WorkSession.claim(board, task_id, agent)` (or `WorkSession.next(board, name, agent, capabilities=...)`)
runs one task's lifetime: it heartbeats the task and its locks every lease/3, marks the agent busy and idle, and always
ends the task: **done** at the end of the block (with `work.result`), **failed** after `work.fail(note)`, **released**
when the block raises, is cancelled or the process shuts down. If a heartbeat is refused `STALE_FENCE` / `NOT_OWNER`
the work is cancelled at once, nothing is written back and the block ends with `WorkLost`: its output must be dropped.
Inside the block `work.acquire([...])` takes more shared things for the task; they are freed with it.

## What is wired

- **Sub-agents** (`subagents.py`, `subagent_board.py`). Each child of `spawn_subagent` and
  `dispatch_native_swarm_goal` is a task on board `abp/<parent session>`, key = hash(parent turn, agent definition,
  prompt, output schema), run inside a `WorkSession` as agent `sub:<dispatch>:<index>`. A repeated spawn in the same turn
  gets the stored result (turn = the agent trace run, or `subagent_board.turn_var`; without a known turn only unfinished
  children are matched); a child someone else is running is waited for; a child whose runner vanished is picked up when
  its lease expires. `list_subagents` shows each child's board state. If the board cannot be reached the child just runs.
- **File edits** (`fileguard.py`, called from `tools.execute_tool`). `write_file`, `edit_file`, `multi_edit` and
  `apply_patch` take `path:<workspace id>:<relative path>` exclusively for the running task (lease
  `coordination.file_lock_lease_s`, renewed with the task, freed when it ends). When another agent holds the file the
  tool raises an error naming the holder and its task and writes nothing. Reads are not locked. The main agent, with no
  session and no live children, takes no locks at all; with live children it locks only for the one write.
- **Swarms** (`bot/swarm/board_run.py`, `strategies.py`). Every step of FanoutSynthesize, LeaderVote, SequentialRelay,
  DecomposeDelegate and Custom is a task on `abp/swarm-<id>`: `deps` carry the order, `needs` carry `instance:<id>` and
  `run:<run id>`, and workers pull with `next`. Results are recorded on the tasks. The same swarm and prompt dispatched
  twice at once share their steps (key = hash(swarm, prompt, step)); a step whose worker dies is requeued. A relay link
  that fails leaves the later links cancelled.

- **Hermes-external swarms** (`hermes_board.py`, called by `POST /api/hermes/{id}/dispatch`, which is what the MCP tool
  `dispatch_swarm_goal` uses). The children of a Hermes run belong to Hermes, so ABP's task is the dispatch itself: board
  `abp/hermes-<instance id>`, key = hash(instance, goal, worker provider, worker model, max children), run inside a
  `WorkSession` (lease + heartbeat) for as long as Hermes works, agent `hermes-dispatch:<task>`. The answer and Hermes's
  per-child breakdown are stored as the task's result; an error is stored as the failure note and re-raised. An identical
  dispatch while one is running waits for it and returns its stored answer (`deduped: true` in the response, Hermes is not
  called again); a finished one is reused for 10 minutes. If ABP dies mid-run the lease expires: the task goes back to
  `open` and the next identical dispatch picks it up (the retry), or after 2 expiries it is `failed` ("lease expired 2
  times") and shows so on the Coordination screen; a repeat of a failed one is a new task. A dispatch whose lease was taken
  over is refused (409) and its output dropped. Board off or unreachable: the call just runs.
- **Kanban mirror** (`kanban.work_board_mirror`, `GET /api/kanban/work-board[?board=]`, `/kanban workboard [board]`,
  `abp kanban workboard`, the Kanban screens' "Agents' work board" panel / "Work board" action). One kanban board per work
  board name, one column per task state, one card per task (title, owner, attempts, lease, note; never detail or result).
  Cards carry `source: "work-board"` and `read_only: true` and sit in their own panel, apart from hand-made cards; there is
  no write route, and nothing in kanban writes to the board.
- **Stuck-work check** (`watch.py`, started with ABP's other background jobs in `bot/main.py`). Every
  `coordination.stuck_watch_interval_s` (60) it reads the board summary and raises a warning - a line in the Sentinel journal
  (the activity log) and the Sentinel's push to paired phones - for a lease about to run out without a renewal, a task that
  failed after all its attempts (once per task), runnable tasks waiting for over a minute while agents are idle, and locks
  held by an agent that is gone. A condition is reported at most once per `coordination.stuck_alert_every_s` (1800).
  `coordination.stuck_watch: false` turns it off. It only reads.

## Not wired

- Notebook edits (ABP has no tool that edits notebooks; `edit_file` on an `.ipynb` is covered like any file).
- Shell commands with a declared write scope (no such declaration exists yet).
- The individual children of a Hermes run: they are Hermes's own and invisible to ABP; only the dispatch is a task (their breakdown is stored on it afterwards).
- A periodic sweeper in LocalBoard: expiry is applied on every call instead, which is enough for one process.

## Seeing it

`GET /api/coordination/summary` (per board: counts by state, who holds what, leases about to expire, idle agents,
blocked tasks and why), `/tasks` and `/locks`; the **Coordination** screen in the dashboard and the desktop app;
`python -m abp_cli coordination summary|tasks|locks`.

## Tests

`tests/test_coordination_board.py` runs one conformance suite against `LocalBoard` and against `SentinelBoard` talking to
a fake AgentSentinel server (`tests/coordination_fakes.py`, written from the protocol, not the real daemon).
`tests/test_coordination_wiring.py` covers the wiring, `tests/test_coordination_panel.py` the screen, `tests/test_coordination_hermes.py` the Hermes dispatch, `tests/test_kanban_board_mirror.py` the kanban mirror and `tests/test_coordination_watch.py` the stuck-work check.
