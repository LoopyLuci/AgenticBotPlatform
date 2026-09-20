# Agent evals

`abp_agenteval` measures the native ABP agent the same way every time, so a change
to the loop, a tool, the prompt or a model can be judged by a number instead of by
feel. It is the first deliverable of the agent roadmap
([ROADMAP.md](ROADMAP.md), P0) and every later capability lands together with tasks
that measure it.

## What a task is

A task is: a **workspace fixture** (files placed in a throwaway directory), a
**prompt**, and **graders** — small deterministic checks run after the agent
finishes:

| Grader | Passes when |
|---|---|
| `file_equals(path, text)` / `file_contains(path, text)` | the file on disk matches |
| `file_absent(path)` | the file was not created |
| `command_passes(["python", "test.py"])` | the command exits 0 in the workspace afterwards |
| `reply_matches(regex)` / `reply_lacks(text)` | the agent's reply does / does not say it |
| `used_tool(name)` / `did_not_use_tool(name)` | the trace shows the tool was / was not called |
| `tool_status(name, status)` | a call of that tool ended `ok`, `failed` or `denied` |
| `finished_ok()` | the run completed without error |
| `within_iterations(n)` | it took no more than *n* model calls |

A task passes only when it has graders and every one passes. A grader that raises
fails the task rather than crashing the run.

Approvals are answered automatically: everything is approved except the tools a task
lists under `approvals={"run_shell": "deny"}`, which is how the suite checks that a
denial holds. Auto-checkpoints are disabled during evals (they have their own tests).

## Two modes

**Scripted** (default) replaces the model with a *golden trajectory* — the list of
`Call(...)` and `Say(...)` steps a good agent would take. It proves the harness, the
tool layer, the approval path and the graders, costs nothing and is deterministic, so
the test suite runs it on every push. It cannot tell you how good a model is.

**Live** uses a real model and does. It is opt-in because it spends tokens:

```bash
python -m abp_agenteval run --live --provider anthropic --model claude-sonnet-5
python -m abp_agenteval run --live --provider openrouter --model some/model --out report.json
```

`--provider` is `anthropic` (needs `ANTHROPIC_API_KEY`) or any provider defined in
`config/providers.yaml`.

## Commands

```bash
python -m abp_agenteval list                       # the tasks
python -m abp_agenteval run                        # scripted, all tasks
python -m abp_agenteval run --task fix_bug --keep  # one task, keep its workspace to inspect
python -m abp_agenteval run --out report.json      # write the JSON report
python -m abp_agenteval run --baseline report.json # fail (exit 1) on a regression
```

Exit status: `0` all passed (and no regression), `1` a failure or regression, `2`
usage error. A **regression** is a task that passed in the baseline and fails now, a
task that disappeared, or a score drop beyond `--tolerance`. New tasks and fixes are
reported, never failed.

## The report

Plain JSON: `score`, `passed`, `total`, `tokens`, `duration_ms`, and one entry per
task with each check's result, iterations, tokens, tool counts and the trace run id.
Compare a live run of one model against another, or against a run of the same model
after a change, by feeding one report to `--baseline`.

## Writing a task

Add it to `abp_agenteval/suite.py`:

```python
Task(
    id="rename_symbol", category="coding", title="Rename a function everywhere",
    prompt="Rename `load` to `load_config` in app.py and cfg.py, then run the tests.",
    files={"app.py": "...", "cfg.py": "...", "test_app.py": "..."},
    script=[Call("read_file", {"path": "app.py"}), ...],       # the golden trajectory
    graders=[g.finished_ok(), g.file_contains("cfg.py", "def load_config"),
             g.command_passes(["python", "test_app.py"])],
)
```

Keep graders about **outcomes** (what is on disk, what was said, what was done), not
about the exact path the agent took, so a better model that finds a shorter path
still passes.

## Traces

Every run leaves a trace in the agent trace store
(`data/agent/traces.db`, override with `ABP_AGENT_TRACE_DB`): the run, each model call,
each tool call and approval. It records shape — tool, status, timing, sizes, a short
redacted target — and never prompts, replies, file contents or tool output. The eval
runner reads it to grade what the agent *did*; dashboards will read the same store.

## Not covered yet

The seed suite exercises today's six-tool kit, so it says little about editing,
search, the web or a browser; those tasks arrive with the phases that add them.
Benchmarks from outside (SWE-bench-style, Terminal-Bench-style tasks) and a
side-by-side run against Claude Code on the same model are planned, not built.
