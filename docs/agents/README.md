# ABP agents: what they can do and how much to trust each claim

ABP's own agent (the `native_agent` / `api` / `custom_model` backends) is a tool-using loop with permissions, sandboxing, memory,
sub-agents, a browser and many surfaces. This index says where each part is described, and - because a feature list is easy to
write and hard to verify - what has actually been run against the real thing.

**Start with the [roadmap](ROADMAP.md).** Its status block lists what has never run against the real counterpart; its per-phase
tables say the same feature by feature. This index only points.

## Where things are

| You want to | Read |
| --- | --- |
| Know what is built and what is not | [ROADMAP.md](ROADMAP.md) |
| Make the agent safe to leave alone: permissions, untrusted content, secrets, sandbox, hooks | [security.md](security.md) |
| Measure the agent | [evals.md](evals.md); results page: [../benchmarks/index.html](../benchmarks/index.html) |
| Give it named agents, skill packs, custom commands | [skills-and-agents.md](skills-and-agents.md) |
| Know what a model can do and how much free allowance is left | [models.md](models.md) |
| Run it from a script, an editor, CI or another program; language servers; SDKs | [developer-surfaces.md](developer-surfaces.md) |
| Let it use a browser, stored logins, routines, approve from a phone | [browser-and-routines.md](browser-and-routines.md) |
| Reach it by e-mail, SMS, Signal, iMessage or voice; use paired phones | [channels-and-devices.md](channels-and-devices.md) |
| Export runs, get model advice, import another product's settings, write a plugin | [learning-and-compatibility.md](learning-and-compatibility.md), [plugin-sdk.md](plugin-sdk.md) |

## Against the other products

The plan measured ABP against Claude Code, OpenCode, Hermes, OpenClaw and the Grok Bot app. What can honestly be said:

* **Built to the same shape**: file editing tools, search, a shell with background jobs and a permission engine, sub-agents with
  worktree isolation, SKILL.md skills, custom slash commands, hooks, MCP client, project instruction files, an editor protocol
  server, headless runs, a browser tool, routines, many chat channels, memory, and a model catalogue with allowance tracking.
* **Where it is behind or different**: no shared cloud computer and no computer-use tool (the Grok Bot pillar); the Android app
  does not yet implement nodes or approvals; no VS Code extension; no live-model measurements, so **no claim of matching any
  product's quality** can be made from this repository - only that the mechanisms exist and their tests pass.
* **Where it deliberately differs**: a session that has read untrusted content cannot auto-run changes, the agent never types
  passwords, imported settings never widen permissions, and every persistent or outward action needs a person.

Nothing was copied from another product; parity here means matching behaviour and standards.

## Running the checks

```bash
python -m pytest -q                       # the whole suite
python -m abp_agenteval run               # the eval suite, scripted (checks the tools and defences, not a model)
python -m abp_agenteval run --live --provider anthropic --model claude-sonnet-5 --record   # costs tokens; measures a model
python scripts/export_openapi.py --check  # the API spec and the JavaScript client are current
```
