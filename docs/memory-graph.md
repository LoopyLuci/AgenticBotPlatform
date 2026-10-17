# Memory graph (phases 1-2)

A memory is formed from a combination of signals: the goal, the tools and skills used, the outcome and the files touched.
The memory graph records one *episode* per agent run and keeps a small graph of which tools, skills, files and models helped
which kind of task. It extends the trace, the handoff packs, the context vault and codeintel; it copies none of them.

## The record
`bot/memory_graph/builder.py` joins, when a run ends (`Trace.end()` calls `builder.on_run_end`; best effort, never raises):
- the trace (`data/agent/traces.db`): tools and their errors, skill loads, model, tokens, usd, iterations, duration, status;
- the handoff pack of the run's task (goal, files touched, test status, last error), when the turn has a task identity.

Stored in `data/agent/memory_graph.db` (beside the trace store; `ABP_MEMORY_GRAPH_DB` overrides). A file of its own because the
vault is off by default and tiered, while the graph is small counts that must be cheap to read and safe to rebuild.

`episodes`: id (= run_id), created, project, task_type, goal_summary (redacted), tools `{name: {calls, errors}}`, skills `[names]`,
files (repo-relative), errors `[{tool, sig}]`, model, tokens, usd, iterations, duration_ms, outcome `{status, test_status, score}`,
run_id, task_id. Score: tests passing 1.0, ok with no test signal 0.7, tests failing 0.3, run failed 0.0 (0.1 if tests passed).
A score of 0.6 or more is a win. Chat runs with no tool calls and no task identity are skipped. `build_episode` is idempotent.

The trace gained `task_type` on `agent.run.start`, `task_id` on `agent.run.end`, and `skill` (a name) on `tool.call` for
`read_skill`/`read_skill_file`. Still content-free.

## The graph
`mem_nodes(id, type, key)`, type in task_type/tool/skill/file/symbol/error/fix/model.
`mem_edges(src, dst, rel, n, wins, fails, weight, last_seen)`, rel in used_in/helped/caused/resolved_by/co_occurred.
`weight = (wins+1)/(n+2)`; recency decay is applied on read (`effective_weight`, half-life `half_life_days`).

| edge | meaning |
|---|---|
| tool/skill --helped--> task_type | used in a run of that type; wins/fails by outcome |
| model, file --used_in--> task_type | same, for models and touched files |
| file --co_occurred--> file | touched in the same episode |
| symbol --co_occurred--> file | codeintel symbols of a touched file (names only) |
| tool --caused--> error | a tool call failed with this error signature |
| error --resolved_by--> fix | the Fix half of a context vault `error_fix` item, on a winning run |

codeintel's own refs/imports/testmap are never copied; they are joined at query time.

## Fused retrieval
`recall.recall_for_task(goal, project, files, root=...)` returns ranked tools, skills, episodes and `avoid`.
Episode score = 0.45 BM25 over goal/task type/tools/skills/files + 0.2 graph proximity (the episode touched a file the goal
names, or one codeintel hop away: importers, imports, test map, files referencing its symbols) + 0.25 outcome x relevance
+ 0.1 recency. Tool/skill score = 0.4 x edge weight x confidence (n/(n+3)) x decay + 0.6 x outcome signal of the relevant
episodes that used it. Tools and skills that mostly appear in failed relevant episodes go to `avoid`, with the errors those
episodes ended on and a known fix if the graph has one. The agent tool `memory_relevant` wraps it with capped output.

Config (`memory_graph:`): enabled (true), codeintel, vault_fixes, half_life_days, max_files, max_symbols_per_file,
max_episodes, scan_episodes, recall_k, max_output_chars, weights, confidence_n. Defaults in `bot/memory_graph/settings.py`.

## Hooks for later phases
- P3: `recall.semantic_scores(goal, episodes)` returns `{}` and has weight 0; embed `goal_summary` (reuse
  `context_vault.embed`), store vectors in an `episodes` column or side table, add a `semantic` weight.
- P4: call `recall_for_task` from the planner/conductor/manager to prefix a brief with `format_text(...)`; pass `root` for
  the proximity term and `task_type` from the caller where known.
- P5: pruning/merging of rarely-seen nodes, a dashboard view.
