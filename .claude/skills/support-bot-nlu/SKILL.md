---
name: support-bot-nlu
description: Use before making any change to the Support Bot's intent-classification system (bot/support_bot/*, android-app/.../nlu/*) — the tiered cascade architecture, Knowledge Module registry, and the hard cross-platform parity rule that must never regress.
---

# Support Bot NLU — tiered modular hybrid architecture

This repo's Support Bot intent classifier is a 3-tier cascade, built to
stay fully offline-capable while gaining real power from server and
internet connectivity when available. Read this before touching any of
`bot/support_bot/*.py` or `android-app/.../mobile/nlu/*.kt`.

## The tiers

- **Tier 0 (on-device, always works, zero network, 100% classical).**
  `bot/support_bot/cascade.py` runs one `TfidfCentroidModel` +
  `NeuralIntentClassifier` pair per enabled Knowledge Module
  (`bot/support_bot/knowledge_modules.py`'s `MODULE_REGISTRY`), then
  `reduce_cross_module()` picks the single best answer across modules.
  Mirrored exactly in Android's `CascadeClassifier.kt` +
  `HybridClassifier.kt`.
- **Tier 1 (server, always live for the desktop dashboard).** The
  single global, unpartitioned `hybrid.classify()` — unchanged,
  always-freshest-trained. `cascade.classify_tier1()` wraps it, folding
  in Tier 1.5 when enabled.
- **Tier 1.5 (optional, server-only, OFF by default).**
  `bot/support_bot/embeddings.py` — a sentence-embedding semantic layer,
  feature-flagged via `config/backends.yaml`'s
  `support_bot.tier1_5_embeddings.enabled`. Lazily imports
  `sentence-transformers` — NEVER a hard dependency of this project.
  Never touches Android.
- **Tier 2 (LLM fallback, only when Tier 1/1.5 both say "unknown").**
  `bot/support_bot/llm_fallback.py` — a real free-model LLM call,
  closed-set classification, gated by `bot/swarm_budget.py`. A confident
  result feeds back into the pending-review queue
  (`source_kind="llm_fallback_live"`), closing the active-learning loop
  on real production misses.

`cascade.classify_full_cascade()` is the entry point that runs all of
this; `bot/dashboard/server.py`'s `POST /api/support-bot/classify` route
calls it.

## The hard rule: golden-fixtures parity must never regress

`tests/test_support_bot_golden_fixtures.py` (Python) and
`android-app/.../nlu/GoldenFixturesTest.kt` (Kotlin) both replay
`bot/support_bot/testdata/golden_fixtures.json` against the SAME trained
model and assert identical `(intent, confidence)` within `1e-4`. This is
a black-box proof on final decisions, not intermediate vectors — it
gives real freedom to restructure orchestration, but:

**Any change to `hybrid.vote()`'s math, or to `cascade.py`'s
`reduce_cross_module()`, needs new golden fixtures covering it, added to
BOTH platforms, before the change is considered done.** Regenerate
fixtures from the real trained Python classifier — never hand-edit
expected values to make a test pass.

## Knowledge Modules

`bot/support_bot/knowledge_modules.py`'s `MODULE_REGISTRY` groups the
system's intents into 14 independently trainable/toggleable modules
(`bots`, `mcp`, `backups`, `devices_security`, etc. — `core_status` is
always-resident, never unloadable). Every intent added to
`bot/support_bot/training_data.py` MUST be added to exactly one module —
`tests/test_support_bot_knowledge_modules.py`'s self-check fails loudly
otherwise (same convention as `bot/hotreload.py`'s own module-
classification self-check).

- `bot/support_bot/module_manifest.py` tracks each module's
  enabled/disabled state and version (a training-data hash) —
  `data/support_bot_models/manifest.json`.
- `cascade.retrain_module(module_id, accept_if_regression_under=None)`
  retrains and persists one module, with the same optional held-out-
  accuracy regression gate `hybrid.retrain_all()` uses for the (still
  separately maintained) single global model.
- Unloading a module flips its `enabled` flag (instant, reversible);
  removing one is a separate, explicit, irreversible deletion — never
  conflate the two.

## Growing training data — use the tools, not hand-edits

`bot/support_bot/training_data.py` is the hand-authored BASELINE, not
where ongoing growth happens. Use the `support_bot_*` MCP tools (see the
`support-bot-training-ops` runtime skill, `bot/skills.py`) or the
dashboard's Training/Pending tabs — never hand-edit `training_data.py`
for routine data growth. The free-model-only synthetic-gen swarm
(`bot/support_bot/synthetic_gen.py`) auto-approves a `(phrase, intent)`
pair straight into the live set only when 2+ independent free models
produce near-identical wording for it in the same batch — everything
else goes through human review in the pending queue, which always shows
an audit trail (`approved_by`, `source_kind`) even for auto-approved
rows, and supports reverting one via
`bot/db.py`'s `revert_support_bot_pending_example()`.

## Design doc

The full architecture rationale (why classical-first, why the tier
boundaries are where they are, the auto-approve rule's precise
definition) lives in this project's original planning conversation —
continue that same plan rather than starting a competing one if you're
picking this work back up.
