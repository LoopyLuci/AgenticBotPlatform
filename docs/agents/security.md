# Agent security: permissions, untrusted content, credentials, sandbox

The native agent can read files, change them, run commands and (when enabled) reach the
web and external tools. This page is how that power is controlled. Everything here lives in
`config/backends.yaml` under `native_agent`, and a host embedding ABP can lock it (see
[Host lock](#host-lock)). The design context is [ROADMAP.md](ROADMAP.md), phase P2.

## The order things are checked

For every tool call, in this order:

1. **Credential guard** - a network or external tool call whose arguments contain one of the
   server's secrets is refused.
2. **Permission rules and mode** - allow / ask / deny.
3. **Hooks** - a `PreToolUse` hook may deny, ask, or rewrite the input (a rewritten call goes
   back through step 2).
4. **Approval** - if a person has to approve, the call waits for them.
5. **Run** - in the [sandbox](#sandbox).
6. **Result** - credentials removed from the output, size capped (the full text is saved in
   the workspace), and the session marked if the output is untrusted.

## Permission rules

```yaml
native_agent:
  permissions:
    mode: default            # default | plan | accept_edits | bypass
    allow_bypass: false      # bypass is ignored unless the host turns this on
    locked: false
    rules:
      - {decision: allow, tool: run_shell, match: "git status*"}
      - {decision: allow, tool: run_shell, match: "python -m pytest*"}
      - {decision: deny,  tool: run_shell, match: "rm *"}
      - {decision: ask,   tool: "class:write", match: "config/**"}
      - {decision: allow, tool: web_fetch, match: "docs.python.org"}
```

A rule names a **tool** (`run_shell`), a **class** (`class:read`, `class:write`, `class:execute`,
`class:network`, `class:agent`, `class:config`, `class:admin`) or `*`, and optionally a **match**
on what the call touches: the command for `run_shell`, the path for file tools (`**` crosses
folders, `*` does not), the host for `web_fetch`, the query for `web_search`.

**Precedence:** a matching deny wins; then plan mode; then a matching ask; then a matching allow
(or accept-edits / bypass mode); otherwise the tool's own setting applies (anything that can
change something asks first).

**Shell commands are matched strictly.** An *allow* rule never matches a command that contains a
shell operator (`; | & < > `` ` `` $(` or a newline), so allowing `git status*` does not allow
`git status; rm -rf ~`. A *deny* rule matches any part of a compound command, so denying `rm *`
catches `ls; rm -rf /`.

**Modes:**

| Mode | Effect |
|---|---|
| `default` | rules apply; otherwise the normal approval prompts |
| `plan` | nothing that can change something may run; reads and the task list still work |
| `accept_edits` | edit tools (`edit_file`, `multi_edit`, `apply_patch`, `write_file`) are allowed without asking; commands still ask |
| `bypass` | everything is allowed except admin tools and denied rules - only if `allow_bypass: true` |

Admin tools are never allowed by a rule or a mode; a person approves each one.

Per-bot rules and mode are stored on the bot instance (`PUT /api/instances/{id}/permissions`) and
add to the host rules. A run may also ask for a mode (`context["permission_mode"]`).

## Untrusted content

Text that comes from outside - a web page, a search result, an external MCP server - can contain
instructions aimed at the agent. The agent is told to treat it as data; this is the enforcement.
Once a session has read such content it is **marked**, and then:

* every "allow" for a tool that can change something becomes "ask", whatever the rule or mode;
* standing approvals ("always allow run_shell") are ignored, and an answer given now is never
  saved as a standing grant;
* the "unrestricted" device tier's automatic approval is suspended;
* delegating (`spawn_subagent`, ...) and reconfiguring need approval.

Read-only tools keep working, so research does not stall. The mark lasts for the session; a
person can clear it (`POST /api/agent/taint/clear`).

External MCP servers are untrusted unless you say otherwise:

```yaml
native_agent:
  mcp_trust:
    my-internal-server: trusted
```

**MCP tool pinning.** The first time an MCP tool is seen, a fingerprint of its description and
input schema is recorded. If either later changes, the tool is blocked - not offered, not
callable - until a person reviews and approves it (`GET /api/mcp/pins`, `POST
/api/mcp/pins/approve`). That is the defence against a server quietly rewriting a tool's
description to smuggle in instructions. Turn off with `mcp_pinning: false`.

## Credentials

* **Not in output.** Values of environment variables named like a credential (token, secret,
  password, api key, private key, ...) and any values injected into the sandbox are replaced in
  tool output by `[secret:NAME]` before the model or the transcript sees them.
* **Not in requests.** A network or external tool call whose arguments contain such a value
  (including percent-encoded inside a URL) is refused.
* **Not in the command's environment.** See below.

## Sandbox

```yaml
native_agent:
  sandbox:
    backend: local            # local | docker | ssh | wsl | windows_job
    network: allow            # allow | none - none takes every backend offline (below)
    network_none:
      extra_paths: []         # Windows: folders an offline command may read and run programs from
    env:
      mode: secrets           # secrets | minimal | inherit
      allow: []
      set: {}                 # NAME: value, or NAME: "${SERVER_ENV_VAR}"
    docker:
      image: python:3.11-slim
      network: none           # none | bridge (host is refused)
      memory: 1g
      cpus: "2"
      pids: 256
    ssh:
      host: ""                 # required
      port: 22
      user: ""
      identity_file: ""
      remote_workspace_root: ""  # required
      connect_timeout: 10
    wsl:
      distro: ""                # "" = the default distro
    windows_job:
      memory_mb: 0               # 0 = unlimited
      active_process_limit: 0    # 0 = unlimited
```

`run_shell` used to inherit the server's whole environment, API keys included. The default
`secrets` mode now removes credential-shaped variables; `minimal` keeps only harmless system
variables plus `allow`; `inherit` restores the old behaviour. `set` injects values (and registers
them for redaction).

`backend: docker` runs each command in a fresh container: the workspace mounted at `/workspace`,
no network by default, memory / cpu / process limits, all Linux capabilities dropped, no
privilege escalation. **It fails closed:** if Docker is missing or not running the command is
refused, never quietly run on the host. **Verified against a real, running Docker Desktop daemon**
(not only the stand-in `docker` program `tests/test_sandbox.py` uses): a real container runs the
command and its writes land back on the host workspace, `network: none` genuinely refuses an
outbound connection instead of only asking for one, the host's own environment secrets are not
visible inside the container, a timeout stops and removes the real container promptly, and a
missing image fails the command instead of quietly running it on the host
(`tests/test_sandbox_live_docker.py`, skipped automatically where no real daemon is reachable).

`backend: ssh` runs the command on a configured, already-trusted remote host (a host key already in
`known_hosts`, key-based auth only - this backend never handles a password) via the local `ssh`
client. Fails closed if `ssh` is missing, `sandbox.ssh.host` or `sandbox.ssh.remote_workspace_root`
isn't set, or the connection fails. **Verified against a real OpenSSH server**
(`tests/test_sandbox_live_ssh.py`, which builds a throwaway Alpine sshd container, pins its host
key in a private `known_hosts` and connects with a generated key; it runs wherever Docker is
available): the command runs as the configured user in the translated folder, exit codes come back,
a timeout kills the remote process as well as the local client, and an unknown host key refuses the
command. That run found that every command which finished normally left a `.abp-*.pid` file in the
remote workspace (and, for `wsl`, in the local one); the remote shell now removes it on exit.
`ABP_TEST_SSH_HOST` still points the older opt-in test at a host of your own.
**Honest limit:** only the *command* runs remotely -
`read_file`/`write_file` and the other file tools still operate on the local workspace, so local and
remote file state only stay in sync if something outside this backend keeps
`remote_workspace_root` in sync with the local workspace. Full remote file tools are roadmap P6's
"cloud computer," not this backend.

`backend: wsl` runs the command inside a WSL2 distro on the same machine via `wsl.exe`, translating
the workspace path onto the distro's default drive-automount path (`/mnt/<drive>/...`). Fails
closed if `wsl.exe` is missing. Verified against a real, registered WSL distro on the development
machine (`tests/test_sandbox_wsl.py::TestLiveWsl`, skipped where none is registered).

`backend: windows_job` runs locally, like `local`, but assigns the process to a real Win32 Job
Object (`bot/agent_runtime/win_job.py`, stdlib `ctypes` only) with kill-on-close set, so the whole
process tree is guaranteed to die when the command is stopped - stronger than the `taskkill /T /F`
tree-walk `local`/`docker` fall back to, which can lose a race against a process that forks quickly
or deliberately detaches. Windows-only; refused elsewhere. Verified for real (`CreateJobObject`,
`AssignProcessToJobObject`, `TerminateJobObject`) in `tests/test_sandbox_windows_job.py`, which
needs no external service and so is never skipped on Windows.

### Offline commands: `network: none`

`network: none` cuts commands off from the network on every backend. Each backend uses the
strongest mechanism that needs no elevation, and all of them **fail closed**: if the host cannot
enforce it, the command is refused, never run online.

| Backend | How the network is removed |
|---|---|
| `local`, `windows_job` on Windows | An **AppContainer** with no capabilities (`bot/agent_runtime/appcontainer.py`). The kernel denies all network access, including loopback and DNS. |
| `local` on Linux | A new, empty network namespace (`unshare --user --net`). Some distros turn off unprivileged user namespaces; commands are then refused with the reason. |
| `local` on macOS | `sandbox-exec` with a profile that denies every network operation. |
| `docker` | `--network none`, whatever `docker.network` says. |
| `ssh`, `wsl` | The remote side runs the command under `unshare -rn`. If `unshare` is missing or refused there, the command fails before it starts. |

**How the Windows path works.** Python cannot start a process in an AppContainer, so a small
launcher (the same file, run as a script) does it:
1. It creates the `ABP.Sandbox.Offline` profile once per user, and grants it access to the workspace.
2. It starts `cmd.exe /c <command>` inside the container, suspended.
3. It places the process in a kill-on-close Job Object, then lets it run.

Nothing the command starts can escape the job, so this closes the small race `windows_job`
documents. With `backend: windows_job`, its memory and process limits apply inside the container.

**On Windows the file system is confined too.** A container process can only open:
- the workspace;
- system folders;
- folders listed in `network_none.extra_paths`, which it can read and run programs from but not write.

Your user folder is out of reach, including `.ssh`, browser profiles and saved credentials. So is a
per-user Python or Node install, until you list it. This is deliberate: a network block that a
command could bypass by reading your saved tokens would not be worth much. Write an absolute
program path in quotes (`"C:\tools\x.exe"`): cmd.exe's lookup of an unquoted absolute path can be
refused inside a container, while a quoted path or a `PATH` lookup works.

**Verified for real** (`tests/test_sandbox_offline.py`):
- On Windows, the same `curl` that reaches a loopback server online cannot reach it offline, and
  DNS fails.
- Files outside the workspace cannot be read, and extra paths are read-only.
- A timeout kills the contained tree.
- `windows_job` limits hold inside the container.
- A launcher failure refuses the command.
- Inside a real WSL distro, an offline command cannot open a socket.

**Still open.** With `network: allow` (the default), the `local` and `windows_job` backends
confine neither the network nor the file system beyond the file tools' workspace guard. There is no
per-host allow list either: a command is fully online or fully offline. Allowing a chosen host would
need a filtering proxy.

## Hooks

Ten events: `PreToolUse`, `PostToolUse`, `PostToolUseFailure`, `SessionStart`, `SessionEnd`,
`UserPromptSubmit`, `Stop`, `SubagentStop`, `PreCompact`, `Notification`. A hook is a local
command (JSON on stdin and stdout) or an `http(s)://` URL (JSON in a POST and in the reply).

* `PreToolUse` may return `{"decision": "deny"|"ask", "reason": ...}` or
  `{"updatedInput": {...}}` to replace the arguments. A rewritten call is judged again by the
  permission rules.
* `Stop` may return `{"decision": "block", "reason": ...}` to make the agent carry on (at most
  twice per turn).
* `PreCompact` may return `{"additionalContext": ...}` to add instructions for the history
  summary.

A hook that fails, times out or answers something odd is treated as having no opinion.

## Host lock

A host that embeds ABP can fix the agent's permissions:

```yaml
native_agent:
  permissions:
    locked: true
    mode: default
    rules: [ {decision: deny, tool: "class:admin"} ]
```

With `locked: true` per-bot rules and modes are ignored, `PUT /api/instances/{id}/permissions`
answers `409 permissions_locked`, and a run can only make the mode stricter, never looser.

## What is and is not covered

Covered by tests: rule matching and precedence (including the shell-operator cases), modes,
untrusted-content escalation through the real tool loop, credential redaction and the outbound
guard, environment scrubbing (a command really cannot read a server secret), MCP pinning, hooks,
the API, and six eval tasks that fail when their defence is removed.

Not covered: a determined prompt injection that persuades a *person* to approve a harmful action
(the approval prompt shows the tool and its arguments, but people click through); reading files
outside the workspace with a locally-run shell command; secrets that do not have a
credential-shaped name; anything a real Docker daemon would do.
