# Local models on ABP's native agent (LF0), 2026-10-13

Two local models served by a ModelMistress 0.2.1 instance (Vulkan, one GPU, context 32768, one slot), driven headless
through ABP's native agent over its OpenAI-compatible endpoint (`--base-url`, `--context 32768`). Both measurements
ran alone on the GPU. An earlier 31-task run of the coder was thrown away because another job loaded a model beside it.
Each number is a single run, so read differences of a task or two as noise (the two coder suite runs, one discarded,
scored 61.3% and 58.1%).

| Model | 31-task suite | Factory bench (20 measured) | Bench avg time / tokens per task | Failed tool calls |
|---|---|---|---|---|
| Qwen3-Coder-30B-A3B (UD-Q4_K_XL) | 18/31, 58.1% | 11/20, 55.0% | 102 s / 188k | 15 of 359 |
| Qwen3.5-9B (Q4_K_M) | 16/31, 51.6% | 8/20, 40.0% | 79 s / 194k | 2 of 397 |

Failure kinds on the factory bench (failed tasks): coder: wrong edit 7, lint 1, bad tool call 1; 9B: wrong edit 8, gave up 2,
lint 2. No run ended with a tool call written as text, and none hit the context limit or the time limit (600 s per task; the
slowest task took 388 s). By task kind (passed/measured), coder / 9B: bug fixes 3/8 and 2/8, flaky-test fixes 4/5 and 2/5,
features 2/3 and 2/3, config 0/2 and 0/2, docs 1/1 and 1/1, hot-reload classification 1/1 and 1/1. The SDK-regeneration task
needs a command and is skipped while the agent has no shell tool. One coder task failed because the server refused the model's
own tool call ("failed to parse tool call arguments as JSON"), recorded as a bad tool call.

Suite time per task differs by an order of magnitude: the coder averaged 68 s, the 9B 6.6 s. The suite describes every tool to
the model, a large prompt that is processed again on each call; the coder's rate of about 1,300 prompt tokens/s makes that
the likely cause, but this was not isolated, and another job on the GPU during the coder run cannot be excluded.

What the suite failures look like: most failed tasks are a tool the model never called (`used grep called 0x`, `used glob`,
`used todo_write`, `used list_dir`) with an answer from guesswork, plus the multi-step ones (`search_by_concept`,
`save_a_task_as_a_routine`) that took 15-23 calls where 3 are allowed. Whether those answers were calls written as text is
not recorded in the report and was not checked.

## How the factory bench ran

The agent had no shell tool (`--shell none`): it can read, search and edit but cannot run the tests, so these pass rates are
a floor for an agent that can iterate on test output. `repo_map` is denied in the bench (see below). Tasks are graded by the
commit's own tests laid over the worktree after the run, plus ruff on the touched files; "lint" means the tests passed and
ruff did not (not examined whether each hit is a real new finding).

Findings from building it: ABP's `repo_map` tool scans the whole tree in a worker thread and did not finish within minutes on
this repository, which also blocked the end of the run (the harness now closes its event loop without waiting for such a
thread); a tool that scans a large repository without a bound is a hazard for local agents.

Reports: `2026-10-13-suite-*.json`, `2026-10-13-factory-*.json` in this folder (per-task rows).
