# Local conductor (LF4, phases 1, 2a and 2b)

The factory's own manager: it turns a goal into small tasks, has workers do them, checks every diff against the owner's
standing rules, and merges what passes into an integration branch that waits in a push queue. It uses **local models only**
(ModelMistress first, then Ollama; never a cloud model, whatever the capacity state) and **never deploys**. It pushes only through
the pusher, which is off by default and acts only on a recorded approval. Code: `bot/conductor/`.

```
goal --planner--> plan (board tasks) --workers--> branch per task --reviewer--> accepted branch
                                                       ^   |  rejected: the reasons go back to the worker
                                                       +---+  rejected twice: escalated, dependents never start
accepted branches --integrator--> integ/agents-N --SDK regen, sharded suite--> green --> push queue (awaiting approval)
                                                                                   --approve--> pusher (if enabled) or a person pushes
```

## Planner (`planner.py`)

`abp conductor plan "<goal>"` (or `FILE:LINE`, e.g. a TODO line or a roadmap table row, whose list or table markup is stripped).
A local model answers with one JSON object of 1 to 8 tasks. Each task has:

| Field | Meaning |
|---|---|
| `key` | `conductor:<plan id>:<task id>`; the plan id is a hash of the goal, so planning the same goal again returns the stored plan and the board joins tasks that are already there instead of duplicating them |
| `deps` | ids of tasks that must be done first (acyclic) |
| `scope.resources` | the locks the task takes while it runs: `worktree:<path>`, `branch:<repo>:<branch>`, and `path:<repo>:<file>` for each file it names, so two tasks on the same file never run together |
| `proof` | a command (usually a test) that shows the task is done, and what it shows |
| `capacity` | `local`, `cloud` or empty: `capacity:cloud` tasks wait while the capacity state is local-only (docs/capacity-governor.md) |

The answer is validated. Safe defects are repaired (odd or duplicate ids, unknown or self dependencies, bad paths, an unknown
capacity word); other defects (no proof, a cycle, too many tasks, not JSON) go back to the model with the exact errors, at most
twice. If the model still fails, or no local model answers, a deterministic plan is used (one task per line or `;` part of the
goal, chained, proved by the test command); the plan says `planner: "fallback"`.

## Workers

Plan tasks run through the swarm board runner (`bot/swarm/board_run.py`): one board task per planned task, pulled with `next`,
leased and heartbeated, requeued by the board when a worker dies, following the capacity state. `Step` gained optional `scope`,
`priority`, `detail` and `key` fields for this; the swarm's own steps leave them empty and behave as before.

A worker is an async callable `worker(spec, feedback) -> {"branch", "worktree", "start", "created", "summary"}` that leaves its work
committed on `spec["branch"]`. `AgentLoopWorker` is the real one: a git worktree from the base (or from the branch a dependency
produced), ABP's headless native agent (`abp_agenteval.factory.drive_agent`, small-model mode) on a local model, then a commit.
**Confinement** (the runner the owner approved on 2026-10-07: native agent, file and test tools, `run_shell` only inside an
AppContainer with no network). `AgentLoopWorker(shell=...)` / `abp conductor run --shell none|sandbox`:

| `--shell` | What the agent has |
|---|---|
| `none` (default) | The LF2 bench's tool set: file tools (read, list, glob, grep, repo map, code search, git diff/status, edit, write, patch) and `run_tests`; permission mode `accept_edits`, a locked permission list that denies every other tool. No shell tool at all. `run_tests` starts pytest through `sandbox.start`. |
| `sandbox` | The same plus `run_shell` (and its output/list/kill), allowlisted to pytest, ruff, py_compile, `python -c`, `dir`, `type`, `echo`, running in a Windows AppContainer with no capabilities (`bot/agent_runtime/appcontainer.py`): the kernel denies all network (loopback too) and every path except the worktree (read/write) and the Python install and venv (read only; folders under Program Files and the Windows folder are readable already). |

`sandbox` is refused, never degraded, where there is no AppContainer (`confine.SandboxUnavailable`, CLI exit 2). Before a worker's
first task, `bot/conductor/confine.py` runs three probes through the same `run_shell` path the agent uses and the same settings:
the user's home folder cannot be listed, a file outside the worktree cannot be read, a loopback server cannot be reached (and the
worktree is writable as a control). Any leak and the worker does not start. `tests/test_conductor_runner.py` runs them for real
and checks they are not vacuous (the same probes against an open shell find the leaks).

**Audit.** Every tool call the agent made, with tokens, iterations and why it stopped, is saved per task attempt
(`store.list_tool_calls(plan_id, task_key)`; table `toolcalls`, with a `tokens` column). LF5's digest counts those tokens
(`store.worker_tokens`) in its factory accounting: the headless worker turns write no agent trace.

`abp conductor run --model REF` uses that local model for the **planner** as well as the workers, and `conductor run`, `unblock` and
`review` run in-process, so they need no dashboard token.

**Parallel workers.** `Conductor(..., workers=N)` / `--workers N`: N workers pull with `next` (heartbeated leases, each task in its
own worktree and branch, scopes keep two tasks off the same file). The agent's turn itself holds a process-wide lock (its config
overlay and ephemeral environment are process-global), so agent turns run one at a time; everything around a turn (worktree,
commit, review, proofs) and the other workers' waiting runs in parallel. One local model serves one request stream well anyway.
**Release on failure:** a failure that is not the task's fault (no local model answering yet) hands the board task back (up to 3
times, attempts not charged) instead of failing it; a lost lease or a crashed worker is requeued by the board as before.

**Proofs are not shell commands.** A plan's proof may only be `python -m pytest <test paths or node ids>` (with `-q -x -v -rf -k EXPR`)
or `ruff check <paths>` (`bot/conductor/proof.py`). Test targets are validated by the same function as the `run_tests` tool
(`run_tests.clean_targets`: no shell syntax, no options, no `..`, nothing outside the worktree) and run through the same sandbox
runner (`sandbox.start`, its timeout and tree-kill). Anything else (`python -c`, `cmd`, `powershell`, `;`/`&&`, outside paths) is
refused when the plan is made (the model is asked to fix it) and again at review time as `proof not allowed: <reason>`; it is never
started. A third, symbolic proof, `changed-tests` (`proof.CHANGED_TESTS`, what the deterministic fallback plan uses), runs the test
files the task's branch added or changed since its start; a task that touched no test file fails it. (The fallback used to be the
whole suite, which can never pass inside the no-network sandbox; the integrator runs the full suite anyway.)

A proof runs with **the conductor's own interpreter first on PATH** (an agent environment with `shell: none`, so the configured
sandbox, under the process-wide lock agent turns hold, `proof.ENV_LOCK`, in a thread so the event loop keeps heartbeating). Before,
`python -m pytest` in a proof was whatever `python` the PATH found first, which may lack the project's packages. It does **not** use the
`--shell sandbox` AppContainer: pytest cannot start there (it opens the NUL device and stats the workspace's parent folders, both
denied), which also means a sandbox-shell worker's own pytest runs fail today; see "Still to do".

## Reviewer (`reviewer.py`)

`abp conductor review --repo R --base main --head BRANCH` (exit 1 on a rejection), `POST /api/conductor/review`, MCP
`conductor_review`. Static checks first, the same answer every time; every finding names the rule, the file and line, and the
line itself:

| Rule | Rejects |
|---|---|
| `no_feature_removed` | a removed **route** (`@app.get("/x")` style decorators), **CLI command** (`add_parser`), **CLI flag or argument** (`add_argument`, click options), **MCP tool**, **dashboard section** (`<section id>`, `data-tab/page/section/view/panel`, `*-root` ids) or page heading, **config key** (YAML under `config/`), **public function or class still named in backticks in the docs**, **enum variant**. A feature that is added back in the same diff (a move, a rename) is kept. |
| `no_deletions` | a deleted file the task did not create (`created_files`) and was not told it may delete (`allow_delete`) |
| `leak_guard` | machine paths, user names, private project names on added lines, using `scripts/leak_guard.py`'s rules, modes and allowlist (public repos; `public_repo: false` skips) |
| `hotreload` | a new `bot/` module that `bot/hotreload.py` does not classify |
| `tests` | code changed and no test added or changed |
| `ruff` | ruff findings in the touched Python files as they are on the branch (the repo's ruff config) |

Then, if `conductor.model_review` is on, a separate local model call (a different model from the planner's when there is one)
sees the goal and a capped diff (`review_diff_cap`) and may add concerns; they reject only when the model says reject and names
concerns. A model that cannot be reached leaves the static verdict standing. The report has `to_dict()` (verdict, per-rule
check state, findings, advisory notes) and `to_text()`.

After a static accept, the task's proof command runs in the worker's worktree; a failing proof is a rejection (`proof`).
A rejection sends the findings back to the worker as feedback. After `conductor.max_rejections` (2) rejections the task is
**escalated**: a row in the conductor's escalation list (`GET /api/conductor/escalations`, which LF5's digest reads), the board
task goes to **`blocked`** with an `ESCALATED: ...` note, and tasks that depend on it never start.

## Blocked state and escalations

`blocked` is a real state of the local work board (`bot/coordination`): not leased, owner kept for the record, its locks freed, never
swept, requeued or counted as an attempt, not terminal. Its result holds the minimum a decider needs (`context`): `kind`, the
`question`, the `failing_check` (rule names and the first finding) and a `diff_excerpt` (1500 characters). A re-plan joins a blocked
task by key instead of duplicating it; the dashboard board card counts it. Kinds:

| `kind` | When |
|---|---|
| `rejections` | the reviewer rejected the task twice |
| `rule_decision` | the last rejections were `no_feature_removed` / `no_deletions`: the question is whether the removal may stand |
| `stuck` | two attempts in a row ended with no change (out of steps or time, or nothing to commit); nothing is sent to review |

`Conductor.unblock(plan_id, task_key, by, note)` / `abp conductor unblock PLAN KEY --by owner` records the decision: the escalation
is resolved, the task's rejection count starts again, and its board task goes back to `open` for the next `run` of the plan.

AgentSentinel's board has no blocked state (`running | review | done | failed | released`; it is not modified). There a blocked task
ends `failed` with a `BLOCKED: ...` note and `{"blocked": true, ...context}` as its result (`review` is leased and would be requeued), and
`unblock` answers `NOT_BLOCKED`; the conductor's own escalation list is the durable record and the next run re-creates the task.

## Integrator (`integrator.py`)

`Conductor.run(plan, integrate=True)` or `abp conductor run GOAL --repo R --integrate`. It creates `integ/agents-N` in its own
worktree from the base branch and merges each accepted branch (one that conflicts is aborted and listed, the rest go on), runs
the suite (`conductor.suite_command`, default `pytest tests -n 8 -q -p no:cacheprovider -rf`) and saves the output. The failed
tests the `-rf` summary names are **re-run alone**: a test that passes on the re-run is a **flake**, one that fails again is a
**failure**. Both are recorded on the integration. Any failure makes it `red` and nothing is queued. A green one (flakes
allowed and listed) goes into the **push queue** as `awaiting_approval`.

### SDK regeneration, shards, budget and the machine lock (phase 2b)

Before the suite, if the merged branches changed routes (anything under `bot/dashboard/`, or an added or removed
`@app.get/post/...` or `include_router` line in `bot/`), the integrator runs `scripts/export_openapi.py` in the integration
worktree and **commits** the regenerated `docs/api/openapi.json` and `sdk/typescript/` on the `integ/agents-N` branch, so the queued
sha already contains them (`sdk` on the integration says what happened; a failing regeneration makes the run `red`;
`conductor.regen_sdk: false` turns it off; a repository without the script skips it).

The suite then runs **under a machine-wide lock** (`shards.machine_lock`, built on `scripts/release_guard.PipelineLock`: a live
holder blocks, a dead or recycled one is stale and replaced). The lock file is one per machine (`ABP_SUITE_LOCK`, else
`%PROGRAMDATA%/abp/suite.lock`, or `/tmp/abp/suite.lock`), so two integrator runs, or an integrator run and any other job that takes
the same lock, never run their suites at once. A run waits up to `suite_lock_wait_s` (1800) and is then `red` with "the machine is
busy". pytest `-n` is **sized from the budget** when `suite_workers` is `auto` (idle CPUs and free RAM at 1.5 GB per worker, at
least 1, at most `max_workers`; the budget used is recorded as `budget`), or set to a number; a suite command without `-n` is left
alone. `suite_shards: N` splits `tests/**/test_*.py` by file size into N shards run one after another, each with its own exit
code, summary, failed tests and seconds in `shards`; the log has a section per shard. Failures from all shards are re-run together
alone, as before, to tell flakes from failures; a shard that exits with no failed test named (a crash, a timeout) is `red`.
`gate_command` (e.g. `[python, scripts/local_pipeline.py, --no-deploy]`) is an optional final gate after a green suite, in the same
lock; its result is `gate`, and a failing gate is `red`.

## Push queue and approval

`abp conductor queue`, `abp conductor approve ID --by owner [--note ..]`, `abp conductor reject ID --by ..`,
`POST /api/conductor/queue/{id}/approve|reject` (dashboard token), MCP `conductor_queue` / `conductor_approve`, and the
Coordination page's conductor card. Approval records who decided and when (and the exact commit approved), and answers with the `git push` command; **approving does
not push**. Either a person (the owner, or Claude when the owner delegates) pushes, or the pusher does.

## Pusher (`pusher.py`, off by default)

`conductor.pusher.enabled: true` turns it on. `abp conductor push QUEUE_ID --by NAME`, `POST /api/conductor/queue/{id}/push`
(dashboard token), MCP `conductor_push`. It acts **only on a recorded approval** (who approved is in the entry, who pushed is in
the audit trail), and checks in this order, stopping at the first failure:

1. enabled, and the entry is `approved` (not awaiting, rejected, pushed, stale or being pushed by another call);
2. the branch tip is exactly the commit that was approved - a branch that moved after approval is refused;
3. `git fetch`, then the remote's base branch is an **ancestor** of the commit. If not, the entry is marked **stale** (HTTP 409)
   and the integration has to be re-run: the pusher never merges, rebases or forces;
4. the leak guard (`scripts/leak_guard.py`, with `ABP_PRIVATE_DIR`) is clean on the exact tree of the commit (a detached worktree
   of it, removed afterwards);
5. `git push <remote> <sha>:refs/heads/<base>`: the approved sha, never a branch name, never `--force`.

A refusal leaves the entry as it was (and says why in the audit trail); a push that fails after the claim puts the entry back to
`approved`. Status flow: `awaiting_approval -> approved -> pushing -> pushed` (or `stale`). The audit trail (approval, refusals,
push started, pushed/stale/failed, with who and when) is `GET /api/conductor/queue/{id}/audit`. The pusher never deploys: deploying
the live ABP stays a separate step for the owner.

## Settings (`conductor:` in config)

`model`, `review_model` (local refs; empty = the first local models the router offers), `max_tasks` (8), `max_rejections` (2),
`model_review` (false), `review_diff_cap` (12000), `public_repo` (true), `check_hotreload`, `check_ruff` (true), `suite_command`,
`suite_timeout_s` (3600), `base_branch` (main), `regen_sdk` (true), `suite_shards` (1), `suite_workers` (`auto` or a number),
`max_workers` (8), `suite_lock_wait_s` (1800), `gate_command` (empty), `gate_timeout_s` (7200), and `pusher:` with `enabled` (false),
`remote` (origin) and `target` (empty = the integration's base branch).

Test only what changed, and remember what passed:

- `integrator: {selective_first: true}`: the integrator first runs the test files codeintel's `impact` maps from the merged diff
  (recorded in the integration's `selective`), then the full suite as the final gate. Both results are kept.
- `review_scope: {fail: false}`: when a task declares its files, the reviewer asks `impact` for callers/importers of the changed code
  outside them: a `scope` advisory (a rejection with `fail: true`).
- `cache: {enabled: true}`: `bot/conductor/cache.py` stores *passing* proofs and suite/shard runs in CacheIt (module `cacheit`, hub
  operations `cache.get`/`cache.put`, namespace `conductor`), keyed by the worktree's tree hash, the exact command and the test ids. A
  hit starts no process. With no CacheIt hub (or any error) every lookup is a miss and nothing is stored: runs just go on normally.

## First live run (2026-10-16)

A throwaway clone of ABP, a ModelMistress hub of its own, Qwen3-Coder-30B-A3B (Q4_K_XL, 32k context, 7900 XTX), `--shell sandbox`,
one worker. Goal: tests for `clamp_lease` and `norm_resources` plus a docstring for `mode_conflicts`. The model planned 3 tasks in
7 s; 427 s for the run; 4 agent turns of 24 to 40 steps (about 636k tokens). Two tasks were accepted first time; the docstring task
was rejected twice (a duplicated function the ruff check caught, then a failing proof) and became `blocked` with its diff excerpt.
What it showed: the model's plan put two tasks on one new test file; a task's proof named one test, so a wrong second test in the
same branch passed review; and an unknown local provider used to resolve to the Anthropic transport (fixed: `llm.transport_for_ref`
refuses anything that is not a local provider).

## Still to do

pytest inside the `--shell sandbox` AppContainer (needs the NUL device avoided, e.g. `-p no:logging -s`, and read-attributes on the
workspace's parent folders: a confinement change for the owner to approve); other pipelines (LF6) taking the same machine lock as the integrator; LF5 reading the blocked contexts; agent turns in
separate processes (so they can truly run in parallel and be killed by PID); measuring plan quality and review false
positives over more live runs; one proof per task file, so a task's proof covers every test it adds.
