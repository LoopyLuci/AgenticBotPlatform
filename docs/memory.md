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
