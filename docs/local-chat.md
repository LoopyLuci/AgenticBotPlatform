# Agentic Workspace (Local Chat)

Shown in the UI as **Agentic Workspace**. The code, routes and ids keep the `local-chat` name. ABP's chat with local models on ModelMistress, with ABP's own agent (`bot/localchat`, `/api/local-chat`). It needs no cloud
backend and no network: with a model on the hub it works offline.

## How a turn runs

Every turn goes through one internal **"Local Chat" app instance** (platform `app`: no external chat platform), so tool
approvals, traces, the memory fabric, the e-stop and privacy mode work exactly as for every other bot. The session chooses
the model per turn (`router.ask(model_override="modelmistress/<id>")`) and the workspace (`cwd`). Modes:

| Mode | Tools |
|---|---|
| `chat` | every tool, conversational (no worktree of its own); edits and commands ask as usual |
| `build` | every tool, in the session's worktree; edits and commands ask as usual |
| `plan` | proposes a plan for approval first (the plan gate) |
| `review` | read-only tools only |

"Every tool" is the whole pool: skills, memory and RAG recall, code search and navigation, web fetch, image and asset
generation, vision, model management, ABP modules, MCP servers, sub-agents, schedules, plugins. A local model in
small-model mode (a window of 32k or less, see `docs/agents/models.md`) is sent a compact core set plus `find_tools`,
which searches the pool and loads what it finds for the next step, so its prompt stays short and nothing is out of reach.

`POST /api/local-chat/sessions/{sid}/send` starts the turn and returns at once. The reply streams on `/api/ws` as
`chat_stream` frames (`chat_id` `localchat:<sid>`); a tool that needs a person arrives as a `local_chat_approval` frame
(decide it through `/api/approvals`); the end is a `local_chat_turn` frame (`done`, `cancelled` or `error`). `cancel` stops
it. A turn left running by a process that died is marked interrupted at the next start, never shown as complete.

## The model picker's catalog

`GET /api/local-chat/models` lists every model the hub can run, grouped `loaded`, `gpu`, `cpu`, `too_big`, `other`. Every
figure says where it came from:

- **metadata**: the model file (architecture, size, quantization, context, mixture of experts, vision)
- **planned**: the hub's `model.plan` against this machine's GPU (fits / fits after unloading another / does not fit, VRAM)
- **measured**: speed from real replies (llama.cpp's own `timings.predicted_per_second`, reported by the transport) and from
  benchmarks (`POST /api/local-chat/models/benchmark`); quality from ABP's eval reports (`docs/agents/eval-reports`)
- **estimated** / **rough_estimate**: speed where nothing was measured, from memory bandwidth / weights read per token,
  calibrated on the models measured on the same device (rough: nothing measured there yet)

Warnings: a chat model served with less context per request than `conductor.min_context`, and a model whose loading would
unload another.

### Cloud models: Grok

When `grok` is installed and signed in, the picker adds a **Cloud (Grok, xAI)** group from `grok models` (cached for 10
minutes). Grok is never the default model. Picking a Grok model (`grok/<name>`) sends the session's turns to the `grok`
backend in the session's folder. Grok's own tools and permissions apply, not ABP's. A Grok session with cloud assist
`off` is refused rather than sent. These rows have no Load or Benchmark buttons.

The session's own switches set Grok's permission mode for each turn:

| Workspace switch | Grok mode |
|---|---|
| Agent type **Plan** | `plan` (proposes, changes nothing) |
| Autonomy Locked, Cautious, Supervised | `default` (manual approval) |
| Autonomy Trusted, Autonomous, Sovereign | `auto` (auto-build: edits run) |

`bypassPermissions` is never used. A Grok turn runs headless (`grok -p`), so it cannot stop to ask you: measured live,
`default` and `acceptEdits` both cancel the turn at the first tool call that would ask. Manual approval therefore means
Grok explains what it would do and changes nothing; switch to Trusted or higher to let it build.

### Grok in Factory runs

`conductor.grok.mode` is **auto** by default: confined Grok when network is approved and it can run, otherwise Grok as
advisor; on a machine without Grok it is skipped and the local worker runs alone. The top bar switches it on the fly:
the **Grok** dropdown (off, advisor, confined, auto) and the **Grok internet** switch (`conductor.grok.network_approved`,
off by default). Both are audited and both turn back off the same way.

## Remote bots as sub-agents

A workspace agent can work with the registered bots (Remote Bots):

- **`list_bot_instances`** (read-only) gives each bot's id, name, platform, backend, model and status, with no credentials.
- **`delegate_to_instance`** sends a bot a task. `conversation` keeps one session per caller, label and bot, for a multi-turn exchange. `files` inlines workspace files. `save_to` writes the reply to a file.
- **`delegate_to_bots`** sends up to 8 tasks to bots in parallel, each with its own result or error.

Write `@botname` in a message and the agent is told which bots you mean (case-insensitive; e-mail addresses are ignored).
Every delegation goes through the same permission gate, taint check, depth limit and audit row. A bot's own side effects
use its backend's approvals. A Grok-backed bot joins a workspace swarm this way, beside local agents and Claude-backed
bots.

## Config

```yaml
local_chat:
  instance_name: Local Chat
  default_model: ""        # "" = the loaded chat model, else the best measured local coder that fits the GPU
  default_mode: build
  default_autonomy: 2      # 0 Locked .. 5 Sovereign (applied to permissions in a later phase)
  cloud_assist: ask        # off | ask | auto
  catalog_ttl_s: 60
```

## Sessions and worktrees

Each session has a **status** (`working`, `needs_you` while an approval waits, `idle`, `error`, `done`) and **unread** (set
when a turn ends, cleared by `POST …/read`); changes go out as `local_chat_session` frames and are kept as events
(`GET /api/local-chat/events`) for the sidebar and notifications. Sessions can be put in user **groups**.

A new Build / Plan / Review session on a git workspace gets its **own worktree** (`local_chat.worktrees: auto`; pass
`"worktree": false` to skip): a new branch `<worktree_prefix>/<slug>-<id>` from the repository's current branch, in a short
folder outside the repository (`local_chat.worktree_root`, default a folder in the agent state folder). Turns work there;
the base checkout is never touched, and Build / Plan never work directly in the live ABP checkout.

| Action | Route (`/api/local-chat/sessions/{sid}/worktree…`) | Notes |
|---|---|---|
| status | `GET` | branch, base, ahead / behind, dirty files, conflicts |
| diff | `GET …/diff` | everything since the session forked from its base, untracked files included |
| sync | `POST …/sync` | merges the base in; refused with uncommitted work; conflicts are listed, the merge is left in progress, and `hand_to_agent` starts a turn that resolves them |
| commit | `GET …/commit-message`, `POST …/commit` | a drafted message the owner confirms |
| merge / push | `POST …/queue-merge` | into the conductor's push queue, awaiting the owner's approval; nothing merges or pushes by itself |
| keep / discard | `POST …/keep`, `POST …/discard` | discarding unmerged work answers 409 with what would be lost until `confirm` |

Archiving a session removes an unchanged worktree and keeps a changed one; deleting a session with unmerged work asks
first. `POST /api/local-chat/worktrees/prune` removes orphan worktrees without uncommitted work (and, with
`clean_merged`, the clean worktrees whose branch is merged into its base).

**Rewind**: every Build / Plan turn checkpoints the worktree (the shadow checkpoint store, never the repository's own git)
before its tools run. `POST …/rewind {"turn_id"}` puts the files back, marks that turn's and every later turn's messages
reverted (nothing is deleted) and checkpoints the state it left, so a rewind can be undone too.

**Steer and queue**: `POST …/steer {"text", "how": "steer"}` adds a message to the running turn at its next step;
`"how": "queue"` keeps it for the next turn, which starts by itself when the current one finishes cleanly.

## Linked sessions

From any session: **Fork** (`POST …/fork`) starts a new session with this conversation as context, in its own worktree
cut from this session's branch tip (committed work only; uncommitted changes are listed, not carried); **New task**
(`POST …/task {"brief"}`) starts a fresh session on a brief; **Hand off** (`POST …/handoff {"plan"}`) moves the
in-progress task to a new session with the remaining plan and the recent conversation. The child's first turn receives
that context. When a linked session ends a turn, a note with its recap lands in the parent (`local_chat_note` frame) and
the parent is marked unread; `GET …/children` lists them.

**Factory workers** appear as read-only linked sessions under the session that started the run: the conductor takes an
optional `on_event` observer, and `links.factory_observer(parent)` turns each task's start, review, acceptance or
escalation into the worker session's status and messages. A worker's worktree belongs to the conductor (confined); Local
Chat never changes or removes it.

## Agent and Factory in chat

**Autonomy 0-5** sets each turn's permission mode and run-level rules (`bot/localchat/autonomy.py`): 0 Locked (every call
asks), 1 Cautious (reads run, edits and commands ask), 2 Supervised (ABP's defaults), 3 Trusted (edits run), 4 Autonomous
(edits and the usual build / test / git-inspection commands run), 5 Sovereign (any single command runs). At every level
destructive or irreversible commands ask and `git push` is denied: pushing only happens through the push queue. A
host-locked permission config accepts only the deny / ask rules.

**Cloud assist**: `off` never leaves the machine (a cloud request is refused); `ask` adds "Ask the cloud" for one message
(`send {"cloud": true}`, recorded with model `cloud`); `auto` uses the cloud when the capacity governor is cloud-led.
Tokens are shown per message and per session (local / cloud); `GET …/context` also gives the context meter (estimated
tokens vs the tokens the hub serves per request).

**Factory mode**: the message is a goal; the conductor plans it and the plan arrives with a Run button. Run
(`POST …/factory/run`) uses the conductor's real worker with `shell="sandbox"` (commands only inside a Windows
AppContainer without network) and is refused, never degraded, without it; the first run in a repository needs
confirmation. Workers appear as linked worker sessions; the green integration waits in the push queue, and Approve in
the chat records the approval without pushing.

## Long sessions

Before a turn, when the conversation since the last compaction passes `local_chat.compact_at` (default 0.7) of the
tokens the hub serves the model per request (32k when it does not say), the older turns are summarized by the session's
model (no tools; an extract, labelled, when no model answers). A visible "Compacted" marker holds the summary; no message
is removed. The agent then continues on a fresh history (`localchat:<sid>:g<N>`) whose first turn starts from the summary
and the newest messages. `POST …/compact` does it on demand.

In Build / Plan / Review the agent is asked to end each turn with a task list and a next-step box (fenced `abp-steps` /
`abp-next` blocks); they are taken out of the reply text and shown under it, the answers as buttons
(`local_chat.next_step_box`). Chapters (`POST …/chapters`) are dividers with a jump list; `GET …/export?format=md|json`
downloads the session; `local_chat.auto_archive_days` archives quiet sessions (never pinned, working or waiting ones; a
worktree with unmerged work is kept).

## Offline and shared-GPU behaviour

Local Chat needs no network: catalog, sessions, worktrees, Build turns, Factory planning and the capability index all run
on this machine (`tests/test_localchat_offline.py` runs the flow with every non-local connection refused). When a turn
fails because ModelMistress is down or degraded, the error says so and what to do (start it from Modules; Retry). One GPU
is shared: a chat turn will not load a model that would unload the one a running Factory run uses (409, and the picker
warns); the Factory's own model, or one that fits beside it, is fine. The picker answers from its cache
(`local_chat.catalog_ttl_s`) without asking the hub again.
