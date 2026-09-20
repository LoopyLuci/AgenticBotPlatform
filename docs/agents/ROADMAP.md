# ABP Agents — parity roadmap

> **Status: P0 is built (its live-model measurement and surface adoption of streaming remain); everything after it is design.** This page is the
> agreed plan for bringing the native ABP agent (and the platform around it) to
> parity with the leading agent products. Each phase says what exists today and
> what does not, so nothing here should be read as a description of current
> behaviour. Update the status table as phases ship.

| Phase | What | Status |
|---|---|---|
| P0 | Foundations: eval harness, traces, `ToolSpec`, prompt builder, streaming | **Built** (see below for what remains) |
| P1 | Core coding toolset and a better loop | Design |
| P2 | Permissions, sandboxing, prompt-injection defence, hooks v2 | Design |
| P3 | Context and memory | Design |
| P4 | Agents, skills and commands | Design |
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

### P1 — Core coding toolset and loop (L)

* `edit_file` (exact replace, uniqueness check, fuzzy fallback, read-before-write
  staleness check), `multi_edit`, `apply_patch`.
* `grep` (ripgrep with a Python fallback), `glob`.
* `read_file` with offset/limit/line numbers, images, PDFs, notebooks.
* `todo` / plan-tracking tool.
* Client-side `web_fetch` and `web_search` (pluggable provider, readability
  extraction, SSRF guards).
* Shell: persistent sessions, PTY, background processes with tail/kill, per-call
  timeout, cwd tracking, output spill.
* Loop: parallel read-only tool calls, budgets (tokens/cost/time) instead of a fixed
  20, no-progress detection, structured error recovery, mid-turn cancellation with
  partial results kept.

Acceptance: eval suite edit/refactor tasks reach the agreed target with no
regression in existing tests.

### P2 — Permissions, sandbox, security (L; before broad sidecar use)

* Rule engine: allow/ask/deny on tool, argument pattern and path; modes (plan,
  default, accept-edits, bypass); persisted per-agent and per-project allows; host
  policy can lock any of it.
* Sandbox interface: restricted local, Docker, SSH, WSL and Windows job objects;
  Modal/Daytona later. Network egress policy; secrets injected, never transcribed.
* Prompt-injection defence: untrusted-content tagging for web and MCP results,
  trust levels per MCP server, confirmation before a tainted context triggers a
  dangerous action, tool-description pinning against tool poisoning.
* Hooks v2: ~12 events (`PostToolUseFailure`, `Stop`, `SubagentStop`, `PreCompact`,
  `Notification`, `SessionEnd`…), `updatedInput`, HTTP hooks.

### P3 — Context and memory (L)

* Real token counting, per-model windows, compaction at thresholds mid-loop, old
  tool-result clearing, cache breakpoints.
* Hierarchical AGENTS.md/CLAUDE.md loading with `@imports`.
* Repo map (tree-sitter) and optional code index (FTS5, embeddings later).
* Session-search tool (FTS5); typed memories (user, feedback, project, reference)
  with dedupe, decay and a review UI that keeps the approval gate; optional user
  model, off by default.

### P4 — Agents, skills, commands (M–L)

* Markdown agent definitions (frontmatter: tools, model, permissions, prompt),
  compatible with `.claude/agents` and OpenCode agents; built-ins explore, plan,
  build, general, reviewer; named subagent invocation with isolated context,
  worktree isolation and background notifications.
* SKILL.md standard with progressive disclosure, bundled scripts/resources and
  allowed-tools; install from a git URL or registry with security scan and
  signature check.
* Autonomous skill learning: draft into a review queue and evaluate before
  enabling. Nothing self-installs.
* Markdown custom slash commands.

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
