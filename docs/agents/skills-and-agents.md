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

### Linked libraries

`native_agent.skills.external_dirs` adds folders of skills that are read in place, such as another agent's library
(`abp_import hermes` and `abp_import openclaw` link theirs). An entry is a path, or `{path, exclude: [names]}`. Skills
may sit up to three folders deep, as Hermes groups them by category; hidden folders such as `.archive` are skipped. Your
own skills win over a library's, and a library's over a repository's.

Libraries can be large: a real Hermes install holds about 10,000 skills.
- Each `SKILL.md` is parsed once and reused until it changes. Bundled files are listed only when a skill is opened.
- A library loads and refreshes (every minute) on a background thread, so no turn waits for it.
- The prompt names at most 40 skills and then says how many more there are. The agent searches them with `list_skills`
  and a `query`; without a query, `list_skills` returns 100 and says how many it left out.

Measured on that 10,000-skill library:

| Step | Time |
|---|---|
| First load (in the background) | 4.8 s |
| A later turn | 1 ms |
| A search | 13 ms |

Front matter made of plain `key: value` lines is read without YAML. That is faster, and it also reads descriptions such as
`description: Affiliate: commissions`, which strict YAML rejects. About one skill in a hundred in that library is written
this way, and those skills were invisible to the model before.

### Installing from git: quarantine

`/skills fetch <https git url> [ref]` (admins) or `POST /api/skills/fetch` clones shallowly from an
allowed host (`github.com`, `gitlab.com`, `bitbucket.org`, `codeberg.org` plus
`native_agent.skills.allowed_hosts`), scans, and holds the pack in **quarantine**. Nothing in quarantine is
visible to the model. The scan blocks pipe-to-shell, decode-and-exec, `rm -rf /`, reading `~/.ssh` and
cloud credentials, symlinks, binaries, oversized packs; it warns on mentions of key files. An Ed25519
`SKILL.sig` is verified against `native_agent.skills.trusted_keys`, but **a signature never overrides a
block**. `/skills approve <name>` (or the API) is a person's decision; there is no automatic path.
An installed pack is removed with `DELETE /api/skills/packs/{name}`, `/skills remove <name>` (bot admins) or the Remove
button on the Skills screen; only the user's own pack folder is touched, and the removal is audit-logged.

### Skill drafts

With `native_agent.skill_learning.enabled: true` (off by default; costs one extra model call), a turn that
used at least `min_tool_calls` tools ends with one no-tools question: was a reusable procedure revealed?
A well-formed, secret-free, script-free answer becomes a **draft** for `/skills drafts`,
`/skills approve-draft <name>` or `/skills reject-draft <name>`. Drafts are never shown to the model.

**Draft check.** Next to every draft ABP runs an automatic, deterministic check (no model call):
`GET /api/skills/drafts/{name}/evaluation` or `/skills check-draft <name>`. It re-runs the format lint, looks for secrets,
re-runs the skill scanner's patterns, checks the size against the skill budget, flags instructions that try to change
permissions, approvals or the tool list or that name credential files, and tests the description: it must match the draft's
own `Example:` tasks and must not match unrelated ones. The verdict (`pass`, `review`, `fail`) is **advice only**: it never
approves, rejects or edits a draft. A scripted run of the agent over the draft was not feasible without a model, so the
behavioural check is a lint.

### The Skills screen

The dashboard and the desktop app have a Skills screen (`skills-panel.js`, the same file in both): installed packs,
the quarantine (scan findings, signature status, approve or reject after a confirmation), drafts (read the text, see the
check, approve or reject) and a form that fetches a git URL into quarantine. Approving uses the write-auth routes, so a
paired phone can read the screen but not approve. Skill text is shown as text, never as HTML. There is no remove button
for installed packs because there is no route for it; delete the folder in the skill folder.

## Custom slash commands

`.claude/commands/name.md`, `.opencode/command(s)/name.md`, `.abp/commands/name.md` or the user's
`commands` folder. The file body is the prompt; `$ARGUMENTS` and `$1`..`$9` are replaced with what the
user typed. `/commands` lists them. A command file cannot override a built-in command.

## Agent definitions: sub-folders and hooks

Agent folders are searched recursively (three levels). `.abp/agents/review/security.md` is the agent
`review/security` (use that in `spawn_subagent`) unless its front matter has its own `name:`. Two files with
the same name: the one that sorts last wins and a warning is logged.

A `hooks:` block in the front matter holds hooks that run only while that agent runs, after the global hooks,
and can only narrow (deny / ask / block / add context; "allow" and `updatedInput` are dropped):

    hooks:
      PreToolUse:
        - matcher: run_shell
          command: python check_cmd.py
        - {type: prompt, prompt: "Is this command destructive?", model: openrouter/small, matcher: run_shell}

Only the user's own agent folder may define hooks; the block is ignored (and listed by `list_agents`) in a
project's agent file, because a repository must not be able to run commands.

## Not done

A public skill registry or search; running a skill's bundled scripts under the sandbox as a first-class
step (the agent runs them like any other command, under the normal permission rules); more hook events.
