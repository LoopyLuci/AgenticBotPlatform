# ABP Agents — parity roadmap

> **Status (2026-09-20): every phase P0-P9 has been built as far as it can be verified on one Windows machine with no live model, no real
> third-party accounts and no phone app work. "Built" in the table means code plus tests; the ledger below says what has and has not been run
> against the real thing.** This page is the plan and the record: each phase says what exists and what does not, so it is not a description
> of behaviour you can assume. Nothing has been pushed or released.

> **What has never run against the real thing:** a live model on the eval suite (so no measured comparison with any other product); Docker with a
> daemon; a real git remote (skill fetch); Zed or any ACP editor; pyright and typescript-language-server (rust-analyzer *was* used); a GitHub
> runner; the real OpenCode and OpenClaw programs; Gmail / a real mailbox, Twilio, a Signal bridge, BlueBubbles; a real Whisper service or Piper;
> real websites in the browser tool (a real Edge against local pages *was* used); Firebase push; and the Android app, which implements none of
> the new node, approval or canvas surfaces. **Not built at all:** the shared cloud computer, native computer use, Google Chat, Teams, Hermes /
> OpenClaw importers, a VS Code extension, dashboard screens for traces / permissions / usage / vault / routines, and consumption of streaming
> by the UIs.

| Phase | What | Status |
|---|---|---|
| P0 | Foundations: eval harness, traces, `ToolSpec`, prompt builder, streaming | **Built** (see below for what remains) |
| P1 | Core coding toolset and a better loop | **Built** (interactive PTY and a stateful shell are not) |
| P2 | Permissions, sandboxing, prompt-injection defence, hooks v2 | **Built** (network egress control for local/windows_job is investigated, not built - see security.md) |
| P3 | Context and memory | **Built** (no embeddings, no tree-sitter, no user model) |
| P4 | Agents, skills and commands | **Built** (no registry; git fetch untested against a real remote) |
| PM | Model knowledge, limits and usage (added later) | **Built** (API and chat only; no dashboard screen) |
| P5 | Code intelligence and developer surfaces | **Built** (none of it run against Zed, pyright, a GitHub runner or the real OpenCode / OpenClaw; no VS Code extension) |
| P6 | Browser, computer use, routines (the Grok Bot pillar) | **Partly built** (browser, vault, routines, approvals; not the cloud computer or computer use) |
| P7 | Channels, devices and voice | **Partly built** (four new channels, node protocol, voice, canvas - all against fakes; no Google Chat / Teams; Android does not implement nodes) |
| P8 | Learning and efficiency | **Built** (the model router now auto-selects and auto-fails-over, not advisory-only; it and the tuning harness have never had a live model to measure) |
| P9 | Compatibility and docs | **Partly built** (no Hermes / OpenClaw importers; no public benchmark) |

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
| Sandbox backends: `local` (scrubbed environment), `docker` (no network, limits, capabilities dropped, fails closed), `ssh` (a configured remote host), `wsl` (a WSL2 distro on the same machine), `windows_job` (a real Win32 Job Object, guaranteed tree-kill, no container) | Built. **Docker and WSL verified against the real daemon / a real registered distro** (`tests/test_sandbox_live_docker.py`, `tests/test_sandbox_wsl.py::TestLiveWsl`); `windows_job` verified against the real Win32 API (needs no external service); `ssh` verified against a fake stand-in plus an opt-in live class for a real host (`ABP_TEST_SSH_HOST`) |
| Network egress control for the `local`/`windows_job` backends | **Not built - investigated, not half-built.** A per-command Windows Firewall rule needs this process to run elevated (rejected on purpose, see `firewall.py`); a real non-elevated block needs an AppContainer, which needs bypassing Python's subprocess/asyncio process-creation plumbing - a separate, larger piece of work. `ssh`'s remaining honest limit: only the command runs remotely, not the file tools (that's the "cloud computer" of P6) |
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

### P5 — Code intelligence and developer surfaces (L) — built; see [developer-surfaces.md](developer-surfaces.md)

| Deliverable | State |
|---|---|
| LSP client: diagnostics shown to the agent after every edit, and an `lsp` tool (diagnostics, symbols, definition, references, hover) | Built, off until configured. Tested against a stand-in server and **by hand against a real rust-analyzer**, which showed that modern servers answer diagnostics on request and refuse while loading (both handled). **Not run against pyright or typescript-language-server** |
| Formatters on write (no shell, scrubbed environment, timeout; the agent's "I read this" record is refreshed) | Built, off until configured. Tested with a stand-in formatter; no real formatter (ruff, prettier) is installed here |
| Headless `python -m abp_run` (ephemeral by default, JSON output, exit codes, approvals denied unless allowed) | Built |
| ACP server `python -m abp_acp` for editors | Built and tested with a stand-in client and a real pipe. **Never run against Zed** or another real editor |
| OpenAPI document (`docs/api/openapi.json`, kept current by a test) with a Python client and a generated JavaScript client with type declarations | Built. Python client tested in-process, JavaScript client against a live server. Not published to PyPI / npm |
| GitHub Action that reviews a PR read-only and keeps one comment updated | Built. Tested with a real git repository, a scripted model and a faked GitHub API. **Never run on a GitHub runner** |
| Conversation export (`/export`, `/api/agent/sessions/<key>/export`; Markdown or JSON; secrets removed) | Built. **No hosted "share link"**: ABP has no public server to host one |
| `opencode` and `openclaw` delegating backends | Built, tested against a stand-in program only (neither is installed). ABP's own permissions and traces do not apply inside them; output parsing is a best reading of their docs |
| VS Code extension | **Not built** |
| Eval: `fix_what_the_language_server_reports` | Built |

### P6 — Browser, computer use, routines (XL) — partly built; see [browser-and-routines.md](browser-and-routines.md)

| Deliverable | State |
|---|---|
| Browser tools (`browser`, `browser_act`, `browser_handoff`) on Playwright: numbered-element snapshots, persistent per-profile logins, every request checked against the public-internet rules, downloads refused, page content marks the session untrusted | Built, off by default. **Tested with a real Microsoft Edge against local pages** (including a proof that a page cannot reach a non-allowed address); **not tested on real websites**. Screenshots are saved as files, not shown to the model; shadow DOM, canvas and frames are not covered |
| Credential vault (`python -m bot.vault`) with the agent never seeing a secret: filled only into the site an entry belongs to, redacted from output, blocked from leaving in requests; TOTP codes | Built. RFC 6238 vectors pass. **The key file sits beside the vault unless `ABP_VAULT_KEY` is set**, so it protects against backups and casual reads, not against someone who owns the machine |
| Human hand-off: `browser_handoff` is always put to a person (also in bypass mode); the agent refuses to type passwords, card numbers and one-time codes | Built. A person can act in the page only with `headless: false` |
| Routines: the agent writes a parameterised template after doing a task (`routine_save`); `/routine run / schedule / pause / resume / history / delete`; runs recorded in history | Built on the existing scheduler. **Not a click recorder**: a routine is a prompt, not a replay. No dashboard screen |
| Approvals as objects: `/api/approvals` with a diff / command preview, deciding from any surface, standing grants only with the dashboard token, a phone push notification when one is created | Built and tested. **Push is untested against a real Firebase project** and the **Android app does not show them yet** |
| Shared cloud computer (container with a browser and desktop, live view, take-over) | **Not built** - needs infrastructure this repository does not have |
| Native computer-use tool | **Not built** - the transports do not carry images in tool results, and there is no display to control |
| Eval: `handoff_reaches_a_person_even_in_bypass_mode`, `save_a_task_as_a_routine` | Built |

### P7 — Channels, devices, voice (L) — partly built; see [channels-and-devices.md](channels-and-devices.md)

| Deliverable | State |
|---|---|
| E-mail, SMS (Twilio), Signal (signal-cli bridge) and iMessage (BlueBubbles) channels, with per-channel allow-lists, forged-sender protection for e-mail, Twilio signature checking, and forms in the dashboard, desktop app and terminal UI | Built. **Tested against fakes only** (in-process IMAP/SMTP servers, a fake Twilio, bridge and BlueBubbles); never run against a real mailbox, Twilio account, Signal bridge or Mac. Twilio's signature algorithm reproduces the example in Twilio's own documentation |
| Google Chat, Microsoft Teams | **Not built** - they need Google / Microsoft sign-in flows that cannot be tested here |
| Paired phones as nodes (camera, screen, location, clipboard, notification) with per-capability consent that starts at deny, `node_invoke` / `node_list` tools, long-poll protocol, push wake-up | Server half built and tested over real HTTP with a reference node. **The Android app does not implement it**: no real phone has answered a command |
| Speech to text (OpenAI-compatible endpoint such as Groq's free Whisper, or your own command) and text to speech (endpoint, Windows speech, or command); Telegram voice messages transcribed and optionally answered by voice | Built. Windows speech verified producing a real WAV; **not tested against a real Whisper service, Piper or whisper.cpp**. Voice on the other channels and in the apps is not done; no wake word, no push-to-talk |
| Canvas: a live page the agent draws, in a no-network sandbox behind signed links | Built and tested. A web page only; not embedded in the desktop or Android apps |
| Proactive heartbeat | Already existed (`/heartbeat`); nothing added |
| Eval | None added: these are integrations, checked by their own tests |

### P8 — Learning and efficiency (M) — built; see [learning-and-compatibility.md](learning-and-compatibility.md)

| Deliverable | State |
|---|---|
| Trajectory export (`python -m abp_trajectory`): finished runs as JSONL chat transcripts with tool calls, secrets removed, tool output shortened, optional PII masking, duplicates dropped; opt-in with `--confirm` | Built and tested. Not "compression" in the model sense; it shortens and de-duplicates. Nothing scores whether an answer was right |
| Model router (`/route`, `suggest_model`): classifies the task, filters by ability and remaining allowance, ranks on quality / economy / headroom | Built, **and no longer advisory-only.** A `native_agent` bot with no model (or `model: auto`) has it actually pick and run — the top-ranked candidate the first time that bot is used, never an Anthropic model unless `native_agent.router.candidates`/`.also` explicitly lists one (see `docs/agents/models.md`). `native_agent.router.auto_failover` turns a transport failure into a bounded, router-driven retry chain instead of stopping after the one static per-bot fallback model. Surfaced on the ABP Agents page's Models tab (settings + a live "what it would pick right now" panel), not YAML-only. Quality is still a measured eval pass rate when one was recorded (`abp_agenteval run --live --record`), otherwise a labelled guess from the catalog — no live model has been evaluated yet, so every recommendation still rests on the guess until that changes. **Honest limit:** auto-select classifies once, on a bot's first turn, and stays on that pick after that turn (not re-classified fresh on every single message) |
| Prompt and tool-description tuning driven by eval scores: `abp_agenteval compare` runs the suite per variant of `prompt.extra` / `tool_descriptions` | Built and tested for its mechanics. **Never run with a live model**, which is the only way it produces evidence (scripted runs cannot tell variants apart, and it says so); a difference of two tasks or fewer is reported as noise |

### P9 — Compatibility and docs (S, continuous) — partly built; see [learning-and-compatibility.md](learning-and-compatibility.md)

| Deliverable | State |
|---|---|
| Importers: Claude Code (`settings.json`, `settings.local.json`, `.mcp.json`) and OpenCode (`opencode.json(c)`) into permission rules, hooks and MCP servers; dry run by default; never widens permissions | Built and tested against sample files written from the documented formats, **not against real installs**. **Hermes and OpenClaw importers not built** (formats not checked) |
| Plugin SDK versioning: `SDK_VERSION`, `REQUIRES_SDK` / `PLUGIN_VERSION`, a refusal that names both versions | Built. See [plugin-sdk.md](plugin-sdk.md) |
| Results page (`abp_agenteval page`) and a committed one | Built. The committed page shows a **scripted** run and says it is not a model comparison; no live run and no peer product has been evaluated, so there is no public benchmark yet |
| Docs: this index ([README.md](README.md)), developer surfaces, models, browser and routines, channels, learning and compatibility, plugin SDK, security, evals | Written. Install and embedding docs for the agent specifically are in [developer-surfaces.md](developer-surfaces.md); the platform-level ones already existed |

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
