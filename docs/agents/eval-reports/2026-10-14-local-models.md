# Local models re-measured with run_tests and small-model mode (LF0 re-run), 2026-10-14

The 2026-10-13 measurement (`2026-10-13-local-models.md`) repeated on ABP main 4e7ae2c, which adds `run_tests` (pytest on
named test paths, for an agent without a shell), a fast `repo_map` (allowed again in the bench) and small-model mode (core
tools, compact prompt, tool results cleared earlier). Same models and quants, same ModelMistress 0.2.1 Vulkan build in its
own home (not the owner's hub), context 32768, one slot, one model on the GPU at a time (gpu0 held on the board for the
whole run). Single runs unless stated; read a difference of two or three tasks as noise.

## Result: no improvement, and the coder got slower

| Model | Suite 10-13 | Suite 10-14 small on / small off | Factory 10-13 | Factory 10-14 |
|---|---|---|---|---|
| Qwen3-Coder-30B-A3B | 18/31 | 16/31 / 16/31 | 11/20 (55%) | 10/20, 9/20, 7/20 (three runs, see below) |
| Qwen3.5-9B | 16/31 | 16/31 / 17/31 | 8/20 (40%) | 8/20 |

Factory, per run (20 measured; `sdk-regenerate-cluster-blobs` is still skipped: it needs a command and `run_tests` is not one):

| Run | Passed | Failure kinds | Avg time / tokens | Failed tool calls | run_tests calls |
|---|---|---|---|---|---|
| Coder 10-13 baseline | 11/20 | wrong edit 7, lint 1, bad tool call 1 | 102 s / 188k | 15 of 359 | none |
| Coder 10-14 A (small prompt/context, full bench tool list) | 10/20 | wrong edit 9, lint 1 | 175 s / 289k | 58 of 522 | 114 (20/20 tasks) |
| Coder 10-14 B (small mode, tool list cut to the core set) | 9/20 | wrong edit 7, lint 3, gave up 1 | 187 s / 288k | 38 of 521 | 109 (20/20 tasks) |
| Coder 10-14 C (same as B, repeat) | 7/20 | wrong edit 10, lint 2, gave up 1 | 205 s / 288k | 44 of 513 | 95 (18/20 tasks) |
| 9B 10-13 baseline | 8/20 | wrong edit 8, gave up 2, lint 2 | 79 s / 194k | 2 of 397 | none |
| 9B 10-14 (config B) | 8/20 | wrong edit 5, gave up 6, lint 1 | 141 s / 320k | 2 of 625 | 23 (12/20 tasks) |

Runs B and C are the configuration the task asked for; A is an earlier run kept because it differs only in the tool list (see
"Bug in the bench"). The three coder runs scored 10, 9 and 7 against 11: the spread between identical-config runs (9 and 7)
is as large as the difference to the baseline, so the honest reading is "not better, possibly slightly worse", not a measured drop.

By kind (passed/measured), coder B / C, 9B: bug fixes 3/8, 2/8, 3/8; flaky-test fixes 3/5, 1/5, 2/5; features 1/3, 2/3, 1/3;
config 0/2, 1/2, 0/2; docs 1/1, 0/1, 1/1; hot-reload 1/1 each. The tasks every coder run passed are the same ones as before
(`deploy-app-only-instance`, `gate-instance-token`, `gate-registry-reader`, `hotreload-classify-streaming`,
`module-provider-secret`); the tasks that moved are the borderline ones.

### Did run_tests help?

* The coder calls it on essentially every task (5-6 calls per task, 95-114 per run) and still does not pass more. So being able
  to run the tests did not turn wrong edits into right ones; 7-10 wrong edits per run remain. It mostly spent iterations
  (mean 27 per task against 19) and tokens (289k against 188k per task).
* Pass rate split by "called run_tests": the coder called it in every task of A and B, so there is nothing to compare. In C it
  was called in 18 tasks (7 passed) and not in 2 (0 passed): too small to say anything.
* The 9B called it in 12 tasks (7 passed) and not in 8 (1 passed). That looks like a large effect but is confounded: the tasks
  without a call are mostly runs that spent all 60 steps reading and searching ("gave up" rose from 2 to 6, seven runs hit the
  60-step limit) and never reached the point of checking. It does not show that run_tests made the difference.
* Failed tool calls rose for the coder (15 -> 38-58) while the 9B stayed at 2. In run C `run_tests` accounted for 23 of the 44
  (the rest `edit_file` 18, `multi_edit` 3). Per-tool failure counts were only recorded from run C on (new `tool_error_counts` row
  field). That a `run_tests` failure is a refused argument (a path that does not exist, a node id with a character the tool does
  not allow) rather than a failing test is an inference from `bot/agent_runtime/run_tests.py` (a failing test returns text, only a
  bad argument raises); the error texts were not captured, so this is not verified.
* Two 9B runs ended with a tool call written as text (`desktop-mcp-platforms`, `gate-registry-temp-name`); none in the coder runs.
* Timing: the average per task rose from 102 s to 175-205 s for the coder. The run time includes the pytest runs inside
  `run_tests` (the repository's test files on a loaded machine); that share was not separated from model time.

### The 31-task suite

| Model / mode | Passed | Time per task | Tokens per task |
|---|---|---|---|
| Coder 10-13 | 18/31 | 67.8 s | 134k |
| Coder 10-14 small on | 16/31 | 5.7 s | 13.6k |
| Coder 10-14 small off (control, same code) | 16/31 | 54.1 s | 170k |
| 9B 10-13 | 16/31 | 6.6 s | 64k |
| 9B 10-14 small on | 16/31 | 6.0 s | 11.6k |
| 9B 10-14 small off | 17/31 | 9.9 s | 83k |

Small-model mode cuts the suite's tokens by 12x (coder) and 7x (9B) and the coder's time by 10x; that matches the 10-13 note that the
suite's large tool list dominated its cost (the cause is now the better supported reading, though only the on/off pair above
tests it). The pass rate is unchanged. The suite is not a fair test of small mode: it checks that specific tools get used
(`todo_write`, background shell, `lsp`, `list_agents`, `know_your_allowance`, `web` ones), and a tool outside the core set
is simply not offered, so those tasks cannot pass. Failures that appear with small mode on and not off are of that kind
(coder: `background_job`, `web_injection_is_contained`, `list_agents_then_delegate_read_only`, `know_your_allowance`,
`fix_what_the_language_server_reports`); I judged that from the tool sets and did not read each transcript.
The same code with small mode off is the fairer comparison with 10-13: 16/31 and 17/31 against 18/31 and 16/31, i.e. no change.

## Was small-model mode really on?

Not by default, and that is a finding. The bench's transport is built from a bare `--base-url`; it has no provider name, so
`small_model.profile_for` reads it as not local (`turn_budget.is_local` needs a provider name in `LOCAL_PROVIDERS`; loopback alone
does not count) and "auto" returns None. The overlay therefore sets `native_agent.small_model.enabled: true` (new
`--small-model on|off|auto`, default `on`, for `factory run` and `run --live --base-url`). With it the "small-model mode on for
... forced on by native_agent.small_model.enabled; sending 13 core tools, compact prompt, keeping 3 tool results, compacting at 60%"
trace note appeared (checked on a single task by wrapping `small_model.announce`; it is not stored in the reports).

Bug in the bench found on the way: the bench passes `allowed_tools` to the turn, and `native_backend` skips small mode's tool filter
for a turn that names its tools. So the first coder run (A) had small-mode prompt and context management but the bench's own 14-tool
list (including `git_diff`, `git_status`, `tool_output`, `todo_read`, which are not core). Fixed in `abp_agenteval/factory.py`: with
small mode `on` the bench offers the intersection with `small_model.core_tools()`; runs B, C and the 9B run use that. This is a
bench-side fix; the `allowed_tools` bypass in `bot/backends/native_backend.py` is deliberate ("honoured as it is") and was not changed.

## What confounds the comparison

* One run per model and configuration; the coder's three runs span 7 to 10, the baseline is a single 11.
* ABP changed between 10-13 and 4e7ae2c in more than the three features (the repo moved on); `repo_map` also became available.
* The tool list shrank under small mode (no `git_diff`/`git_status`/`tool_output`/`todo_read`), so "small mode" and "run_tests" are
  measured together, not separately. A run with small mode off and `run_tests` on was not done for the factory bench (about an hour
  per run); only the suite has that control.
* The owner's hub and its `llama-server` (started 2026-10-11) were running on the machine throughout, as they presumably were on
  10-13; whether that process held GPU memory or compute was not checked. The board lock on gpu0 covered every agent job.
  The coder's average time rose 70-100% with no other change visible to me, so some of it may be machine load.
* The judge, tasks and prompt are the same except the prompt now tells the agent to use `run_tests`.
* `run_tests` runs `python -m pytest` in the bench with no sandbox configured (the bench uses `--shell none`, no sandbox section), so
  the tests ran as a plain process in the throwaway worktree, as the judge's own tests do.

## Reports

`2026-10-14-factory-qwen3-coder-30b-a3b.json` (B), `...-run2.json` (C), `...-fulltools.json` (A), `2026-10-14-factory-qwen3.5-9b.json`,
`2026-10-14-suite-qwen3-coder-30b-a3b.json`, `...-smalloff.json`, `2026-10-14-suite-qwen3.5-9b.json`, `...-smalloff.json`.
Rows hold per-task tool counts; `tool_error_counts` exists only in `...-run2.json`.
