# Context vault

The most useful context API models work with (decisions, plans, file summaries, error fixes, preferences, rules, task
state, board briefs and reviews) is copied **passively** into a local store, and recalled for local models. Off until
`context_vault.enabled: true`; nothing is created or captured while it is off.

## Tiers

An ordered list in `config/backends.yaml`, fastest first:

```yaml
context_vault:
  enabled: true
  inject_for_local: true       # a small "Relevant context" block on local-model turns, never on API turns
  tiers:
  - {name: ram,  kind: ram, max_bytes: 268435456}
  - {name: nvme, kind: dir, path: /mnt/nvme/abp-ctx, max_bytes: 4294967296}
  - {name: ssd,  kind: dir, path: /mnt/ssd/abp-ctx,  max_bytes: 21474836480}
  - {name: hdd,  kind: dir, path: /mnt/hdd/abp-ctx,  max_bytes: 0}       # 0 = no cap
```

Windows paths with forward slashes (a drive letter followed by `:/abp-ctx`) work. Default (no `tiers`): a RAM tier and one directory tier under
the data folder.

| Tier | Medium | Role |
| --- | --- | --- |
| hot-fast | GPU VRAM | Not here. ModelMistress will keep saved KV/prompt caches; the vault only supplies the text. |
| hot-slow | RAM | `kind: ram`. A **cache**: items that were read recently are copied in; the durable copy stays below. Lost on restart, rebuilt by use. |
| warm-fast | NVMe | First `dir` tier: where every new item lands. |
| warm-slow | SATA SSD | Next `dir` tier. |
| cold | HDD | Last tier. Never evicts until its own `max_bytes` is reached; then the lowest-importance, oldest items are deleted (logged). |

Put the tier you want items to land in first, and the biggest, slowest disk last. Extra optional keys: `use_cacheit: true`
on the RAM tier mirrors it into CacheIt (`bot/cache.py`) when a CacheIt hub answers; tier backends implement
`tiers.TierBackend`, so a CacheIt N-tier store can replace the file tiers later.

## How items move

An item lives in exactly one durable tier (plus an optional RAM copy). The SQLite index (`<data>/context_vault/index.db`)
holds metadata, a 400-character preview and the optional embedding; bodies are files in the tiers.

- **Promotion on read.** Reading bumps `hits`/`last_access`. When the keep score
  (0.3 importance + 0.4 frequency + 0.3 recency, 7-day half-life style) is at least `promote_score` (0.35) the item is
  copied into RAM and moved one tier up, in the background; `min_residency_s` stops ping-pong.
- **Demotion.** A tier over `max_bytes` sheds its lowest keep scores to the next tier until it is under
  `demote_target` (90%) of its cap; that cascades down. Maintenance runs in the vault's own thread every
  `maintenance_interval_s`, and after writes.
- **Crash safety.** Move = mark `moving` -> write destination -> verify SHA-256 -> update index -> delete source. On
  open, `recover()` finishes a verified move, rolls back a damaged one, and sweeps `.part` files and unreferenced bodies.

## Capture

Cheap hooks queue work; a worker thread extracts, redacts, embeds and stores.

- compaction summary (`compression.maybe_compress`): sentences classified into decision / plan / rule / preference /
  file_summary / error_fix, plus the whole summary as `task_state`;
- turn end (native agent loop): error -> fix pairs from tool results, stated preferences/rules, decisions in the answer;
- work-board briefs (created), reviews and results (done/failed) from `bot/coordination`; swarm/sub-agent step results.

Only turns served by API models are captured (`capture.local_turns: true` to include local ones). Optional
`summarise.enabled` asks a configured local model (ModelMistress) for the items; heuristics run if it is down.

**Secrets are never stored.** Text goes through `abp_native.scrub_credentials`, `secrets_guard.redact` and
`diagnostics.redact`; an item with a private key block or more than two credential hits is dropped.

**Dedup.** Item id = hash of the normalised text. Near duplicates (same kind and project) are dropped by embedding
cosine above `dedup_threshold` (0.92) when embeddings are on, else by 90% word overlap. Re-seeing an item raises its importance slightly.

## Recall

`vault.recall(query, project, kinds, k, budget_tokens)` returns the best items that fit the budget (one oversized item
is truncated). Keyword (BM25) scoring always works; with `embeddings.enabled` and a ModelMistress embedding model the
score blends cosine similarity, falling back to keywords on any failure. The agent tool `context_recall` is read-only.
For local-model turns `inject_for_local` adds a budgeted block (`inject_k`, `inject_budget_tokens`) to the system prompt.

## Surfaces

`python -m bot.context_vault status|tiers|recall|import|export|rebalance`; `GET /api/context-vault/status` and `/items`
(read-only); a card on the Storage page: per-tier items and bytes, captured / deduped / promoted / demoted, hit rate.
