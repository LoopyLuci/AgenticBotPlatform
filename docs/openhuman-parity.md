# OpenHuman feature parity

[OpenHuman](https://github.com/tinyhumansai/openhuman) is GPL-3.0, so none of its code is in ABP: every feature below
is ABP's own code, written from OpenHuman's documented behaviour. This page tracks each feature and where ABP has it.

**Done** = built and tested. **Had** = ABP already had an equivalent. **Planned** = not built yet. **Not doing** = out of scope, with why.

## Memory

| OpenHuman | ABP | Status |
|---|---|---|
| Obsidian vault (memory as Markdown you edit) | `bot/memoryfabric/vault.py`: memories (two-way), summaries, entities, notes | Done |
| Memory tree: canonicalise → chunk → score → summary trees | `bot/memoryfabric/knowledge.py` | Done |
| Scoring gate (signals, keep/drop bands, borderline model check, priority boost) | `knowledge.score` | Done |
| Entity extraction + canonical ids + co-occurrence graph | `knowledge.entities`, `neighbors` | Done (regex + name heuristic; a local-model NER pass is planned) |
| Retrieval modes: search_entities, query_source, drill_down, cover_window, fetch_leaves, ingest_document, walk | `knowledge.query`, agent tool `memory_tree`, MCP `memory_tree`, `POST /api/memory/tree` | Done |
| Memory sources (folder, GitHub, RSS, web page, conversation) with status and freshness | `bot/memoryfabric/sources.py` | Done |
| Auto-fetch every 20 minutes | `bot/memoryfabric/service.py` | Done |
| Memory diff (git-backed snapshots, read markers, checkpoints) | `bot/memoryfabric/diff.py`, tool `memory_diff` | Done |
| Memory tools (recall, write, search) | `save_memory` (+ `shared`), `memory_search` | Done |
| Tool-scoped memory (critical/high pinned in the prompt; edict and failure capture) | `bot/memoryfabric/rules.py` | Done |
| Shared memory across other coding agents (agentmemory backend) | `/api/memory`, MCP `memory_*`, the model server's `+memory` | Done (ABP is the shared store) |
| Per-agent source scoping | | Planned |
| Composio OAuth sources (Gmail, Slack, Notion...) | | Planned (needs each service's own OAuth app) |

## Agent and tools

| OpenHuman | ABP | Status |
|---|---|---|
| Approval gate (fails closed) | `bot/agent_runtime/approval.py`, per-call `needs_approval` | Had |
| TokenJuice (tool-output compression, recoverable originals) | `bot/agent_runtime/tokenjuice.py`, tool `tool_output` | Done |
| Goals | `rules.goals`, `goals.md` in the vault, every prompt | Done |
| Session todos | `todo_write` / `todo_read` | Had |
| Orchestrator, sub-agent fleets | swarms, `spawn_subagent`, delegation | Had |
| Workflows (visual, durable) | routines, schedules | Had in part; a visual canvas is planned |
| Cron & scheduling | `schedule_command`, routines | Had |
| Coder tools | file, grep, git, shell, LSP tools | Had |
| Browser & computer control | browser bridge / gateway | Had |
| Web search & scraper | Anthropic server tools on Claude | Had in part; a provider-independent search tool is planned |
| Model routing (task hints) | `bot/model_router.py` | Had |
| Local models & BYOK | Local AI (`bot/localai`), providers | Had |
| Privacy mode (local only) | `bot/privacy.py` | Done |
| Personalization (learned style, identity, vetoes) | memory extraction (`extract.py`), edicts (`rules.py`) | Done in part |
| Messaging channels | Telegram, Discord, Slack, and more | Had |
| Notifications & activity | push, activity log | Had |
| Voice (STT/TTS, meeting agent) | | Planned |
| Realtime mascot | ScreenBuddy integration | Planned (with the ScreenBuddy module) |
| Theme Studio | | Planned |
| iOS companion | Android app + pairing | Had (Android) |
| Cloud deploy of the core | Web Hosting (`bot/hosting`) | Had |
| OS keyring for secrets | ABP's vault (`bot/vault.py`) | Had |
| Media generation (image/video) | | Not doing yet (needs a paid provider) |
| Wallet (crypto transfers by the agent) | | Not doing: agents do not move money |
| Billing & credits | | Not doing: ABP has no subscription |
