# ABP Agents — parity roadmap

> **Status: P0 is built (its live-model measurement and surface adoption of streaming remain); everything after it is design.** This page is the
> agreed plan for bringing the native ABP agent (and the platform around it) to
> parity with the leading agent products. Each phase says what exists today and
> what does not, so nothing here should be read as a description of current
> behaviour. Update the status table as phases ship.

| Phase | What | Status |
|---|---|---|
| P0 | Foundations: eval harness, traces, `ToolSpec`, prompt builder, streaming | **Built** (see below for what remains) |
| P1 | Core coding toolset and a better loop | **Built** (interactive PTY and a stateful shell are not) |
| P2 | Permissions, sandboxing, prompt-injection defence, hooks v2 | **Built** (docker backend untested against a real daemon) |
| P3 | Context and memory | **Built** (no embeddings, no tree-sitter, no user model) |
| P4 | Agents, skills and commands | **Built** (no registry; git fetch untested against a real remote) |
| PM | Model knowledge, limits and usage (added later) | **Built** (API and chat only; no dashboard screen) |
| P5 | Code intelligence and developer surfaces | Design |
| P6 | Browser, computer use, routines (the Grok Bot pillar) | Design |
| P7 | Channels, devices and voice | Design |
| P8 | Learning and efficiency | Design |
| P9 | Compatibility and docs | Continuous |

## 1. What "ABP Agents" are today

Two different things share the name:

* **Delegating backends** — `cli` (Claude Code), `ui` (Claude Desktop),
  `hermes_cli`, `hermes_gateway`. ABP drives another agent; capability comes from
  that agent. They are the "premium engines" and stay.
* **The native agent** — `NativeAgentBackend` over the Anthropic, OpenAI-compatible
  and Responses transports (`bot/agent_runtime/`). This is where the gap is.

### Verified strengths

Control and safety (stop/queue/steer/pause/background, emergency stop, plan-first
gating, approvals, device tiers, admin-tool gate, hooks, shadow-git checkpoints with
rollback/undo/branch/worktree, one-hop provider failover); orchestration (batch
subagents with role-based tool stripping, mixture-of-agents consult, five swarm
strategies with a cost ceiling, delegation between bots, kanban, cron/loop/heartbeat);
integration (MCP client with OAuth and sampling, MCP server, approval-gated plugin
authoring); surfaces (desktop, Android, TUI, dashboard, Telegram, Discord, Slack,
Matrix, WhatsApp); vision/PDF input, prompt-cache telemetry, one-shot compaction.

### Verified gaps

| Area | Today | Baseline in peer products |
|---|---|---|
| Coding tools | `run_shell`, `read_file`, `write_file`, `list_dir`, `git_status`, `git_diff` only | surgical edit, patch, grep, glob, todo, notebook |
| Web / browser | Anthropic server tools only, off by default; no browser or computer use (`ui` only automates Claude Desktop) | search, fetch, headless browser, computer use |
| Shell | unsandboxed, 60 s timeout, 8 k output cap, no session/PTY/background | persistent or sandboxed shells, background jobs |
| Sandbox backends | local only | Docker, SSH, cloud sandboxes |
| Loop | sequential tools, hard cap of 20 iterations, no streaming | parallel tools, budgets, streaming |
| Context | char-count compaction once per turn; system prompt = memory + skills | mid-loop compaction, hierarchical project rules, repo map |
| Permissions | binary "dangerous → ask" | pattern rules, modes, persisted allows |
| Hooks | 4 events, no input rewriting | ~20 events |
| Memory | approved-facts summary | searchable sessions, user model |
| Skills | descriptive text only | SKILL.md standard with resources, registries, learning |
| Agent definitions | personas are metadata | markdown-defined named agents |
| Code intelligence | none | LSP, formatters, editor protocol |
| Voice | none | STT/TTS |
| Agent evals | none (Support Bot only) | continuous benchmarks |

## 2. Strategy

1. **Table stakes first, signature features second.** Do not clone feature for
   feature. Reach parity on the coding tools, permissions, sandbox, streaming and
   context, then adopt each peer's signature capability.
2. **Keep ABP's edge.** Multi-backend orchestration, phone/tablet/server surfaces,
   governance (estop, tiers, approvals), sidecar embedding with locked host policy.
3. **Two tracks.** Keep delegating to Claude Code and Hermes while the native agent
   matures; add `opencode` and `openclaw` delegating backends; put one permission,
   telemetry and policy layer around every engine.
4. **Open standards, not private formats.** MCP, AGENTS.md/CLAUDE.md, agentskills.io
   SKILL.md, ACP for editors, importers for `.claude/` and `.opencode/`.
5. **Measure.** The eval harness (P0) exists so that "parity" is a number.
6. **Reuse the CI/CD platform.** The event store is the trace store; the policy
   engine is the agent policy, so a host can lock tools, sandboxes and models.
7. **ML stays advisory.** Anything learned (routers, skill drafts) proposes; rules
   and humans decide. Same principle as `docs/cicd/README.md`.

## 3. Phases

Sizes are relative (S, M, L, XL), not calendar commitments.

### P0 — Foundations (M) — built

| Deliverable | State |
|---|---|
| Agent eval harness `abp_agenteval` (see [evals.md](evals.md)) — task = workspace fixture + prompt + deterministic graders; scripted (CI-safe) and live (opt-in) modes; JSON report; `--baseline` regression gate | Built. Live mode is implemented but **not yet run against a real model**; a comparison run against Claude Code is planned |
| Trace recording — every run, model call, tool call and approval into the tamper-evident store, shape only, with redaction; sub-agent runs link to their parent | Built. Not yet shown in a dashboard panel |
| Declarative `ToolSpec` — permission class, read-only / concurrency-safe flags, output ceiling; `register()` for schema + spec + handler in one place; built-in tools described without rewriting their handlers | Built. Spill-to-file for large output moves to P1 (it needs the search tools to read it back) |
| System-prompt builder — guidance, operator text, skills, memory, environment, session context, in cache-friendly order, each switchable | Built. Project files (AGENTS.md) are P3 |
| Streaming — `send_stream()` with a shared event model; Anthropic and OpenAI-compatible stream, the Responses transport delivers whole replies; the loop delivers events to `context["stream_notify"]` and signals a reset on failover | Built (transports + loop). Dashboard, desktop, Android and chat channels do not consume it yet, so users see no change today |

Acceptance: the seed suite scores 100 % in scripted mode in CI (met); every tool in
the schema list has a spec (met, enforced by a test); no existing test regresses
(met); a live run against one real model produces a comparable report (open).

### P1 — Core coding toolset and loop (L) — built, with two items deliberately left out

| Deliverable | State |
|---|---|
| `edit_file` (exact match, unique-match check, whitespace-insensitive fallback with re-indentation, closest-line hint, CRLF preserved, atomic write), `multi_edit` (all or nothing), `apply_patch` (unified diff, create / delete / rename, all or nothing) | Built |
| Read-before-write: an existing file can only be changed after the agent read it in this session, and not if it changed since | Built (`native_agent.require_read_before_write`) |
| `grep` (regex, path and glob filters, context, files / count modes, caps, time budget) and `glob` (newest first, vendored folders skipped) | Built, pure Python. No ripgrep acceleration: none is installed on the dev machine, so it could not be tested |
| `read_file`: offset / limit / line numbers, notebooks as cells, binary and image files reported, PDFs via optional `pypdf` | Built. The `pypdf` path is **untested** (the package is not installed here); images are reported, not shown to the model |
| `todo_write` / `todo_read` (per session, persisted) | Built |
| `web_fetch` and `web_search` (SearXNG, Brave, Tavily) | Built, **off by default** until P2's injection defences land. web_fetch is public-internet only, checks every redirect and the connected peer, caps the body, labels content untrusted. Search providers are tested against faked responses only, not live |
| Shell: per-call timeout, `cwd`, background jobs with `shell_output` / `shell_list` / `shell_kill`, process-tree kill on timeout / cancel / kill, large output saved in full inside the workspace | Built and tested on Windows. Untested on POSIX |
| Shell: interactive PTY; a shell that keeps `cd` and exported variables between calls | **Not built.** Each needs a platform-specific implementation that must be tested on each OS |
| Loop: parallel read-only tool calls (bounded), step / time / token limits that end a turn with a summary instead of an error, repeat and all-failing detection, cancellation that answers the open tool calls so the session stays valid, repair of sessions an older crash left dangling | Built |
| Cost budget (dollars) per turn | Not built: price lookup is a network call; token and time limits cover the need for now |
| Leaf sub-agents get the new tools | Built |
| Eval tasks for all of the above (17 in the suite) | Built |

Acceptance: eval suite edit / refactor / search / planning / shell tasks pass in scripted
mode (met, 17 of 17); no existing test regresses (see the commit); a live-model run
against the same tasks is still open (needs a provider key and your go-ahead to spend).

### P2 — Permissions, sandbox, security (L) — built; see [security.md](security.md)

| Deliverable | State |
|---|---|
| Rule engine: allow / ask / deny on tool, class, and a pattern on the command / path / host / query; modes plan, default, accept_edits, bypass; strict matching of shell allow-rules; per-bot rules; host lock | Built |
| Untrusted-content escalation: a session that read web or untrusted-MCP content cannot auto-run changes, ignores standing approvals, and cannot delegate without a person's approval | Built, enforced in the tool loop |
| Credential protection: redaction of secrets in tool output, refusal of outbound calls carrying a secret (also %-encoded), and a scrubbed environment for commands | Built |
| MCP: untrusted-by-default servers, per-server trust setting, tool-description pinning with an approval flow | Built |
| Sandbox interface with `local` (scrubbed environment) and `docker` (no network, limits, capabilities dropped, fails closed) backends | Built. **Docker tested against a stand-in `docker` program only**; no daemon was available |
| SSH, WSL, Windows job-object backends; network egress control for the local backend | **Not built.** Remote execution needs the file tools to run remotely too (P6's cloud computer); the local backend cannot restrict the network |
| Hooks v2: ten events, `updatedInput`, blocking `Stop`, `PreCompact` context, HTTP hooks | Built. Not built: async hooks, MCP-tool / prompt / agent hook types |
| API: `/api/agent/permissions`, `/api/instances/{id}/permissions`, `/api/mcp/pins`, `/api/agent/taint` | Built. No dashboard screen for them yet |
| `web_fetch` / `web_search` on by default | **Not done, on purpose:** they stay off until an operator enables them, even though the defences now exist |
| Eval: six security tasks that fail when their defence is removed | Built |

Acceptance: a page containing instructions cannot get a command run; a deny rule holds; a
credential never reaches output or a request; plan mode changes nothing (all met, as eval tasks
and unit tests).

### P3 — Context and memory (L) — built, with limits stated below

| Deliverable | State |
|---|---|
| Token estimate calibrated against each provider's own count; per-model context windows (table + `context_windows` override) | Built. There is no offline tokenizer, so counts are estimates that converge on the real ratio after a few calls |
| Mid-turn context management: old tool outputs cleared before each model call once the window is 70 % full, then summarised if still too full | Built (Anthropic and OpenAI-compatible transports; the Responses transport does not clear outputs) |
| Moving prompt-cache breakpoint on the last message (Anthropic) | Built |
| Fix: a long session used to return its *oldest* 200 messages, silently dropping the newest | Fixed (`list_agent_messages` returns the newest 2000) |
| AGENTS.md / CLAUDE.md / .claude/CLAUDE.md from the working directory, plus your own AGENTS.md, with `@imports` | Built. Labelled untrusted in the prompt; they cannot grant permissions. Nested per-folder files and parent folders are **not** read |
| `repo_map`: Python via `ast`; JS/TS, Go, Rust, Java/Kotlin/C#, Swift, Ruby, PHP via regular expressions; ranked by how often other files mention a file's symbols; token budget; cached | Built. **Heuristic, not tree-sitter** (not installed); no call graph |
| `code_search`: SQLite FTS5 index per workspace, incremental, identifier parts (`refresh_token`, `parseHttp`) searchable, BM25 ranking | Built. Keyword index only: **no embeddings**, so it will not link synonyms |
| `session_search`: FTS5 over earlier conversations of the same bot instance (never another instance's); cleared conversations disappear from it | Built |
| Typed memories (user, feedback, project, reference, fact), de-duplication (same words and numbers, so "port 8080" and "port 8081" stay separate), fading from the prompt with age unless re-confirmed, `/memory list / forget`, approval gate kept | Built. Fading never deletes; only a person deletes |
| Optional user model (Hermes/Honcho-style) | **Not built**; typed `user` memories are the nearest thing |
| Eval: `search_by_concept`, `orient_with_repo_map` | Built |

### P4 — Agents, skills, commands (M–L) — built; see [skills-and-agents.md](skills-and-agents.md)

| Deliverable | State |
|---|---|
| Markdown agent definitions (front matter: tools, model, mode, isolation, prompt) from `.claude/agents`, `.opencode/agent(s)`, `.abp/agents` and the user's folder; built-ins explore, plan, reviewer, general; `spawn_subagent` takes `agent`; `list_agents` | Built. A definition can only **narrow** a child (fewer tools, read-only, a model); it cannot grant anything. Not built: hooks inside a definition, agents in sub-folders |
| Git-worktree isolation for a sub-agent (`isolation: worktree`), removed if unchanged | Built and tested against real git. Background-child notifications already existed |
| SKILL.md packs with progressive disclosure (`read_skill`, `read_skill_file`), from `.claude/skills`, `.agents/skills`, `.abp/skills` and the user's folder | Built. `allowed-tools` is information only |
| Install from a git URL into **quarantine**: allowed hosts only, shallow clone, scan (pipe-to-shell, decode-exec, credential paths, symlinks, binaries, size), Ed25519 signature check, approval by a person only | Built. Tested with a faked `git clone`; **never run against a real remote**. A signature never rescues a blocked pack. No public registry or search |
| Skill learning: after a long task, one no-tools question; a clean answer becomes a **draft** a person approves or rejects | Built, **off by default**. Evaluating a draft before enabling it is not built (a person reads it) |
| Markdown custom slash commands (`$ARGUMENTS`, `$1`..`$9`) from `.claude/commands`, `.opencode/command(s)`, `.abp/commands`, the user's folder; work in chat and `/commands` lists them | Built |
| API `/api/skills/*`; `/skills fetch|quarantine|approve|reject|drafts|approve-draft|reject-draft` (admins) | Built. No dashboard screen |
| Eval: `follow_a_skill_pack`, `list_agents_then_delegate_read_only` | Built |

### PM — Model knowledge, limits and usage (M) — built; see [models.md](models.md)

Added at the owner's request: ABP must understand each model's context window, its request and token
allowance per minute and per day (especially for free models), and everything else an agent or a person
would want to know about a model.

| Deliverable | State |
|---|---|
| One lookup per `provider/model`: identity, context window, output limit, modalities, tool / reasoning / structured-output support, knowledge cutoff, release date, open weights, price (in, out, cache), free or not; each group records its source (override, curated, catalog, builtin) | Built (models.dev catalog, downloaded and cached; nothing new to install) |
| Published free-tier limits with source URL and date checked (`config/model_limits.yaml`): OpenRouter free, Groq free, Google's reset rule | Built. **Provider numbers change without notice**, they were read by an assistant summarising each provider's docs page, and most providers publish none - a missing figure is `None`, never "unlimited" |
| Your own limits and overrides (`native_agent.models.limits`, `.overrides`, patterns like `openrouter/*:free`) | Built |
| Usage counted for every call of every transport (persisted): requests and tokens per minute and per day, 429s, in-flight calls; calendar (UTC or Pacific) or rolling day windows; shared free-tier counters | Built |
| Provider headers (`x-ratelimit-*`, `anthropic-ratelimit-*`, `retry-after`) parsed and remembered | Built. Some providers send them only on errors; the Anthropic SDK path records them on errors only |
| Enforcement: wait briefly for a per-minute limit; refuse at once, with the reset time in your time zone, for a daily limit / 429 / concurrency cap; the loop fails over to the fallback model with no wasted call; used-up free models skipped when picking swarm models | Built and tested with fake transports. **Not exercised against a real provider's 429** |
| Agent tools `model_info`, `find_models`; a stable prompt line about its own model; `/modelinfo` (`/limits`); `/api/models/*`; MCP `get_model_info`, `get_model_usage`, `find_models` | Built |
| Context windows from the catalog (smaller of catalog and table for Claude) | Built |
| Dashboard and desktop screens for usage and limits | **Not built** (API only) |
| Eval: `know_your_allowance` | Built |

### P5 — Code intelligence and developer surfaces (L)

* LSP client (diagnostics fed back after edits, symbols, references), formatters on
  write.
* ACP server so editors can use ABP as an agent; later a VS Code extension.
* Headless `abp run` (JSON output, exit codes), OpenAPI spec with Python and TS
  SDKs, GitHub Action for PR review, session share/export.
* `opencode` and `openclaw` delegating backends.

### P6 — Browser, computer use, routines (XL)

* Playwright browser tool using accessibility-tree snapshots, screenshots as
  fallback, persistent per-agent profiles.
* Shared cloud computer: a container with browser and desktop, live view in desktop
  and Android, pause / take over / return control.
* Credential vault with human hand-off for passwords, 2FA and CAPTCHAs; the agent
  never handles them.
* Routines: record a workflow once, generalise it into a parameterised routine,
  schedule it, keep run history and an active toggle (built on scheduler + kanban).
* Approvals as first-class objects: push notification, diff preview, approve/deny
  from Android.
* Native computer-use tool for models that support it.

### P7 — Channels, devices, voice (L)

* Signal, iMessage (bridge), Google Chat, Teams, email, SMS; per-channel policy and
  pairing.
* Android as a node (camera, screen, location, notifications, device-local
  actions) with per-capability consent.
* Local STT, TTS, voice memos, push-to-talk; wake word later.
* Proactive heartbeat and a Canvas-style live artifact surface.

### P8 — Learning and efficiency (M)

* Trajectory export and compression for evals and optional fine-tuning.
* Small advisory router that picks a cheap or strong model per task class.
* Prompt and tool-description tuning driven by eval scores.

### P9 — Compatibility and docs (S, continuous)

* Importers for Claude Code, OpenCode, Hermes and OpenClaw configuration.
* Install, embedding and policy docs; public benchmark page; plugin SDK versioning.

## 4. Order

P0 → P1 → P2 → P3 → P4, then P5, P6 and P7 in parallel where possible, then P8, with
P9 throughout. P0–P2 come first because a sidecar deployment needs sandboxing,
locked policy and traces before it needs more features. Cheap wins alongside them:
the `opencode` and `openclaw` delegating backends.

## 5. Risks

* **Prompt injection and tool poisoning** grow with every reach the agent gains
  (web, browser, MCP). P2 is a hard prerequisite for P6.
* **Scope.** Each pillar is a product on its own. The eval harness is how to stop.
* **Licensing.** Check each peer's licence before reusing anything. Parity means
  matching behaviour and standards, not copying code; Grok Bot is proprietary.
* **Drift.** Peer projects move quickly; re-verify their feature lists before each
  phase.

## 6. Sources (checked when this plan was written)

Grok Bot mobile docs (docs.x.ai/grok-bot/mobile) and its Google Play listing;
OpenCode repository and docs; OpenClaw repository; Hermes Agent repository. Only
README/docs-level information was read — no peer source code. Details beyond those
pages (for example Claude Code's hook events) come from prior knowledge and should
be spot-checked before they drive a decision.
