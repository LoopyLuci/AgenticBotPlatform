# The ABP Agents page

One place, in the dashboard and the desktop app, for everything about ABP's own agent: which bots use it, how careful it is,
what tools it has, and how it runs sub-agents and swarms. Open **ABP Agents** in the side menu.

An "ABP agent" is a bot whose backend is **ABP Agent** (`native_agent`), or `api` / `custom_model`, which run the same
tool-using loop. Bots on the Claude CLI, Hermes or other backends have their own settings elsewhere.

## Setting up a bot to use an ABP agent

1. **Give it a model.** On the **Models** page, add a provider (an API key, or a local server such as Ollama). Free models are
   marked, and each model's context window and rate limits are shown.
2. **Create the bot.** On the **Bots** page, **ABP Agent** is the first backend in the list and the default for a new bot.
   Enter the model as `provider/model`. The form then shows an **ABP Agent settings** panel with this bot's own settings:
   how careful it is (its permission mode), how many sub-agents it may run at once, the model and effort for its
   sub-agents, a fallback model, and whether it must ask you to approve a plan first. Anything left blank follows the
   global defaults, which the panel summarises. The panel also appears for `api` and `custom_model`, which run the same
   ABP agent loop.
3. **Tune it later.** Editing a bot fills the panel from what that bot has set itself (not values it inherits, so saving
   never pins a default). A bot's card has an **Agent settings** button that opens the fuller per-bot settings on this page.
4. **Add swarms** (optional). Define them on the **Swarms** page; their spending limits are on the *Sub-agents & swarms* tab.

The **Overview** tab checks each of these for you (a provider exists, a bot uses an ABP agent, approvals are on, the swarm
spending guard is on, nothing is waiting for review) and gives each one a button to go and fix it.

## The tabs

| Tab | What is on it |
| --- | --- |
| **Overview** | Readiness checks, counts, and a table of the bots that run an ABP agent with their model, permission mode and sub-agent limits. |
| **Runtime** | Steps, time and token limits per turn; the instructions every agent gets; project instruction files (`AGENTS.md`, `CLAUDE.md`); when the conversation is condensed; run tracing; prompt caching. |
| **Safety** | The default permission mode, whether bypass mode is allowed, locking permissions, permission rules, reading a file before changing it, where commands run (local or Docker) and which environment they see, and pinning MCP tool descriptions. Also each bot's own permission mode. |
| **Tools** | The web (and allowed / denied sites), the browser, code intelligence (language servers), provider-hosted tools, skill sources and learning, MCP sampling, and the full **tool inventory** with which tools ask first. |
| **Sub-agents & swarms** | How many sub-agents run in parallel, the swarm spending guard, cross-bot control, and each bot's own agent settings (worker model, effort, fallback, plan approval). |
| **Skills** | Skill packs awaiting your review, skills the agent drafted, installed packs, and installing a pack from git. Nothing new is used until you approve it. |
| **Models** | How model limits and free-tier allowances are enforced. |

## How settings work

* **Every setting has a plain-language label, help text, its default, and a "Reset to default".** Settings that are rarely
  needed sit behind *Show advanced settings*.
* **Changes are staged.** A bar at the bottom counts unsaved changes; **Save changes** checks all of them together and
  writes nothing unless every one is valid. A rejected setting is marked with the reason, for example *must be at most
  0.95*.
* **Saving keeps `config/backends.yaml`'s comments and layout.** Settings take effect on the next turn; the few that need a
  new session say so.
* **Risky settings warn you while they apply**, for example allowing bypass mode, running commands locally, or letting
  commands see every key.

### Permission rules

One rule per line: a decision (`allow`, `ask` or `deny`), a tool name, then an optional pattern. Add ` # note` to explain it.

```
deny run_shell rm -rf*   # never
allow read_file
```

A tool can be a name, `class:<permission>` (such as `class:write`) or `*`. See [security.md](security.md) for how rules,
modes and untrusted content combine.

### A bot's own permission mode

On the Safety tab each bot can follow the default or choose its own mode. Choosing *Follow the default* clears the bot's own
choice. If permissions are locked, bots cannot choose their own.

## What is edited in the config file

A few settings hold tables or commands that a form cannot show honestly, so the page lists them and says where they are:
per-model limit overrides, context-window sizes, language-server commands, MCP trust and the Docker sandbox options. Edit
these in `config/backends.yaml`.

## For developers

Every setting is described once in `bot/agent_runtime/settings_schema.py` (label, help, type, bounds, default, tab, section).
The page draws its forms from `GET /api/agent/config/schema`, so a setting cannot exist without appearing on the page:
`tests/test_agent_settings_schema.py` fails if a key in the shipped config has no entry. Routes:

| Route | Purpose |
| --- | --- |
| `GET /api/agent/config/schema` | The description of every setting, the tabs and the config-file-only list |
| `GET /api/agent/config` | Current values (the default where nothing is set) and which are explicitly configured |
| `POST /api/agent/config` | `{changes: {<id>: value}}`; `422` with `{errors: {<id>: reason}}` if any is invalid, and nothing is written |
| `POST /api/agent/config/reset` | `{ids: [...]}`, back to defaults |
| `GET /api/agent/overview` | Readiness checks, counts and the agent bots with their effective settings |
| `GET /api/agent/tools` | Every tool with its permission class and whether it asks first |

The page script, `agents-panel.js`, is one file kept identical in `bot/dashboard/static/` and `desktop-app/ui/`.
