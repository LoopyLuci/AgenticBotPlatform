# Named agents, skill packs and custom commands

## Named agents

A Markdown file with front matter defines an agent the main agent can hand a task to
(`spawn_subagent` with `agent: "name"`, `list_agents` to see them):

```markdown
---
name: reviewer
description: Reviews a change for bugs and missing tests. Use after finishing a change.
tools: read_file, grep, glob, git_diff
model: openrouter/qwen/qwen3.8-27b:free      # optional
mode: plan                                    # optional; only "plan" (read-only) is honoured
isolation: worktree                           # optional; run in a throwaway git worktree
---
You review code. Be specific; cite file and line.
```

Looked for, later folders winning: `.claude/agents`, `.opencode/agent(s)`, `.abp/agents` in the project,
then `agents/` in the ABP data folder. Built-ins: `explore`, `plan`, `reviewer`, `general`.

A definition can only **narrow** a child. It can remove tools, force read-only and pick a model, but it
cannot grant a tool the parent lacks, switch off approvals, or lift a permission rule. Claude Code and
OpenCode tool names (`Read`, `Grep`, `Bash`, `Edit`, ...) are translated. With `isolation: worktree` the
child works in its own git worktree; its result says where, and it is removed if unchanged.

## Skill packs (SKILL.md)

A folder with a `SKILL.md` (front matter `name`, `description`) and any bundled files. Found in
`.claude/skills`, `.agents/skills`, `.abp/skills` in the project and the user's `skill_packs` folder.
Only the name and description are in the prompt; `read_skill` loads the instructions and
`read_skill_file` a bundled file, so a large pack costs nothing until it is used. A pack's
`allowed-tools` is information only; it grants nothing.

### Installing from git: quarantine

`/skills fetch <https git url> [ref]` (admins) or `POST /api/skills/fetch` clones shallowly from an
allowed host (`github.com`, `gitlab.com`, `bitbucket.org`, `codeberg.org` plus
`native_agent.skills.allowed_hosts`), scans, and holds the pack in **quarantine**. Nothing in quarantine is
visible to the model. The scan blocks pipe-to-shell, decode-and-exec, `rm -rf /`, reading `~/.ssh` and
cloud credentials, symlinks, binaries, oversized packs; it warns on mentions of key files. An Ed25519
`SKILL.sig` is verified against `native_agent.skills.trusted_keys`, but **a signature never overrides a
block**. `/skills approve <name>` (or the API) is a person's decision; there is no automatic path.

### Skill drafts

With `native_agent.skill_learning.enabled: true` (off by default; costs one extra model call), a turn that
used at least `min_tool_calls` tools ends with one no-tools question: was a reusable procedure revealed?
A well-formed, secret-free, script-free answer becomes a **draft** for `/skills drafts`,
`/skills approve-draft <name>` or `/skills reject-draft <name>`. Drafts are never shown to the model.

## Custom slash commands

`.claude/commands/name.md`, `.opencode/command(s)/name.md`, `.abp/commands/name.md` or the user's
`commands` folder. The file body is the prompt; `$ARGUMENTS` and `$1`..`$9` are replaced with what the
user typed. `/commands` lists them. A command file cannot override a built-in command.

## Not done

A public skill registry or search; running a skill's bundled scripts under the sandbox as a first-class
step (the agent runs them like any other command, under the normal permission rules); agent definitions
with a `hooks:` block; nested agent definitions in sub-folders.
