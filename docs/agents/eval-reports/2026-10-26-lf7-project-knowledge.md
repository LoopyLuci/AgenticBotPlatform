# LF7 project knowledge on the factory bench, 2026-10-26

Does the project knowledge module (`bot/conductor/projectkm.py`, mmkm/1) raise the local coder's pass rate? This compares one
factory-bench run with `--knowledge off` and one with `--knowledge km`. The `km` run builds the module from each task's own base
commit, so the module cannot contain the answer. Each prompt got about 1.6k characters: AGENTS.md rules and module contracts that
match the issue. Lessons from earlier runs were left out of these runs.

Setup: Qwen3-Coder-30B-A3B-Instruct UD-Q4_K_XL on ModelMistress (Vulkan), context 32768, `--shell none` with `run_tests`,
small-model mode on, 20 measured tasks (`sdk-regenerate-cluster-blobs` skipped: it needs a shell). One run each.

## Result: 8/20 → 9/20, within noise

| | off | km |
|---|---|---|
| Passed | 8/20 (40%) | 9/20 (45%) |
| Clean (no unrelated files) | 7 | 6 |
| Failure kinds | wrong edit 8, lint 4 | wrong edit 10, lint 1 |
| Avg time / tokens per task | 187 s / 279k | 247 s / 305k |
| Failed tool calls | 51 of 513 | 59 of 529 |
| By kind | bugfix 3/8, config 0/2, docs 1/1, feature 0/3, flaky 3/5, hotreload 1/1 | bugfix 3/8, config 0/2, docs 1/1, feature 2/3, flaky 2/5, hotreload 1/1 |

Five tasks flipped:
- Gained with km: `sentinel-config-discovery`, `module-provider-secret`, `gate-registry-temp-name`. All three were **lint**
  failures without km: the change was right but ruff rejected it.
- Lost with km: `sandbox-reused-pid`, `gate-registry-reader`. Both were wrong edits.

## Reading

- One extra pass is not a measured gain. Identical-setup runs on 10-14 scored 9 and 7 (`2026-10-14-local-models.md`), so a
  difference of one or two tasks is noise. **LF7's exit criterion (the pass rate rises over LF2 with the same model) is not shown yet.**
- The one pattern worth testing: lint failures fell from 4 to 1, and the three gained tasks were all lint failures before. The
  module's AGENTS.md rules include "ruff before every commit", which may be why. Wrong edits did not fall (8 → 10), so the
  module does not help the model find the right code change.
- The prompt was about 1.6k characters longer, and tasks took longer (+32% time, +9% tokens). Part of the time may be
  machine load; it was not separated out.

## Next

- Repeat both arms at least twice more (three or more runs each), then compare the means and the lint-failure counts.
- If the lint effect holds, test a smaller module that carries only the conventions, without the contracts. That would show
  whether the contracts help at all or only add tokens.

Reports: run outside the repo (`bench-off.json`, `bench-km.json`); not committed.

## Second round (2026-10-27) and what changed

A second pair of runs: off 11/20, km 8/20 (r1: 8 and 9). Mean off 9.5, km 8.5. Per task across both pairs, km lost
`sandbox-reused-pid` twice, and `sentinel-config-discovery`, `module-provider-secret` and `gate-registry-reader` once each. Some of
those flips are a lint slip or an environment error (`PermissionError` on a test's temp folder) rather than the model doing worse.

What the knowledge actually was: about 1.6k characters per task, mostly one-line descriptions of loosely related modules (for
`sentinel-config-discovery`: the TUI connect screen, the local model server, model discovery, backups...). It came after the
issue, under "follow these conventions and contracts". It added reading, not information: in the km runs the agent read
and edited more (for example `sandbox-reused-pid`: 6 edits instead of 1, +10-30% tokens).

Changes (branch `claude/lf7-km-bench`):
- **Strict selection** (`projectkm.select`, the default): a module contract only for a file the issue names, or a module in the
  same sub-package that matches the issue well; a convention or finding only when it shares at least two words with the issue;
  at most 3 items per kind. Average knowledge per bench task fell from about 1,600 to about 260 characters; two tasks get none.
- **Placement and framing:** the block now comes before the issue, as "background, reference only".
- **Token share:** capped at 3% of the context window (1,200 characters at 32k).
- **Self-check and fix rounds** (`factory run --fix-rounds`, default 2): after the agent's turn the bench checks that the edits
  landed, the changed files parse and pass ruff, and the visible tests pass (tests the agent wrote, and `tests/test_<module>.py`
  of each module it changed; never the judge's tests). It then gives the problems back for at most two more turns within
  the task's time limit. Lint failures (4 and 3 tasks in two of the runs) are the direct target.
- **Edit read-back:** `edit_file` and `multi_edit` read the file back after saving. A write that did not land is an error; a
  Python file that no longer parses gets a warning naming the line.
- **`sdk-regenerate-cluster-blobs` runs now:** a task that needs a command gets `run_script`. It runs only the repository's
  generator scripts on an allowlist, with no arguments, and refuses unless the sandbox is a confining one (windows_job,
  docker, wsl or ssh) with network "none". The bench sets windows_job with no network for it.

These changes are not measured yet; the next bench rounds measure them.
