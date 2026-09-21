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

`python -m abp_import claude-code` and `python -m abp_import opencode` read `.claude/settings.json`, `.claude/settings.local.json`,
`.mcp.json` and `~/.claude/settings.json` (Claude Code), or `opencode.json(c)` (OpenCode), and print a plan. **A dry run unless you
add `--apply`.** Permissions become ABP rules (allow / ask / deny with the tool names and patterns translated; deny rules are
written first); hooks become ABP hooks; MCP servers become external MCP servers (untrusted by default, whatever they were
before). It never widens what the agent may do on its own: a blanket allow such as a bare `Bash` is reported and skipped, "bypass
permissions" is never imported, and a host with locked permissions refuses to be rewritten. Model settings, API keys, themes,
keybindings, agents, skills and commands are not imported (ABP reads the `.claude/` and `.opencode/` folders directly). Checked
against sample files written from those products' documented formats, **not against real configuration files from real
installs**. The Hermes and OpenClaw importers are **not built**: their formats were not checked.

## Plugin SDK versioning

The plugin API has a version (`bot.plugins.SDK_VERSION`, now `1.0`). A plugin can declare `REQUIRES_SDK = ">=1.0,<2"` and
`PLUGIN_VERSION = "1.2.0"`; one that needs a version this build does not provide is refused with a message naming both. A plugin
that declares nothing is assumed to want the 1.x line. A breaking change to what `setup(api)` and the handlers may rely on bumps
the major number; additions bump the minor. See [plugin-sdk.md](plugin-sdk.md).

## The results page

`python -m abp_agenteval page report.json [more.json] --out docs/benchmarks/index.html` writes a static page. The committed
one shows the scripted run, and **says it is not a model comparison**; it becomes one only when live reports from at least two
models are given. It does not compare ABP with Claude Code, OpenCode or any other product: those were not run here.
