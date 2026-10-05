# Shared memory: one memory and one conversation for every model

Switch a bot from Claude to a local Qwen, from OpenCode to Kestrion, or let a backup backend answer when the first
fails: the next model knows what the others were told to remember and what was said. That is the memory fabric
(`bot/memoryfabric`).

## What is shared

| | Who sees it | Where it comes from |
|---|---|---|
| **Shared memories** | every bot, every model, and programs connected to ABP | the dashboard, `/memory`, the agents' `save_memory` with `shared: true`, "remember ... for every bot", MCP `memory_add` |
| **A bot's memories** | that bot, whatever model it runs | the same, without `shared` |
| **The conversation** | the next model to answer in that chat | recorded at the router for every backend |

Memories keep the review gate they always had: a bot's own follow its "memory approval" setting; shared ones follow
`shared_approval` (on by default), so nothing becomes a shared memory without a person's say-so unless that is
turned off.

## How a model gets them

- **Memories.** The most confirmed ones sit in the system prompt (the same block every turn, so prompt caching keeps
  working). The ones related to the message - found by meaning with ABP's local embedding model, a local Ollama, or
  hashed TF-IDF when neither runs - are sent with the message. Backends that build no system prompt of their own
  (Claude CLI, Claude Desktop, Hermes, Kestrion, OpenCode, OpenClaw) get the block in front of the message.
- **The conversation.** Each backend gets the turns it did not see: a backend that keeps its own session (the native
  loop, the Claude CLI, Hermes, Kestrion) gets only the turns other models answered since it last spoke; a one-shot
  backend gets the recent conversation every time. Long conversations hand over a running summary plus the latest
  turns, within `handoff_chars`.
- **Things said outright** ("remember that ...", "my name is ...", "I prefer ...", "never ...") are proposed as
  memories after each turn, through the same gate. Questions never are.

## For other programs

- **Ollama / OpenAI clients** of ABP's model server (port 11436): ask for model `qwen2.5:0.5b+memory`, or send
  `X-ABP-Memory: 1` (and `X-ABP-Instance: <id>` for a bot's own memories). The memories go into the system message.
- **HTTP**: `/api/memory` - entries, review, `search`, `context` (the block for a prompt), and `threads`, where
  another program can add its own turns so ABP's models see them too. Integration keys get it with the
  `memory:read` / `memory:write` scopes (the Omnisystem preset has both).
- **MCP**: `memory_search`, `memory_add`, `memory_context` on ABP's MCP server.

## Settings (`PUT /api/memory/settings`)

| Setting | Default | |
|---|---|---|
| `shared_approval` | true | shared memories wait for review |
| `recall_k` / `recall_min` | 6 / 0.45 | related memories per message; the similarity a neural embedder must reach |
| `block_chars` | 4000 | size of the memory block |
| `handoff_chars` / `handoff_turns` | 6000 / 12 | the conversation a model gets after a switch |
| `inject` | true | turn the whole thing off for backends that build no prompt of their own |
| `auto_extract` | true | propose memories from what the person says outright |

## What feeds the knowledge base

Underneath the memories sits the knowledge tree (`bot/memoryfabric/knowledge.py`): every source's text as
scored chunks, entities and summary trees, which the agents' `memory_tree` tool, MCP and
`POST /api/memory/tree` all query. There are seven kinds, one of them the vault's own notes:

| Kind | Reads | Needs |
|---|---|---|
| `folder` | a local folder (`glob`, default `**/*.md,**/*.txt`) | `path` |
| `notes` | the vault's `notes/` - always present | - |
| `github` | a repository's commits, issues and pull requests | `repo` (`owner/name`) |
| `rss` | an RSS or Atom feed's items | `url` |
| `web` | a web page, optionally one element (config `selector`: a tag, `#id` or `.class`) | `url` |
| `conversation` | ABP's own threads, one item per thread | - |
| `nexusfoundry` | the NexusFoundry foundry's Knowledge Modules, one item per KM | `path` |

```
abp memory source-add nexusfoundry "NexusFoundry" --path X:/Projects/NexusFoundry
abp memory source-sync <the id it printed>
```

A `nexusfoundry` source points at a NexusFoundry checkout or straight at a Knowledge Module folder (a
checkout's own store, `storage/knowledge_modules`, is found by itself). Each KM becomes one document in
the base: its name and `km_id` as the title, its domain, subdomain and tags as the entities a model can
match on, and the contents of its `chunks.json` as the body - so every model ABP runs can recall what the
KMs hold, not just the ones running a NexusFoundry adapter. A folder without `meta.json` is not a module
and is skipped, an unreadable one is skipped rather than failing the sync, and a module deleted from the
store is dropped from the base on the next sync. `max_chunks` (default: all) caps one module's facts.

The same seven kinds are what `GET/POST /api/memory/sources` accepts, and `GET /api/memory/sources` reports
each one's chunk counts and freshness (`active` / `recent` / `idle`).
