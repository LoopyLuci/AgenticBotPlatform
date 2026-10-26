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
