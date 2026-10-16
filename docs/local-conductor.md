# Local conductor (LF4, phase 1)

The factory's own manager: it turns a goal into small tasks, has workers do them, checks every diff against the owner's
standing rules, and merges what passes into an integration branch that waits in a push queue. It uses **local models only**
(ModelMistress first, then Ollama; never a cloud model, whatever the capacity state) and **never pushes or deploys**. Code:
`bot/conductor/`.

```
goal --planner--> plan (board tasks) --workers--> branch per task --reviewer--> accepted branch
                                                       ^   |  rejected: the reasons go back to the worker
                                                       +---+  rejected twice: escalated, dependents never start
accepted branches --integrator--> integ/agents-N --suite--> green --> push queue (awaiting approval) --approve--> a person pushes
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
**Confinement.** The agent gets the existing confined native-agent tool set of the LF2 bench (`shell="none"`): file tools
(read, list, glob, grep, repo map, code search, git diff/status, edit, write, patch) and `run_tests`, with permission mode `accept_edits` and a locked permission list that denies
every other tool. It has no shell tool; `run_shell` exists only in the bench's `sandbox` mode (Windows job / AppContainer, no
network), which the conductor does not turn on in this phase. `run_tests` itself starts pytest through `sandbox.start`.
The confined AppContainer worker runner is phase 2.

**Proofs are not shell commands.** A plan's proof may only be `python -m pytest <test paths or node ids>` (with `-q -x -v -rf -k EXPR`)
or `ruff check <paths>` (`bot/conductor/proof.py`). Test targets are validated by the same function as the `run_tests` tool
(`run_tests.clean_targets`: no shell syntax, no options, no `..`, nothing outside the worktree) and run through the same sandbox
runner (`sandbox.start`, its timeout and tree-kill). Anything else (`python -c`, `cmd`, `powershell`, `;`/`&&`, outside paths) is
refused when the plan is made (the model is asked to fix it) and again at review time as `proof not allowed: <reason>`; it is never
started.

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
task goes to `review` and then `failed` with an `ESCALATED: ...` note, and tasks that depend on it never start.

## Integrator (`integrator.py`)

`Conductor.run(plan, integrate=True)` or `abp conductor run GOAL --repo R --integrate`. It creates `integ/agents-N` in its own
worktree from the base branch and merges each accepted branch (one that conflicts is aborted and listed, the rest go on), runs
the suite (`conductor.suite_command`, default `pytest tests -n 8 -q -p no:cacheprovider -rf`) and saves the output. The failed
tests the `-rf` summary names are **re-run alone**: a test that passes on the re-run is a **flake**, one that fails again is a
**failure**. Both are recorded on the integration. Any failure makes it `red` and nothing is queued. A green one (flakes
allowed and listed) goes into the **push queue** as `awaiting_approval`.

## Push queue and approval

`abp conductor queue`, `abp conductor approve ID --by owner [--note ..]`, `abp conductor reject ID --by ..`,
`POST /api/conductor/queue/{id}/approve|reject` (dashboard token), MCP `conductor_queue` / `conductor_approve`, and the
Coordination page's conductor card. Approval records who decided and when, and answers with the `git push` command; **it does
not push**. A person (the owner, or Claude when the owner delegates) pushes.

## Settings (`conductor:` in config)

`model`, `review_model` (local refs; empty = the first local models the router offers), `max_tasks` (8), `max_rejections` (2),
`model_review` (false), `review_diff_cap` (12000), `public_repo` (true), `check_hotreload`, `check_ruff` (true), `suite_command`,
`suite_timeout_s` (3600), `base_branch` (main).

## Phase 2 needs

The confined worker runner (AppContainer worktrees, N in parallel); the integrator regenerating the SDK and running sharded
pipelines (LF6); a pusher that acts on an approval; a durable `blocked` state on the work board for escalations (today they use
`review` then `failed` plus the conductor's list); a live run of a real roadmap item with a local model, and measuring plan quality
and review false positives on it.
