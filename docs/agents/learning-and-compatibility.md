# Learning, efficiency and compatibility

## Trajectory export

`python -m abp_trajectory export --out runs.jsonl --confirm` writes finished runs as JSONL chat transcripts (messages, tool
calls, tool results, plus outcome, model, tokens and the tools used) for evaluation or fine-tuning. The shape of a run comes
from the trace store; the words come from the session's stored messages. **The words are conversation content** - people's
messages and tool output - so the command refuses without `--confirm`, removes secrets and credential-shaped strings,
shortens tool output, drops identical transcripts, and with `--scrub-pii` masks e-mail addresses, phone numbers and IP
addresses. Filters: `--only-ok` (default), `--min-tool-calls`, `--exclude-denied`, `--model`. Nothing exports itself, and nothing
here judges whether an answer was *right*: a run that finished is not a run that succeeded.

## The advisory model router

`/route <task>` and the `suggest_model` tool rank your candidate models for a task and say why. It classifies the task (trivial,
coding, hard reasoning, long context, vision, bulk), drops models that cannot do it (no tool calling, no images, window too
small, allowance used up right now), and scores the rest on quality, economy and remaining allowance, weighted by the class. It
**advises only**; nothing switches models by itself. Quality is the model's **measured pass rate** from
`python -m abp_agenteval run --live --record` when there is one, and otherwise a coarse prior from the catalog that every
recommendation labels as "a guess". Candidates: `native_agent.router.candidates`, or the free models ABP knows plus
`native_agent.router.also`. A scripted eval run is never recorded as a measurement.

## Comparing wordings

`python -m abp_agenteval compare --variants variants.json --live --provider P --model M` runs the suite once per variant of
settings - `native_agent.prompt.extra`, or `native_agent.tool_descriptions` (which replaces a tool's description without touching
its name or parameters) - and reports pass counts, tokens and steps per variant. **Only a live model makes it evidence**: scripted
runs replay fixed trajectories, so every variant scores the same and the command says so. Differences of two tasks or fewer are
reported as noise. This has not been run against a live model; that would spend your provider allowance, so it is left to you.

## Importing another product's settings

`python -m abp_import claude-code` and `python -m abp_import opencode` read the configuration the products themselves read and
print a plan. **A dry run unless you add `--apply`.** Permissions become ABP rules (allow / ask / deny with the tool names and
patterns translated; deny rules are written first); hooks become ABP hooks; MCP servers become external MCP servers (untrusted by
default, whatever they were before). It never widens what the agent may do on its own: a blanket allow such as a bare `Bash` is
reported and skipped, "bypass permissions" is never imported, and a host with locked permissions refuses to be rewritten. Model
settings, API keys, themes, keybindings, agents, skills and commands are not imported (ABP reads the `.claude/` and `.opencode/`
folders directly).

| From | Read from |
|---|---|
| Claude Code | `~/.claude/settings.json` (or `$CLAUDE_CONFIG_DIR`), the project's `.claude/settings.json`, `.claude/settings.local.json`, `.mcp.json`, and `~/.claude.json` - where `claude mcp add` keeps this project's user-scope servers, the tools it allowed there, and the `.mcp.json` servers this project has switched off |
| OpenCode | `$OPENCODE_CONFIG` (the file it was launched with), `$XDG_CONFIG_HOME` or `~/.config/opencode/opencode.json(c)`, then the project's `opencode.json(c)` |

`${VAR}` in a server's environment is read from your environment (and never printed). `permissions.additionalDirectories` and
OpenCode's `permission.external_directory` have **no** ABP equivalent and are reported: a bot's workspace is its own folder, so
ABP cannot be widened to reach a sibling folder. The SSE transport is reported too - ABP speaks stdio and streamable HTTP.

### How it was checked

Against the **real installs on the development machine**, read with every value masked and never written to: `~/.claude/` (which
holds no `settings.json` on this machine, so the user-settings importer had nothing to read - the rules all live in the projects),
`~/.claude.json`, the `opencode.jsonc` the swarm's own launcher passes in `$OPENCODE_CONFIG`, the global
`~/.config/opencode/opencode.jsonc` (50 bytes, `$schema` only), and the `.claude/settings.local.json` of five `Z:/Projects`
checkouts plus one `claude/settings.json` with hooks in a sixth (all 225 folders searched for `opencode.json(c)` and `.mcp.json`;
none). Both importers were run as a dry run against each of them and every line of every plan read. `--apply` was **not** run
against anything real: that is yours to do.

That found things a docs-based importer cannot:

- **`PowerShell(...)` permissions.** Claude Code has a separate PowerShell tool on Windows and the real files are full of it -
  212 of 735 entries in the largest one. ABP dropped every one with "names a tool ABP does not have". It is ABP's only shell, so
  they are now `run_shell` rules, with one note per file saying the dialect is not the same (ABP's `run_shell` is the platform
  shell, `cmd.exe` on Windows, so PowerShell-only syntax in those patterns will not run).
- **`//c/Users/...` and `//z/Projects/...` paths.** Claude Code spells a Windows folder with a leading `//` and a drive letter. The
  pattern was copied verbatim, so it could never match what ABP matches on. It is normalised now, and a path inside the project
  becomes workspace-relative, which is what the entry meant.
- **`SessionStart` hooks lost to their own matcher.** A session event's matcher (`startup|resume|clear|compact`) names session
  sources, not tools, so translating it as a tool name matched nothing and the hook was **dropped**. ABP fires these hooks
  unconditionally, so the matcher is dropped with a note and the hook is kept.
- **`~/.claude.json`.** 43 project entries, each with `mcpServers`, `allowedTools`, `enabledMcpjsonServers` and
  `disabledMcpjsonServers` - all ignored before. A server this project has switched off is now left out of `.mcp.json`.
- **OpenCode's config is not where ABP looked.** The launcher sets `$OPENCODE_CONFIG`, so the real config had no `opencode.json`
  name and the importer found nothing but the 50-byte `$schema` stub in `~/.config/opencode`. `$OPENCODE_CONFIG` and
  `$XDG_CONFIG_HOME` are honoured now.
- **A switched-off server and an unreadable decision value** were skipped in silence; both are reported by name now, and so is a
  hook's `timeout` / `statusMessage` (an ABP hook is given 30 seconds and no progress message).
- **`%USERPROFILE%\.mcp.json`** on this machine holds `{"inputs": [], "servers": {...}}` - not Claude Code's `mcpServers` shape. It
  belongs to another tool, so ABP deliberately does not read it.

The dry-run summary, per real file, counts only (no values):

| Real file | Rules before -> after | Hooks | MCP servers | Notes before -> after |
|---|---|---|---|---|
| `Omnisystem/.claude/settings.local.json` (735 entries, 212 of them PowerShell, 9 additionalDirectories) | 533 -> **745** | 0 | 0 | 213 -> **3** |
| `TransferDaemon/.claude/settings.local.json` | 5 -> **11** | 0 | 0 | 6 -> **1** |
| `NeuroForge/.claude/settings.local.json` | 29 -> **31** | 0 | 0 | 2 -> **1** |
| `JumpingSpiderSimulator/.claude/settings.local.json` | 13 -> **13** | 0 | 0 | 0 -> 0 |
| `AI_Transcriber_Caption_Maker/.claude/settings.local.json` | 1 -> **1** | 0 | 0 | 0 -> 0 |
| a `claude/settings.json` with `SessionStart` / `SessionEnd` hooks (a `Z:/Projects` checkout) | 0 -> 0 | 1 -> **2** | 0 | 4 -> **2** |
| `~/.claude.json` (43 project entries; every list in them is empty on this machine) | 0 | 0 | 0 | 0 |
| `~/.config/opencode/opencode.jsonc` (the real global config: `$schema` only) | 0 | 0 | 0 | 0 |
| `$OPENCODE_CONFIG` = the swarm's `opencode-swarm.jsonc` (6 servers, all `enabled: false`; 4 `allow` permission values) | 0 | 0 | 0 | 0 -> **5** |

Nothing on this machine widens: every rule that got imported was an `allow` the user had already written for a shell or a file
tool, the only `allow`s refused are the blanket ones, and ABP's own `permissions.decide()` is run against the imported rules in
the tests. The tests use fixtures in these shapes with every value replaced by a fake.

### Hermes Agent and OpenClaw

`python -m abp_import hermes` and `python -m abp_import openclaw` move a whole setup across, not just its permissions. Each
finds its data by itself: Hermes the way Hermes does (`HERMES_HOME`, then `%LOCALAPPDATA%\hermes`, then `~/.hermes`), OpenClaw
at `~/.openclaw` (or the older `clawdbot` / `moltbot` names). `--source DIR` picks another folder. Like the other importers,
this is **a dry run unless `--apply`**.

| From | Becomes in ABP |
|---|---|
| Providers, and API keys in `.env` or `openclaw.json` | Providers. A key is matched to its provider through the models.dev catalog, e.g. `OPENROUTER_API_KEY`. |
| The default and fallback models | Models the router may pick. Never an Anthropic model. |
| MCP servers | External MCP servers, untrusted by default. |
| The skill library | Linked in place, not copied; the skills that ship with Hermes are left out. |
| `SOUL.md` (and OpenClaw's `IDENTITY.md`) | A bot's custom instructions. |
| `MEMORY.md`, `USER.md` and OpenClaw's daily `memory/*.md` | That bot's memories. |
| Scheduled jobs that run a prompt | That bot's scheduled commands, **created paused**. |
| Telegram, Discord and Slack channels | Bots on those platforms, **created switched off**, since one token must not be polled by two programs. |
| A website blocklist, denied tools | Deny rules. |

The instructions, memories and jobs go on a new app-only bot, or on the first chat bot, or on `--instance N`, whose own
instructions are never overwritten. Importing again reuses what the first import made.

**Never imported, and said so:**
- approval mode "off" or "auto";
- the commands you approved in the other product;
- Hermes's gateway hooks (Python handlers for Hermes's own events);
- script-only jobs;
- OAuth logins;
- OpenClaw's jobs, because their format was not checked against a real install;
- a cron schedule that does not repeat at a fixed interval, because ABP's schedules are intervals and an approximation
  would fire at the wrong times.

`--no-secrets` imports no API key, chat token or MCP server secret, and secret values are never printed.

**How it was checked.** The formats come from real installs on the development machine: a Hermes install with its
config, `.env`, memories, jobs and a 10,000-skill library, and an OpenClaw config. They were read with every value masked,
together with Hermes's source and its own OpenClaw migration script. A dry run against that real Hermes install finds its
4 MCP servers, 4 providers (keys included), its models, `SOUL.md`, 13 memories and the library, and explains everything
it skips.

That check found details a docs-based importer would have missed:
- MCP arguments given as one string, and as a JSON list inside a string;
- a model setting written as a mapping;
- a script-only job;
- a character a Windows console cannot print, which crashed the first version.

The tests use fixtures in those real shapes. **Not done:** an `--apply` against the real installs, which is yours to run.

## Plugin SDK versioning

The plugin API has a version (`bot.plugins.SDK_VERSION`, now `1.0`). A plugin can declare `REQUIRES_SDK = ">=1.0,<2"` and
`PLUGIN_VERSION = "1.2.0"`; one that needs a version this build does not provide is refused with a message naming both. A plugin
that declares nothing is assumed to want the 1.x line. A breaking change to what `setup(api)` and the handlers may rely on bumps
the major number; additions bump the minor. See [plugin-sdk.md](plugin-sdk.md).

## The results page

`python -m abp_agenteval page report.json [more.json] --out docs/benchmarks/index.html` writes a static page. The committed
one shows the scripted run, and **says it is not a model comparison**; it becomes one only when live reports from at least two
models are given. It does not compare ABP with Claude Code, OpenCode or any other product: those were not run here.
