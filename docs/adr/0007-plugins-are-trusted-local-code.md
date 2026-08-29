# ADR-0007: Plugins are trusted local code, not sandboxed, not a marketplace

**Status:** Accepted
**Date:** 2026-08-29

## Context

Phase B of the multi-provider/plugin/platforms roadmap (see the session's
own plan log) called for a plugin API: a way to add new agent tools and
slash commands to BotServer without editing core code. Before writing
`bot/plugins.py`, the codebase's actual security posture was checked, not
assumed: `bot/agent_runtime/tools.py`'s `run_shell` is a bare
`asyncio.create_subprocess_shell` with the full privileges of the
BotServer process, gated only by a human-approval prompt
(`bot/agent_runtime/approval.py`), not a sandbox or allowlist. The
project's own documented security model (`bot/agent_control.py`'s
docstring) is "single trusted operator, the dashboard token is the real
perimeter" — not multi-tenant isolation, and not defense against
malicious code the operator chose to run.

A plugin API that ran arbitrary third-party Python in-process with no
isolation would be a real, silent regression from that model if it
implied "safe to install anything" — it does not, and needs to say so
explicitly rather than let the feature's existence imply otherwise.

## Decision

A plugin is exactly as trusted as a `run_shell` command the operator
already approved: it runs in-process, as plain Python, with the full
privileges of the BotServer process. `bot/plugins.py`'s `install(path)`
only ever loads a `plugin.py` file already sitting on this machine's
disk — nothing fetches code over a network, mirroring `bot/skills.py`'s
existing "local install, not a hosted hub" precedent for the exact same
reason. There is no sandbox, no permission model finer than "installed
or not," and no signature/provenance verification, because building any
of those would imply a safety guarantee this codebase doesn't make
anywhere else.

A plugin registers into the same registries built-in tools and commands
already use (`bot/agent_runtime/tools.py`'s tool-execution path,
`bot/commands.py`'s command dispatch, `bot/slash_commands.py`'s
help/menu listings) via a small validated API (`register_tool`,
`register_command`) that only rejects name collisions with a built-in or
another plugin — it does not vet what the handler itself does. Installed
plugins are tracked in a `plugins` DB table (name, path, enabled,
description) so `enable`/`disable` can unregister a plugin's
tools/commands without losing install history, and a plugin that fails
to load at startup is logged and skipped rather than taking the whole
process down.

A real plugin *marketplace* — network fetch, an index, signature
verification — is deliberately out of scope here. None of that
infrastructure exists today, and building it is a separate, larger trust
decision (who signs what, what "verified" means) that this ADR does not
make.

## Consequences

Installing a plugin is a decision on par with running a shell command
found on the internet and approving it — the dashboard's "Plugins" card
says this plainly, not just this ADR. There's no technical barrier
stopping a malicious `plugin.py` from doing anything the BotServer
process itself can do (read the database, call `run_shell` directly,
exfiltrate secrets from `.env`). That's an accepted cost of shipping a
real extension point on a codebase whose stated perimeter is already
"trust the operator," not a gap introduced by this feature. If BotServer
ever needs to run plugins from operators it doesn't fully trust (a real
marketplace, multi-tenant hosting), that requires revisiting this
decision and `ADR-0003`'s single-process assumption together, not just
adding a sandbox on top of the current design.
