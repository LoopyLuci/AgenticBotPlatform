# ADR-0008: A single-instance/single-device admin-tool gate, not a general RBAC system

**Status:** Accepted
**Date:** 2026-09-08

## Context

Per the operator's request, one designated bot instance (Telegram, Server
Chat, or Support Bot) needed real conversational control over BotServer
itself — creating/updating/deleting other bot instances, changing the
global default backend, managing another instance's `agent_settings`/
auto-manage config, engaging the emergency stop, and managing lifecycle
hooks — plus a further tier of device/pairing management, destructive
local operations, and unrestricted shell/file access reserved for
Android/Server-Chat only.

ADR-0007 established that a *tool*, once it exists and is approved, runs
with the full privileges of the BotServer process — that's a decision
about code trust. It never addressed a different question: *which
conversational surface (which bot instance, which paired device) should
be allowed to invoke an already-trusted, already-powerful tool at all.*
Before this change, every top-level bot instance's agent-runtime tool
loop saw an identical tool list (confirmed by reading
`bot/agent_runtime/tools.py::all_tool_schemas()`, which takes no
per-caller parameter), and every paired device was equally privileged
against the dashboard REST API (confirmed via `api_keys`' flat
valid/revoked schema and `_identify_caller`'s own docstring, which
states "any mobile key = full parity with desktop" as a *deliberate,
separate* decision this ADR does not touch or reverse).

## Decision

Two narrow, additive privilege boundaries, not a general permission
system:

1. **Instance-to-instance**: `agent_settings.is_admin_instance` (a sparse
   boolean, same fallback-chain precedent as `require_plan_approval`)
   marks the one bot instance whose tool loop gets
   `bot/agent_runtime/tools.py`'s `ADMIN_TOOLS_STANDARD` set. Enforced at
   three independent points — schema filtering before the model ever
   sees the tools exist (`native_backend.py`), a defense-in-depth
   `_require_admin()` check at dispatch time, and the existing
   `DANGEROUS_TOOLS` human-approval gate for every state-changing one.
   Settable only through the operator-authenticated dashboard/MCP
   channel; the agent-runtime `admin_set_agent_settings` tool explicitly
   refuses to touch this field, so no instance can self-promote.

2. **Device-to-device**: `api_keys.permission_tier`
   (none/standard/elevated/unrestricted, `bot/device_tiers.py`) gates
   `ADMIN_TOOLS_ELEVATED` (device/pairing management, destructive local
   operations) and relaxes the dangerous-tool approval prompt for
   `run_shell`/`write_file` specifically at the `unrestricted` tier. A
   device may only ever manage (retier or revoke) a strictly
   lower-ranked device — never itself, never a peer or superior — and
   may only mint a new device at up to its own tier. The desktop
   `DASHBOARD_TOKEN` is the unconditional top authority, bypassing both
   checks.

`ADMIN_TOOLS_ELEVATED` requires **both** conditions at once (the calling
instance is the admin instance, and the turn carries an
elevated-or-higher `device_tier`) — a Telegram-driven turn never carries
a `device_tier` at all, so these tools are structurally unreachable from
Telegram regardless of `is_admin_instance`.

Both mechanisms are opt-in per tool (`ADMIN_TOOLS`/`LEAF_BLOCKED_TOOLS`
membership) — there is no default-safe list a new tool automatically
joins or is excluded from. A future tool with fleet-wide reach must be
explicitly added to these sets, or it's offered to every instance/device
exactly as everything else already is.

## Consequences

The pre-existing dashboard REST API's flat "any paired device = desktop
parity" model (`_identify_caller`) is **deliberately left untouched** —
retrofitting it to respect `permission_tier` would be a separate, much
larger change with its own blast radius, and wasn't what was asked for.
`permission_tier` and `is_admin_instance` only govern the *new*
conversational admin surface (agent-runtime tools, Server Chat's
BotServer pipeline, Support Bot's new actions) and device/pairing
management itself.

This is not a general RBAC system: there's no role hierarchy beyond the
four fixed device tiers, no per-tool custom policy, and no concept of
"admin instance" beyond the single boolean flag. If BotServer ever needs
finer-grained, per-tool authorization independent of these two axes,
that's a new decision to make deliberately, not an extension implied by
this one.
