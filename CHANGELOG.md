# Changelog

All notable changes to AgenticBotPlatform are documented here. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versioning
follows [Semantic Versioning](https://semver.org/) for the desktop
app's own version (the Android app versions independently — see its own
`versionName`/`versionCode` in `android-app/app/build.gradle.kts`).

## [Unreleased]

### Added
- **Every bot now has a real backend-level fallback, OpenCode then Hermes CLI, never
  Claude.** Until now, only a bot with its own per-instance `action_overrides` entry got
  a backup chain if its backend failed — an ordinary bot with just its own `backend` set
  (the common case) had no fallback at all. `config/backends.yaml`'s new top-level
  `backend_backup: [opencode, hermes_cli]` is now appended to every resolved routing
  chain (`Router.resolve_chain`/`_with_global_backup`), so a down/misconfigured/
  out-of-quota primary backend now falls through to OpenCode, then Hermes CLI, before
  giving up — matching the standing "never Claude by default" instruction (a fallback
  the router reaches for on its own follows the same rule a default does). See
  `README.md`'s "How routing works".
- **ABP never uses Claude by default now — a bot picks a free model automatically instead.**
  `default_backend` and the `quick_question`/`project_task` routes used to go straight to the
  Claude Code CLI; they now go to ABP's own agent loop (`native_agent`) with model `auto`, which
  asks the model router (`bot/model_router.py`, previously advisory-only) to actually pick and run
  a real model — the top-ranked free candidate, never Anthropic unless you explicitly list one
  under `native_agent.router.candidates`/`.also`. A bot's Add-bot form has a new **Auto** button
  next to the model field for the same thing. If a turn's model fails, the existing one-hop
  fallback-model retry can now optionally (`native_agent.router.auto_failover`, off by default)
  keep trying more router-ranked models instead of giving up after one. The ABP Agents page's
  Models tab gained a **What ABP would pick right now** panel and real settings for all of this
  (`router.enabled`, `.candidates`, `.also`, `.auto_failover`, `.max_failover_hops`) — previously
  YAML-only. See `docs/agents/models.md`'s new "Automatic model routing" section.
- **Three more sandbox backends for `run_shell`, and the docker/WSL ones verified against the real
  thing.** `native_agent.sandbox.backend` now also accepts `ssh` (a configured, already-trusted
  remote host), `wsl` (a WSL2 distro on the same machine, workspace path translated automatically),
  and `windows_job` (a real Win32 Job Object confining the whole process tree, no container needed,
  Windows-only). All fail closed the same way the existing `docker` backend does: if the required
  tool or config is missing, the command is refused, never quietly run on the host instead. The
  `docker` backend was, until now, only ever tested against a stand-in `docker` program - it's now
  also verified against a real, running daemon (a real container runs the command, `network: none`
  genuinely blocks an outbound connection, a timeout stops and removes the real container). See
  `docs/agents/security.md`'s Sandbox section for each backend's honest limits, including the two
  gaps investigated but not built this round (network confinement for `local`/`windows_job` - it
  would need either running this process elevated, which the app deliberately never does, or an
  AppContainer rewrite of process spawning, both out of scope for this pass).
- **ABP Agent is now the first backend when you add a bot, and the form has its settings.** The Add / Edit a bot form used to
  bury ABP Agent at the bottom of a "Custom / local" group, default to the Claude CLI, and offer no agent settings. ABP Agent
  is now first (and the default for a new bot), the choices are labelled by what they do, and choosing ABP Agent, `api` or
  `custom_model` shows an **ABP Agent settings** panel: permission mode, sub-agents at once, sub-agent model and effort,
  manager effort, a fallback model, and plan approval, with the global defaults it starts from summarised. It saves with the
  bot, and editing a bot loads only what that bot set itself. `GET /api/agent-settings` gained `?own=true`.
- **An ABP Agents page, in the dashboard and the desktop app.** Until now ABP's own agent had no settings screen: its ~60
  settings could only be changed by editing `config/backends.yaml`, and permissions, skills, sub-agent limits and the tool
  list had no GUI. The new page has an Overview with readiness checks and the bots that run an ABP agent, and tabs for
  Runtime, Safety, Tools, Sub-agents & swarms, Skills and Models. Every setting has a label, help, its default and a reset;
  changes are staged and validated together and saved without losing the config file's comments. Bots on an agent backend
  get an **Agent settings** button on their card, and the Add-bot form explains the **ABP Agent** backend. Also new: a
  tool inventory, per-bot permission modes (with "follow the default"), reviewing skill packs and drafts, and installing a
  skill pack from git. Routes: `/api/agent/config[/schema|/reset]`, `/api/agent/overview`, `/api/agent/tools`. The per-bot
  agent-settings card moved here from Automation. See `docs/agents/agents-page.md`.
- **Removing a model provider is no longer final.** Every provider ever configured is kept in a provider store
  (`data/provider_store.db`, its own file so a snapshot restore cannot swallow it). Remove moves a provider to a new
  **Deleted providers** list on the Models page (dashboard and desktop app) with its address, settings and API key, the
  key stored encrypted with the vault key and never returned by the API. Restore puts it back as it was, with its
  per-model on/off choices, and accepts a replacement key; Forget deletes the copy for good. A provider that
  disappears by a hand edit or a restored snapshot is kept too, and
  `python -m bot.provider_store recover --from <state folder>` rebuilds removed providers from an install's config
  history (without their keys). New routes: `GET /api/providers/store`, `POST /api/providers/store/{name}/restore`,
  `DELETE /api/providers/store/{name}`.

### Changed
- **Chat opens in "Chat with Bot"** (it was "Send from Server") in the dashboard, the desktop app and the Android app. Send from
  Server is one click away.
- **The top-bar "Bot online" pill now shows live bot activity, and "Hot-reload armed" is gone.** The pill reads, for
  example, "2 bots running agents · 4 agent jobs · 1 queued": the number of bot instances with at least one running job
  right now (jobs with no instance count together as one default bot), the running and queued jobs, and, when idle,
  how many bots are enabled. Hover for the per-bot breakdown and tokens today. `GET /api/overview` gained
  `bots_running_agents`, `active_bots` and `bots_enabled`.
- **The dashboard token is never asked for, anywhere.** The "Set token" button and the paste dialog are gone from the
  dashboard and the desktop app, the setup wizard no longer has a token field (or a Generate button, or the
  `/api/setup/generate-token` route), and the terminal wizard generates it silently. The server places the
  auto-generated token in the dashboard page for a plain page load from this machine only (loopback client and Host,
  no forwarding headers, no `Origin`, no cross-site fetch); any other route gets no token and a one-line notice.

### Fixed
- **The desktop window no longer sits on "Starting the bot process…" forever.** Two causes. The boot panel kept its "Starting…"
  text and a spinning indicator after the server was up (only the small pill said "running"), so any time the panel was
  opened it looked like loading never finished; it now ends in "Ready." with the spinner stopped. And the readiness check
  polled every 500 ms with an attempt counter; it now starts at 100 ms with a time budget, and a failing setup step can no
  longer stop it from ever checking the server.
- **Start-up is faster and no longer waits on the bots.** The dashboard now starts before external MCP servers and chat
  platforms connect (a slow or unreachable bot used to delay the whole UI), and the Support Bot's neural net is no longer
  trained on every start: it loads its saved model, or trains once on first use / in a background warm-up. The dashboard
  answers about 3 seconds after launch, down from about 5.
- **The "Agentic Bot Platform running" pill no longer sits on top of the Terminal/Activity bar.** It is anchored to the
  bar's live height, so it stays just above it when the bar is collapsed, open, drag-resized or maximized, and follows
  window resizes.
- **A config reload no longer writes provider API keys into the log and audit trail.** The change summary only hid a
  key named exactly `secret`, so adding a provider printed its `api_key` into `logs/bot.log`, `config_history` and the
  audit log, which the API serves back and support bundles include. Any secret-named key is now masked at any depth.
- **The Terminal/Activity bar is now the bottom of the window.** It used to
  float (`position:fixed`) over a page that scrolled as a whole, with a
  padding guess to keep content clear of it, so content near the bottom and
  the lower items of the sidebar could end up underneath it. The window is
  now a real layout — the sidebar and the content each scroll inside their
  own space, and the bar is the last row — so nothing can be hidden behind
  it at any window size or in any bar state (collapsed, open, dragged,
  maximized). Jump-to-section links and toast messages were updated to match.
  Checked live at 375, 414, 768, 960, 1366, 1920 and 2560px wide and down to
  480px tall, in both the dashboard and the desktop UI.
- **Layouts that overflowed the screen** (found while testing at phone
  width): the terminal bar's own header was ~430px wide and widened the whole
  page; `<select>` menus sized themselves to their longest option; form rows
  couldn't shrink or wrap; tables and long unbreakable strings (e.g.
  `PreToolUse/PostToolUse/…`) spilled out and were silently clipped by
  `content-visibility`. The desktop UI's Chat header also sat 24px past the
  edge at the app's own minimum window width, and its 280px list pane left
  ~95px for the conversation on a phone. All now wrap, shrink or scroll
  inside their own space.

- **Android: an unreachable server no longer hangs or shows raw errors.**
  Found by testing on a real tablet. With the server down, Settings spun for
  50s+, Server Chat sat on "Loading…" and then printed a raw exception, Chat
  printed the exception with no way to retry, and Devices claimed "No other
  devices paired yet." Every screen now shows a plain explanation with a
  Retry button (loading / error / empty are distinct states), and once every
  saved address has failed, further requests fail immediately for a few
  seconds instead of each repeating the ~20s failover cycle. LAN/loopback
  addresses use a 3s connect timeout so a dead one doesn't delay the next.
- **Android: the app could adopt the wrong server.** After pairing it
  overwrote its working address with whatever the server *reported* about
  itself (a LAN IP it wasn't listening on, a Funnel URL), and mDNS discovery
  would adopt any ABP on the network — on a test network it switched to an
  unrelated machine and would have sent the API key there. Addresses are now
  only adopted if they answer as the server this device paired with (see
  Added), and cleartext identity probes carry no credentials.
- **Android: the bottom-navigation label "Server Chat" wrapped** in portrait
  on narrow screens; labels are now single-line and hidden on unselected tabs
  on narrow widths.

### Fixed
- **v0.7.28 first shipped the wrong installer** (a leftover `0.7.99` build
  from a local repro, which sorted after `0.7.28`); the release script took
  the alphabetically last installer and its verification compared the file
  with itself. The release was repaired minutes after publishing (correct,
  signed installer attached; the wrong assets removed and read back). The
  script now selects exactly this version's installer, reads the version the
  installer identifies itself as and refuses a mismatch, and fails if the
  published release carries any other installer.

### Added
- **ABP Agents parity roadmap and its first phase (P0, `docs/agents/ROADMAP.md`).**
  The roadmap records what the native agent has and lacks against Claude Code,
  OpenCode, Hermes, OpenClaw and Grok Bot, and the phased plan to close the gap.
  P0 lays the foundations the rest depend on:
  - **Agent eval harness** (`python -m abp_agenteval list | run`): tasks with a
    workspace fixture, a prompt and deterministic graders (file contents, a command
    that must pass, reply text, which tools the trace shows were used). Scripted mode
    replays golden trajectories so it is free and deterministic (the seed suite runs in
    the test suite on every push); `--live --provider P --model M` measures a real
    model; `--baseline` turns a report into a regression gate.
  - **Agent traces**: every native-agent run, model call, tool call and approval is
    recorded (shape only - never prompts, replies, file contents or tool output) in
    the same tamper-evident store the CI/CD platform uses, with secret redaction.
    Sub-agent runs link to their parent. Off with `ABP_AGENT_TRACE=0` or
    `native_agent.trace.enabled: false`.
  - **Tool specs**: every built-in tool now declares what it is (read, write,
    execute, agent, config, admin), whether it is read-only and safe to run alongside
    others, and how much it may return. Plugin and MCP tool output is now capped
    (it was unbounded). New tools register schema, spec and handler in one place.
  - **System-prompt builder** (`native_agent.prompt`): built-in operating guidance
    (verify before claiming done, respect the workspace boundary and denials, treat
    tool and web text as data), an environment block (OS, working directory, git,
    date), then skills and memory - ordered so the cached prefix survives. Each part
    can be switched off, and operators can add their own text.
  - **Streaming**: transports gain `send_stream()`; Anthropic and OpenAI-compatible
    stream reply text as it is generated, others deliver it whole. A turn given
    `context["stream_notify"]` receives the events; on failover it is told to discard
    what it showed. Dashboards, the desktop app, Android and chat channels adopt it next.
- **Learning, efficiency and compatibility (roadmap P8-P9).** `python -m abp_trajectory export` writes finished runs as redacted JSONL
  transcripts (opt-in, `--confirm`); `/route` and a `suggest_model` tool rank your models for a task (advice only; quality is a
  measured eval pass rate when one was recorded, otherwise a labelled guess); `python -m abp_agenteval compare` runs the evals per
  prompt / tool-wording variant and `page` writes a static results page; `python -m abp_import claude-code|opencode` turns another
  product's permissions, hooks and MCP servers into ABP's (dry run by default, never widens permissions); plugins can declare
  `REQUIRES_SDK` against a versioned SDK. No live model has run the evals yet, so nothing here compares models or products.
- **Channels, paired-phone nodes, voice and a canvas (roadmap P7).** Four new bot platforms - e-mail (IMAP/SMTP; only
  authenticated mail from allowed senders is answered), SMS through Twilio (signature-checked webhook), Signal through a
  signal-cli REST bridge, iMessage through BlueBubbles - selectable in the dashboard, desktop app and terminal UI. Paired
  phones can be asked by the agent for a photo, the screen, the location or the clipboard (`node_invoke`), with consent per
  capability starting at "deny"; only the server half exists, the Android app does not implement it yet. Telegram voice
  messages are transcribed (Groq/OpenAI-compatible Whisper endpoints or your own command) and can be answered by voice; a
  `canvas_update` tool draws a live page in a no-network sandbox. All of it tested against fakes only; see
  `docs/agents/channels-and-devices.md`. Adding a new channel is now: one adapter module plus a guide entry.
- **Browser, stored logins, routines and approvals (roadmap P6).** An optional browser for the agent (Playwright;
  off by default): it reads pages, clicks and fills forms, treats everything a page says as untrusted, checks every
  request the page makes against the public-internet rules, never types passwords or codes itself, and hands
  CAPTCHAs, codes and payments to a person (`browser_handoff`, which is asked every time even in bypass mode). A
  small encrypted vault (`python -m bot.vault`) holds logins that are filled into their own site without the model
  seeing them. `/routine` runs and schedules tasks the agent saved as parameterised routines. `/api/approvals`
  shows what the agent is waiting on with a diff or command preview and lets a person decide from any surface; paired
  phones are pushed a summary. Tested with a real Edge against local pages only. Not built: the shared cloud computer
  and native computer use. See `docs/agents/browser-and-routines.md`.
- **Developer surfaces (roadmap P5).** `python -m abp_run` runs the agent once without a chat channel (JSON
  output, exit codes, read-only mode, approvals denied unless allowed); `python -m abp_acp` lets editors that
  speak the Agent Client Protocol use it; after an edit the agent can be shown a formatter's and a language
  server's verdict, and gets an `lsp` tool (off until configured; checked by hand against rust-analyzer);
  `/export` and `/api/agent/sessions/<key>/export` produce a redacted transcript; the dashboard API now has a
  committed OpenAPI document with a Python client and a generated JavaScript client; a GitHub Action reviews pull
  requests read-only; `opencode` and `openclaw` are new backend names that hand a turn to those products (their
  own permissions apply, not ABP's). Several of these have only been tested against stand-ins; see
  `docs/agents/developer-surfaces.md` for exactly what was and was not run.
- **Model knowledge, limits and usage (roadmap PM).** ABP now knows each model's context window, output limit,
  abilities, price, knowledge cutoff and published free-tier limits (requests and tokens per minute and day,
  with the source and the date checked; `config/model_limits.yaml`), counts every model call, reads the
  provider's rate-limit headers, and holds a call back - or fails over to the fallback model - before a used-up
  free model would only answer 429, saying when it frees up in your time zone. New: agent tools `model_info`
  and `find_models`, `/modelinfo` (`/limits`), `/api/models/*`, MCP `get_model_info` / `get_model_usage` /
  `find_models`; your own limits under `native_agent.models`. Published figures can be out of date and most
  providers publish none; see `docs/agents/models.md`. Requires `tzdata`.
- **Named agents, skill packs and custom commands (roadmap P4).** Agents defined in Markdown
  (`.claude/agents`, `.opencode/agent`, `.abp/agents`) with `spawn_subagent agent=...`, optional git-worktree
  isolation and a definition that can only narrow what a child may do; SKILL.md packs loaded on demand
  (`read_skill_file`); `/skills fetch <git url>` into a quarantine with a security scan and signature check,
  approved only by a person; optional skill drafts written after long tasks (off by default, human approval);
  Markdown slash commands. See `docs/agents/skills-and-agents.md`.
- **Agent context and memory (roadmap P3).** The loop now watches how full the model's window is (token
  estimates calibrated on the provider's own counts) and, before each call, clears old tool outputs and if
  needed summarises older conversation; Anthropic requests carry a moving cache breakpoint. New tools:
  `repo_map` (outline of the codebase), `code_search` (SQLite FTS5 keyword index of the project),
  `session_search` (earlier conversations of the same bot). AGENTS.md / CLAUDE.md in the working directory
  and `@imports` are added to the system prompt (labelled untrusted; they cannot grant permissions). Memories
  gain kinds (user, feedback, project, reference), de-duplication, fading with age, `/memory list` and
  `/memory forget`. Fixed: a long agent session used to load its oldest 200 messages instead of its newest.
- **Agent security: permissions, untrusted-content defence, credential protection, sandbox (roadmap P2,
  `docs/agents/security.md`).** Permission rules (allow / ask / deny by tool, class and a pattern on the
  command, path, host or query) and modes (default, plan, accept_edits, bypass), with a host lock. Shell
  allow-rules never match a command containing an operator, so `git status*` cannot smuggle in `; rm -rf`.
  A conversation that read web or untrusted-MCP content can no longer auto-run changes: allows become asks,
  standing approvals are ignored, delegation needs approval. Commands no longer inherit the server's API
  keys (credential-shaped variables are removed), secrets are redacted from tool output, and a call that
  carries one to an external tool is refused. Optional docker sandbox (no network, limits, fails closed).
  MCP servers are untrusted by default and a tool whose description changes is blocked until approved. Hooks
  gain `PostToolUseFailure`, `Stop`, `SubagentStop`, `PreCompact`, `Notification`, `SessionEnd`, input
  rewriting and HTTP hooks. New API under `/api/agent/*`, `/api/instances/{id}/permissions`, `/api/mcp/pins`.
  Behaviour change: `run_shell` now runs with a scrubbed environment; set `sandbox.env.mode: inherit` for the
  old behaviour.
- **Agent editing, search and shell tools; a sturdier loop (roadmap P1).**
  New tools for the native agent: `edit_file`, `multi_edit` and `apply_patch` (careful
  matching, atomic, all-or-nothing), `grep`, `glob`, `todo_write` / `todo_read`, and
  `shell_output` / `shell_list` / `shell_kill` for background jobs. `run_shell` gains a
  timeout, a `cwd` and `background`, and stops the whole process tree when it times out or
  is cancelled. `read_file` gains offset / limit / line numbers and reads notebooks.
  Changing an existing file now requires that the agent read it first and that it has not
  changed since. Large tool output is saved in full under `.abp-tool-output/` in the
  workspace instead of being cut. `web_fetch` / `web_search` exist but are off until
  `native_agent.web.enabled`. The loop runs consecutive read-only calls in parallel, ends a
  turn that hits a step / time / token limit (`native_agent.limits`) with a summary rather
  than an error, notices when the agent is going in circles, answers open tool calls when a
  turn is cancelled so the session stays valid, and repairs sessions an earlier cancellation
  left dangling. Not included: an interactive terminal and a shell that remembers `cd`.
- **CI/CD telemetry and control plane (step 1 of `docs/cicd/README.md`).** New
  dependency-free package `abp_cicd`: an append-only, tamper-evident event
  store (SQLite WAL, hash chain, allow-listed schema with secret redaction,
  JSONL export, retention that keeps the chain verifiable), a recorder, read
  models, and a CLI (`python -m abp_cicd status | runs | run | explain | steps |
  decisions | workers | events --follow | verify | export | prune`). The
  release and pipeline scripts now record every run — steps and timings,
  decisions (why a step was skipped), flaky re-runs, self-healing, rollbacks
  and outcomes — and a release links to the pipeline gate it launched.
  `/api/cicd/*` serves the same data (with an SSE stream); the API, CLI and
  a local read all go through one service layer and a parity test fails if
  they drift. The installer bundle and Docker image ship the package.
- **Dashboards for the pipeline (step 2 of `docs/cicd/README.md`).** A terminal
  dashboard (`python -m abp_cicd tui`) and the native desktop dashboard
  **ABP_CI-CD_GUI** (`python -m abp_cicd gui`, or double-click
  `scripts/ABP_CI-CD_GUI.pyw`), with an overview, runs with a step timeline
  (Gantt) and a plain-language explanation, step-timing statistics, decisions,
  ML worker status (stale when silent), a live event tail, and log-integrity
  verification. Both are built from one set of panel definitions and charts
  drawn through a toolkit-independent painter; tests fail if either client lacks
  a panel or a service capability is shown nowhere.
- The Rust check rebuilds the staged bundle when a bundled resource is
  missing (previously a confusing `tauri_build` failure after a resource was
  added).
- **A hardened, self-healing release pipeline** (`scripts/release_guard.py`,
  used by `publish_release.py` and `local_pipeline.py`). A release used to
  bump versions and commit before discovering — mid-build — that a leftover
  backend process held a file in the staged venv open, leaving a half-made
  release commit to undo by hand. Now:
  - **Pre-flight, before anything is modified:** git state (on `main`, not
    behind origin, no rebase/merge in progress, stale `index.lock` cleared),
    tracked files committed, no source files hidden by `.gitignore`, version
    newer than every tag and unused locally and on origin, the signing key
    matches the key embedded in the app, tools/`gh` login/network/disk.
    `Cargo.lock` is synced automatically. `--dry-run` runs only this.
  - **Lock healing:** processes running out of the built app or staged venv
    are stopped and the files proven free before a build; a locked-file build
    error heals and retries.
  - **Transactional:** each step is journalled (`.release_journal.json`);
    failures roll back what never left the machine (release commit, tag,
    uncommitted bumps) without discarding unrelated edits, never rewrite what
    was pushed, and `--resume` finishes an interrupted release. HEAD and the
    tracked tree are re-verified before each irreversible step.
  - **Gate before tagging:** the full pipeline runs once on the release
    commit; the pre-push hook then skips itself for that exact commit instead
    of re-running it or rebuilding over the installer about to be signed.
  - **Bundle smoke test:** the staged installer bundle must boot and answer
    `/healthz` (throwaway state dir, random port, mDNS off) before it is tagged.
  - **Retries** with backoff for network steps; a half-created GitHub release
    is completed by upload rather than re-created; asset digests are re-read
    until GitHub has computed them.
  - `local_pipeline.py`: one pipeline at a time (`.pipeline.lock`), a failed
    test is re-run once and reported as flaky if it then passes, and every
    step has a timeout.
- `ABP_DISABLE_MDNS=1` turns off the LAN (mDNS) advertisement, for throwaway
  and test instances.
- `/healthz` reports a random, non-secret `server_id` (persisted in
  `data/server_id`), so a paired phone can tell its own server from another
  ABP on the same network.
- **MIT license.** The repository now carries a `LICENSE` file (it had none,
  which left third parties with no right to use or embed it), and the README,
  `Cargo.toml` and `flake.nix` declare it.
- **`ABP_HOME`** — set it in the process environment to keep all of ABP's
  mutable state (`.env`, `config/`, `data/`, `logs/`) in a directory of your
  choice instead of beside the code, for ABP embedded in another server as a
  submodule/sidecar. The directory is created and the default routing config
  seeded on first run; the global `~/.claude/.env` is never read; hot reload
  defaults to off. New guide: `docs/embedding.md`.

### Changed
- The project root is no longer a hardcoded developer path. `bot/envfile.py`
  used to prefer `Z:\Projects\AgenticBotPlatform` on any machine where that
  directory existed. It now separates `CODE_ROOT` (the running package, UI
  assets, venv) from `PROJECT_ROOT` (state), and a built app running from a
  `cargo tauri build` output inside a source checkout still shares that
  checkout's `.env`/config/data — detected from the layout, not a path.
- A read-only install directory no longer crashes at import: the log
  directory falls back to the system temp directory.

### Fixed
- The provider-warning spam was only half fixed in 0.7.22: the Models page
  polls every 30 seconds through a second code path (`browse_provider_models`)
  that had no backoff, so an unreachable provider still logged a WARNING per
  poll on a running install. That path now shares the same failure backoff
  (a working provider is still fetched live; an explicit Refresh always
  tries and logs).
- The release script signed the update installer before the repo's pre-push
  hook rebuilt it, so v0.7.24's published signature didn't match its
  installer. It now signs immediately before uploading and reads the
  published assets back to verify their hashes and signature.

### Security
- **Installer no longer ships the developer's provider API keys.** The
  bundle included the whole `config/` folder, so the gitignored
  `config/providers.yaml` (holding real API keys) went out inside every
  published installer. Only `config/backends.yaml` ships now; a missing
  `providers.yaml` starts as empty. If you installed any earlier release,
  its `config\providers.yaml` came from the release, not from you — treat
  keys in it as exposed and rotate them.
- **Creating or enabling an agent hook now needs permission tier
  `unrestricted`** (the desktop token always may). Hooks run a shell
  command as the server user, and until now any paired device — even at
  tier `none` — could create one via `POST /api/hooks`, i.e. a paired
  phone could run code on the server. Listing, disabling and deleting
  hooks are unchanged. Raise a device's tier in the Devices view if it
  legitimately manages hooks.
- **Reflected XSS fixed** in the unauthenticated MCP OAuth callback
  (`?error=` was echoed into HTML unescaped, on the same origin that holds
  the dashboard token). Every response now also carries `nosniff`,
  `Referrer-Policy: no-referrer` and a CSP limited to `object-src`,
  `base-uri` and `frame-ancestors`.
- **A blank `DASHBOARD_TOKEN=` no longer leaves the server without a
  token.** `.env.example` ships that line blank; `load_dotenv` turned it
  into an empty string that `setdefault` preserved, so the process ran
  token-less and the token-bootstrap routes (`/api/env/content` — every
  provider key — and `/api/setup/*`) were open to anyone who could reach
  the port. A real token is now always generated (filling the blank line
  in place), and those bootstrap routes only ever answer this machine.
- **Desktop updates are now cryptographically verified.** The updater
  downloaded any URL and ran it silently with no integrity check. It now
  only fetches this repository's HTTPS release assets, requires a detached
  Ed25519 signature (`<installer>.sig`) that verifies against a key built
  into the app before anything is written, caps download size, and only
  ever runs the one file it verified. Releases are signed by
  `scripts/update_signing.py` with a key kept outside the repo; the
  release aborts if it is missing. Apps from before this change can't
  verify, so they update to this version once as before.
- The desktop window's IPC (terminal, token, updater commands) is granted
  only to the dashboard's own origin, `http://127.0.0.1:8787`, instead of
  every localhost port — so another local web app on a different port can
  no longer reach it. Tauri matches this against the request origin (no
  path), so it can't be narrowed to a single page: any script that runs in
  a dashboard page still has IPC, which is why the XSS fix and the
  escaping of every rendered value matter. A unit test now pins the
  pattern, and it was verified against the real window.
- Support bundles and crash reports — built to be pasted into public bug
  reports — now redact API keys, bot/Slack/GitHub/AWS tokens, bearer
  headers, `token=`/`password=` style values and this process's own secret
  environment values.
- Docker: the image no longer copies the whole `config/` folder (which
  baked the builder's `providers.yaml` into every layer), and compose
  publishes the dashboard on `127.0.0.1` only.
- The installer bundle is built from a filtered copy (`scripts/stage_bundle.py`):
  no `pytest`/`pip-audit`, no `__pycache__`, no `activate` scripts or
  pip/pytest launcher `.exe`s, and no build-machine paths (`pyvenv.cfg`'s
  `command =` line, embedded interpreter paths, the developer's username).
  The build fails if any personal path is still found.

### Fixed
- The bottom Terminal/Activity panel covered the bottom of the page
  whenever it was open: it is `position:fixed` over the window, but the
  page's bottom padding was a static 80px regardless of the panel's
  height. The padding now follows the panel's real height (collapse,
  drag-resize, maximize and window resize), so page content is never
  hidden behind it.
- The dashboard and desktop UI's static files (`/`, `/static/*`,
  `/desktop-ui/*`) were served with no `Cache-Control` header, leaving a
  browser or the desktop app's embedded webview free to keep running
  JS/HTML from before an update after a normal reload. They now send
  `no-cache`; ETag revalidation still returns a cheap 304 when nothing
  changed.
- An unreachable custom model provider (a local Ollama that isn't
  running, a mistyped base URL) logged a WARNING every time its
  5-minute cache entry expired, forever. Failing providers now back off
  (up to one hour between retries) and only the first failure in a run
  logs at WARNING; later ones log at DEBUG until the provider recovers.

### Added
- A bot instance (Telegram/Discord/Slack/Matrix/WhatsApp) that crashes
  now restarts itself automatically with backoff instead of sitting
  dead until someone notices in the dashboard and clicks restart. A
  deliberate stop, or a disabled/deleted instance, never triggers a
  restart.
- A new Diagnostics tab (dashboard and desktop app): system info,
  local-only self-healing/error telemetry counters (crash reports
  written, bot-instance crashes and auto-restarts, warnings/errors/
  criticals logged), recent self-healing events, a crash-report list,
  and a "Download support bundle" button that zips everything useful
  for a bug report — system info, telemetry, recent crash reports, and
  the bot.log tail — into one file. Every CRITICAL-level event (reserved
  for genuinely uncaught exceptions and unrecoverable startup failures)
  now also writes a structured JSON crash report with the full
  traceback and recent Activity-tab context. Nothing here is ever sent
  anywhere automatically — it's all local, viewed in the dashboard, and
  exported only when a human clicks the button.

### Changed
- Renamed the project from BotServer to AgenticBotPlatform (a name
  collision with unrelated existing software). Covers display strings,
  the desktop app's window title/`productName`/identifier
  (`com.agenticbotplatform.app`), the Rust crate/lib names
  (`agentic-bot-platform` / `agentic_bot_platform_lib`), the Android
  package (`com.agenticbotplatform.mobile`), the mobile pairing deep-link
  scheme (`agenticbotplatform://pair`, later shortened to
  `_agenticbot._tcp.local.` for the mDNS service type — see Fixed below),
  internal env var names
  (`AGENTICBOTPLATFORM_*`), and docs/scripts throughout. The Python
  package (`bot/`) and Android's on-device storage identifiers
  (credential store, database file name, shared-prefs names) were
  deliberately left unchanged to avoid breaking imports and existing
  installs' stored data. The GitHub repo was also renamed to
  `LoopyLuci/AgenticBotPlatform` (GitHub redirects the old URL); both
  auto-updaters now point at the new repo name.

### Fixed
- The rename above broke mDNS/DNS-SD advertisement outright: the full
  `_agenticbotplatform._tcp.local.` service type is 19 bytes, over the
  15-byte limit `zeroconf` enforces, so `mdns_advertise.start()` failed
  every single time with `Service name (agenticbotplatform) must be <=
  15 bytes` — silently disabling the Android app's on-LAN discovery
  fallback on every install since the rename shipped. Shortened to
  `_agenticbot._tcp.local.` (Android's `NsdDiscoveryClient.SERVICE_TYPE`
  updated to match).
- A logging feedback loop that could crash the bot process outright with
  zero output: `bot/activity_log.py`'s ring buffer handler sits on the
  root logger and calls subscriber callbacks synchronously from
  `emit()`; `bot/dashboard/server.py`'s activity-entry subscriber falls
  back to `logger.warning(...)` when invoked with no running event loop
  (routine during early startup), which re-entered the same handler on
  the same thread, re-notified subscribers, warned again, and so on —
  observed live as a burst of identical warnings followed by a
  `RecursionError` (printed by Python's own logging module as
  `--- Logging error ---`) and, in the worst case, an unrecoverable
  crash with no traceback at all. The ring buffer handler now guards
  against this re-entrancy directly, independent of what any current or
  future subscriber does inside its callback.
- The desktop app's bundled Python runtime only ever worked on the exact
  machine it was built on — Windows venvs embed an absolute base-install
  path, and any other machine failed immediately with `No Python at
  '<path>'` before the bot could even start. The app now detects this
  and repairs itself in place: it finds (or silently installs via
  winget) a compatible Python 3.11, verifies the venv's actual compiled
  dependencies (Pillow, cryptography, numpy) load correctly under it —
  not just that the interpreter starts — and reinstalls them fresh if
  they don't.
- The Dashboard Token could still prompt for manual entry on a
  brand-new install: the desktop app's boot-time token read could win a
  race against the server's own auto-generation and come back empty.
  The token is now guaranteed to exist by the time it's requested, and
  the desktop app no longer falls back to the manual-entry dialog on any
  auth error.
- The bottom Terminal/Activity panel could get permanently minimized
  with no way to reopen it (a page-load state that never synced the
  panel's initial collapsed state with its reopen button's visibility).
- The floating "Open Terminal" reopen button was rendered directly
  underneath the always-visible collapsed panel header bar (a z-index
  overlap), making it completely unclickable in practice. Removed it
  entirely — the panel's own minimize button now toggles open/closed,
  so there's only one control to find.
- The Activity tab could load empty and stay that way if its first
  `/api/activity` fetch raced the dashboard token or otherwise failed
  transiently; switching to it now retries the load if it's still
  empty.
- Port 8787 already being in use (a leftover orphaned process, another
  instance, a crash that didn't release the socket) used to crash the
  whole app outright with no recovery. Fixed at both layers: the
  desktop app now checks whether an existing listener on that port is
  actually a healthy dashboard (adopts it instead of duplicate-spawning)
  or a dead/foreign process (verifies it's really our own `bot.main`
  before killing it, then frees the port before spawning); the Python
  side's dashboard startup now retries with backoff on a bind failure
  instead of taking the whole process down with it — including fixing a
  real asyncio gotcha where `uvicorn`'s `sys.exit()` on bind failure
  wasn't being caught by the retry logic at all because `SystemExit`
  isn't captured by `asyncio.Task`'s result-retrieval machinery the way
  normal exceptions are.
- Any uncaught exception on the main thread or a background thread used
  to only ever print to stderr, easy to miss entirely on a GUI app with
  no visible console. Both are now also logged to `logs/bot.log` and
  the Activity tab so nothing fails silently.
- `bot/hotreload.py`'s and `bot/config.py`'s file watchers could be
  permanently and silently killed by a real (if rare) `watchfiles`
  failure — a deleted watched path, a permission change, an OS-level
  file-watching hiccup — disabling hot-reload or config-file watching
  for the rest of the process's life with no visible sign short of a
  restart. Both now restart themselves automatically on such a failure,
  matching the self-healing already in place for the scheduler,
  retention, and peer-health-check background loops.

### Added
- Copy and Save buttons on the Terminal/Activity panel, acting on
  whichever tab is currently active.

## [0.4.0] — 2026-08-30

### Fixed
- `Router._invalidate()` (fired on every config hot-reload) dropped its
  cached backend dict with no shutdown call, silently leaking any
  backend holding a live external process — `HermesGatewayBackend`'s
  spawned `hermes serve`, in practice, on every `backends.yaml` edit.
  Now schedules a proper `shutdown()` on the old backend set (flagged
  during this session's hot-reload work, fixed as its own follow-up).
- **Critical**: `bot/main.py` crashed on startup (`SystemExit`) whenever
  zero bot instances were configured, and it did this *before* the
  dashboard/API server was even built — a fresh install could never
  reach the "Add a bot" UI needed to fix itself. AgenticBotPlatform now starts
  and the dashboard/desktop UI is fully usable with zero bots; adding
  the first one is just a normal Bots-tab action, not a precondition.
  The setup wizard's own "Ready" gate no longer requires a bot/platform
  to already exist either, for the same reason.
- `scripts/local_pipeline.py`'s deploy step could fail with a Windows
  "file in use" error even after correctly stopping `agentic-bot-platform.exe`,
  because a separate `python -m bot.mcp_server` process (spawned by an
  MCP client from the same bundled `target/release/.venv` a Rust
  check/build needs to overwrite) could independently hold the same
  compiled extension modules memory-mapped. The pipeline now finds and
  stops any such process before a Rust check or deploy, the same way it
  already handles `agentic-bot-platform.exe` itself.

### Added
- A Textual-based terminal UI (`bot/tui/`, launch via `scripts/tui.sh`/
  `scripts/tui.ps1` or `python -m bot.tui`): add/edit/delete bots across
  all 5 platforms, start/stop/restart/enable/disable, live per-field
  validation and setup help, and a schedules panel — talking to an
  already-running AgenticBotPlatform's dashboard HTTP API, so it manages a
  remote/federated install exactly like the desktop app does. A third
  way to manage bots alongside the browser dashboard and desktop app,
  for headless machines, SSH sessions, or terminal-first workflows.
- Completed the Add-a-bot form: inline help and step-by-step setup
  guidance for all 5 platforms (previously only Matrix/WhatsApp had
  any), live green/red field validation as you type (`GET
  /api/platform-guides`, `POST /api/validate-field`) instead of only
  failing after submit, `custom_instructions`/`enabled` settable at
  creation time, and an "Advanced" editor for per-instance
  `action_overrides` (accepted by the API since Matrix/WhatsApp shipped
  but previously had no UI anywhere).
- Scheduled commands (`bot/scheduler.py`, previously chat-only via
  `/cron`/`/loop`/`/heartbeat`) now have a dashboard API
  (`/api/bots/{id}/schedules`) and a "Schedules" card in the Bots tab,
  mirrored into the desktop app.
- Python code hot-reload (`bot/hotreload.py`): most edits to `bot/*.py`
  now apply to the already-running process instead of needing the full
  local-CI/CD stop/rebuild/relaunch cycle — business logic and backends
  apply on the very next call, Discord/Slack/Matrix code gets a brief
  automatic reconnect, and a documented set of core files (routing, the
  DB connection, the dashboard, Telegram's handler registration, and a
  few others confirmed to hold live singleton/subprocess/socket state)
  still require the existing full restart, reported as "restart
  required" rather than silently skipped or half-applied. A failed
  reload enters a degraded state that blocks further cycles until an
  actual restart, rather than risk compounding a broken module. New
  dashboard "Hot Reload" card (status, recent events, manual "reload
  now"), mirrored into the desktop app, plus `hot_reload_status`/
  `trigger_hot_reload` MCP tools. Toggle: `hot_reload_enabled` in
  `config/backends.yaml`. See `bot/hotreload.py`'s module docstring for
  the full reasoning; the classification is guarded by a test that
  parses every file's real imports so it can't silently rot as the
  codebase grows. Second half of an earlier request (the first half —
  config hot-reload hardening + snapshot/restore — shipped separately).
- WhatsApp Cloud API as a fifth chat platform
  (`bot/platforms/whatsapp_platform.py`) — architecturally different from
  the others: messages arrive via a webhook Meta calls
  (`POST /webhooks/whatsapp` on the dashboard's own FastAPI app, verified
  with a real X-Hub-Signature-256 HMAC check since that route can't
  require a dashboard token), not an outbound-connecting client. Full
  two-way messaging including media (images, documents, audio, video) via
  the Graph API's upload/download endpoints, and the same slash-command/
  allowlist/dashboard-Chat-tab integration every other platform gets.
  Requires a real Meta Business/WhatsApp Cloud API app and a public HTTPS
  URL — see the module's docstring. Phase D of the multi-provider/
  plugin/platforms roadmap — completes it.
- Matrix as a fourth chat platform (`bot/platforms/matrix_platform.py`,
  via matrix-nio): a bot instance can now be Telegram, Discord, Slack, or
  Matrix. Full messaging (text + incoming/outgoing images, files, audio,
  video), automatic room-invite acceptance, and the same slash-command/
  allowlist/dashboard-Chat-tab integration every other platform gets.
  Encrypted rooms aren't supported (no Olm/Megolm store) — use an
  unencrypted room. Phase C of the multi-provider/plugin/platforms
  roadmap.
- A plugin API: a single local `plugin.py` file can register new agent
  tools and/or slash commands (`bot/plugins.py`) without touching core
  code — they show up in every backend's tool list and in `/help`/
  `/commands`/Telegram's native menu exactly like built-in ones. Managed
  from a new dashboard "Plugins" card (install/enable/disable/remove),
  mirrored into the desktop app. Local-install only, deliberately not a
  networked marketplace — see
  [ADR-0007](docs/adr/0007-plugins-are-trusted-local-code.md) for the
  trust model (a plugin is trusted local code with full process
  privileges, the same boundary `run_shell` already accepts). Phase B of
  the multi-provider/plugin/platforms roadmap.
- Appearance settings (Control Center tab): theme (System/Light/Dark) and
  a whole-UI text/scale control (85%-150%), per-browser via localStorage,
  applied instantly with no server round trip or flash-of-wrong-theme on
  load. Mirrored across the dashboard and desktop app.
- A live-development safety net: `bot/config.py`'s hot-reload is now
  hardened against a config file that parses but has the wrong shape
  (rejected and logged, same as a syntax error, instead of getting
  swapped in to crash later); a new snapshot/restore system
  (`bot/snapshots.py`, a "Snapshots" dashboard card, and
  `create_snapshot`/`list_snapshots`/`restore_snapshot` MCP tools) takes
  a zero-downtime point-in-time copy of config + the database and can
  restore it later, so an agent (or you) editing this codebase can
  recover from a bad change without a full backup/rebuild.
- Multi-provider model routing: a new `custom_model` backend that talks
  to any OpenAI-compatible endpoint (a local Ollama/LM Studio/vLLM/
  llama.cpp server, OpenRouter, or real OpenAI) via a named provider
  registry (`config/providers.yaml`, managed from the dashboard's new
  "Model providers" card) — runs Agentic Bot Platform's own shell/file/git tool
  loop against it, the same one the `api` backend already uses for
  Anthropic. `/gateway`, `/model`, and the dashboard's model picker all
  treat it as its own family. Phase A of a larger roadmap (plugin API,
  WhatsApp/Matrix platforms) — see `docs/adr/` and the project's plan log.
- A real `pytest` suite (`tests/`).
- A 100%-local CI/CD pipeline (`scripts/local_pipeline.py`) — byte-compiles
  every source file, runs the test suite, audits dependencies, checks the
  Rust side (format/clippy/compile), and builds the Docker image, all on
  your own machine, then rebuilds and redeploys the running instance on a
  green result. Installed as a `pre-push` git hook via
  `scripts/install_git_hooks.sh` / `.ps1`. Replaces an earlier GitHub
  Actions workflow, which this project no longer uses at all. Change-aware:
  a push that doesn't touch `desktop-app/`, Docker files, or `bot/`/`config/`
  skips the corresponding check (or the whole stop/rebuild/restart cycle)
  instead of always paying the full multi-minute cost.
- A per-instance circuit breaker: a bot instance whose backend fails 5
  times in a row now pauses for 5 minutes instead of retrying forever,
  with a "retry now" action in the dashboard.
- `/healthz` (unauthenticated liveness probe) and `/metrics`
  (Prometheus-format gauges/counters) for real deployment monitoring.
- Per-table data export (`/api/export/{table}`, JSON or CSV) from the
  dashboard's Database panel.
- A `/gateway` command (Telegram/Discord/Slack) showing backend readiness
  scoped to a bot's own model family (Claude or Hermes); `/status`'s
  Model line now resolves the real live model in effect instead of a
  generic placeholder.
- A Dockerfile/`docker-compose.yml` for headless server-only deployment,
  and a documented, equally-capable bare-metal path for machines without
  Docker (`scripts/run.sh`/`run.ps1` plus the existing
  `install_service*`/`install_task.ps1` autostart scripts).
- This changelog, and an [architecture decision record log](docs/adr/).

### Changed
- Removed the separate "Custom bot instructions" card now that
  `custom_instructions` is editable inline in the Add-a-bot/edit form;
  its one genuinely useful feature (inserting a persona's default
  instructions) moved into that form as an "Insert `<persona>` preset"
  button.

### Fixed
- `router.resolve_chain()`'s "ui never gets a silent default" guard was
  a no-op if `default_backend` was itself set to `"ui"` (a one-click
  dashboard option) — it re-read the same config value it was meant to
  override. Now hardcoded to fall back to `"api"`.
- `slots.find_bool()` false-positived on ordinary words containing
  "on"/"off"/"no" as a substring (e.g. "turn off notifications" matched
  "on" inside "notifications" and returned `True`). Now uses
  word-boundary matching.

## [0.3.0] — 2026-08-27

### Added
- A visual, hardware-aware GUI installer (`scripts/install_gui.py`) that
  shows live detection and install progress; the text installer remains
  as an automatic fallback for headless machines and scripted use.
- Real, live download progress in the in-app self-updater (previously a
  silent multi-minute wait).
- Opt-in automatic `VACUUM` for the data-retention pass.

### Changed
- Dashboard auto-refresh timers now pause while the tab/window isn't
  visible instead of polling continuously in the background.

### Fixed (post-release, same day)
- The installer crashed with an unhandled `FileNotFoundError` if it
  offered a production build without Rust/Tauri actually being
  available; it also auto-launched the fully-interactive setup wizard
  under an unattended run with no console attached, producing a
  confusing "Aborted." Both now fail/skip cleanly with a clear message.

## [0.2.2] — 2026-08-27

### Changed
- Replaced scikit-learn with a from-scratch NumPy implementation of the
  Support Bot's neural classifier — Windows installer 136MB → 72.7MB
  (MSI), 80MB → 42.6MB (NSIS).

### Added
- Automatic daily data retention: prunes old rows from jobs, telemetry,
  connection-log, and Support Bot classification tables.

### Fixed
- Two orphaned scheduled commands (pointing at long-deleted bot
  instances) had been firing every 5 seconds for days, always failing.
  Deleting an instance now cascades to its scheduled commands, and the
  scheduler auto-disables any schedule whose instance no longer exists.

## [0.2.1] — 2026-08-27

### Added
- Windows Firewall detection and one-click "Open this port" fix for the
  most common reason server-to-server linking silently failed.
- MCP server stability: a shared long-lived HTTP client, retry on
  transient connection failures, and rotating file logging.

## [0.2.0] — 2026-08-27

### Added
- Native Telegram command/menu system with a real agent-loop engine for
  the `api` backend: `/queue`, `/steer`, `/pause`, `/approve`/`/deny`,
  and git-backed checkpoints (`/rollback`, `/undo`, `/branch`,
  `/compress`, `/worktree`).
- Cross-backend `/model` picker grouped by provider, with live model
  lists.
- Cross-network WebRTC fallback and real TURN support for mesh APK
  transfers between devices that can't reach each other directly.
- Server-to-server linking (federation): link multiple AgenticBotPlatform
  installs and manage every one's bots from a single dashboard, using a
  short-lived, single-use, self-describing pairing token rather than
  ever pasting a real dashboard token into another server's UI.

## [0.1.1] — 2026-08-23

### Added
- Session linking: `ui`/`hermes_gateway` bot instances write into one
  real, persistent chat/session instead of a fresh one per call.
- One-click Android build/install/pair from the desktop app's Mobile tab.
- Agent-to-agent control: `ask_instance`/`run_swarm` MCP tools and an
  `agent_control` allowlist for cross-bot queries.
- Chat attachments with chunked uploads and inline thumbnails.
- Linux support (Debian/Fedora/Arch, NixOS flake, Qubes AppVM notes).

## [0.1.0] — 2026-08-21

First public release. One desktop app running any number of independent
Claude/Hermes Agent bots across Telegram, Discord, and Slack at once,
plus a native Android companion.

[Unreleased]: https://github.com/LoopyLuci/AgenticBotPlatform/compare/v0.4.0...HEAD
[0.4.0]: https://github.com/LoopyLuci/AgenticBotPlatform/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/LoopyLuci/AgenticBotPlatform/compare/v0.2.2...v0.3.0
[0.2.2]: https://github.com/LoopyLuci/AgenticBotPlatform/compare/v0.2.1...v0.2.2
[0.2.1]: https://github.com/LoopyLuci/AgenticBotPlatform/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/LoopyLuci/AgenticBotPlatform/compare/v0.1.1...v0.2.0
[0.1.1]: https://github.com/LoopyLuci/AgenticBotPlatform/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/LoopyLuci/AgenticBotPlatform/releases/tag/v0.1.0
