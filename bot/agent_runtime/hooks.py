"""Claude Code-style lifecycle hooks for the native agent loop (Phase E
of the Claude API/Claude Code parity plan) — operator-configured local
automation triggered at real points in the tool-calling loop.

Deliberately scoped to the four events that map cleanly onto real,
already-centralized BotServer call sites, not Claude Code's full ~20-
event surface: `PreToolUse`/`PostToolUse` (both fire from
`bot.agent_runtime.tool_loop.run_one_tool()`, the one choke point every
tool call already passes through), `SessionStart` (fires on
`NativeAgentBackend.ask()`'s `lazily_created` branch), `UserPromptSubmit`
(fires at the very top of `ask()`, before compression/history-append).

Reuses the plugin trust model exactly (`bot/plugins.py`, ADR-0007) rather
than inventing a second "trusted local code" mechanism: a hook is an
operator-configured local shell command, given the event's JSON on
stdin, expected to emit a JSON object on stdout:
`{"decision": "allow"|"deny"|"ask", "reason": ..., "additionalContext": ...}`.
Real, stated scope-limits versus Claude Code's own hook contract:
- Only `PreToolUse` ever acts on `decision`/`reason` — `PostToolUse`/
  `SessionStart`/`UserPromptSubmit` only ever read `additionalContext`
  from a hook's output, matching the plan's own description.
- `PreToolUse`'s own `additionalContext` (if a hook returns one) is
  intentionally NOT injected anywhere — there is no `updatedInput`-style
  mechanism here, and no natural place to surface pre-call context
  without inventing one; only `decision`/`reason` are acted on for this
  event.
- No async/background hook modes, no HTTP/MCP-tool/prompt/agent hook
  types — every hook here is a synchronous local command.
- Never agent-creatable — hooks are dashboard/Telegram-config-only,
  the same trust boundary `create_plugin` already requires human
  approval for, but with no lever for an agent to add one at all.

A broken/slow/misbehaving hook command must never break the turn it's
attached to: any failure (can't spawn, times out, non-JSON output) is
logged and treated as "no opinion" (an implicit allow for PreToolUse, no
context for the others) — the exact same fail-open stance
`bot/agent_runtime/checkpoints.py`'s auto-checkpoint and
`bot/agent_runtime/vision.py`'s dropped-attachment handling already use.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Optional

logger = logging.getLogger("bot.agent_runtime.hooks")

HOOK_TIMEOUT_S = 30
MAX_HOOK_OUTPUT_CHARS = 4000

VALID_EVENTS = frozenset({"PreToolUse", "PostToolUse", "SessionStart", "UserPromptSubmit"})


async def _run_hook_command(command: str, payload: dict) -> Optional[dict]:
    """Runs one hook's shell command with `payload` as JSON on stdin,
    parses a JSON object from stdout. Returns None (never raises) on any
    failure."""
    try:
        proc = await asyncio.create_subprocess_shell(
            command, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
    except OSError:
        logger.exception("hook command failed to start: %s", command)
        return None
    try:
        stdout, _stderr = await asyncio.wait_for(
            proc.communicate(json.dumps(payload).encode("utf-8")), timeout=HOOK_TIMEOUT_S,
        )
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        logger.warning("hook command timed out after %ss: %s", HOOK_TIMEOUT_S, command)
        return None
    text = stdout.decode(errors="replace").strip()[:MAX_HOOK_OUTPUT_CHARS]
    if not text:
        return None
    try:
        result = json.loads(text)
    except json.JSONDecodeError:
        logger.warning("hook command produced non-JSON stdout, ignoring: %s", command)
        return None
    return result if isinstance(result, dict) else None


def _matching_hooks(event: str, matcher_value: Optional[str], instance_id: Optional[int]) -> list:
    from bot import db

    # Deliberately does NOT pass instance_id into list_agent_hooks() —
    # that function's own instance_id filter means "list every row
    # relevant to management for this instance" (global + scoped,
    # documented on list_external_mcp_servers()'s identical convention),
    # which is right for a dashboard listing but wrong here: a runtime
    # call with instance_id=None must see ONLY global hooks, not every
    # hook in the table regardless of scope. Filtering in Python below
    # keeps that runtime semantic correct without changing the DB
    # function's own established (and still-correct-for-its-callers)
    # contract.
    rows = db.list_agent_hooks(event=event)
    matched = []
    for row in rows:
        if not row["enabled"]:
            continue
        if row["instance_id"] is not None and row["instance_id"] != instance_id:
            continue
        matcher = row["matcher"]
        if matcher and matcher_value is not None and matcher != matcher_value:
            continue
        matched.append(row)
    return matched


async def run_pre_tool_use(tool_name: str, tool_input: dict, *, instance_id: Optional[int] = None) -> tuple[str, Optional[str]]:
    """Returns (decision, reason). decision is "allow" when no hook
    matches, or every matching hook allows; the first hook to return
    "deny" or "ask" wins (later hooks aren't consulted — matches the
    approval-gate's own single-outcome shape this feeds into)."""
    for hook in _matching_hooks("PreToolUse", tool_name, instance_id):
        result = await _run_hook_command(hook["command"], {"event": "PreToolUse", "tool_name": tool_name, "tool_input": tool_input})
        if result is None:
            continue
        decision = result.get("decision")
        if decision in ("deny", "ask"):
            return decision, result.get("reason")
    return "allow", None


async def run_post_tool_use(tool_name: str, tool_input: dict, output: str, *, instance_id: Optional[int] = None) -> None:
    for hook in _matching_hooks("PostToolUse", tool_name, instance_id):
        await _run_hook_command(
            hook["command"], {"event": "PostToolUse", "tool_name": tool_name, "tool_input": tool_input, "output": output},
        )


async def run_session_start(*, instance_id: Optional[int] = None) -> Optional[str]:
    return await _run_context_hooks("SessionStart", {}, instance_id)


async def run_user_prompt_submit(prompt: str, *, instance_id: Optional[int] = None) -> Optional[str]:
    return await _run_context_hooks("UserPromptSubmit", {"prompt": prompt}, instance_id)


async def _run_context_hooks(event: str, payload: dict, instance_id: Optional[int]) -> Optional[str]:
    contexts: list[str] = []
    for hook in _matching_hooks(event, None, instance_id):
        result = await _run_hook_command(hook["command"], {"event": event, **payload})
        if result and result.get("additionalContext"):
            contexts.append(str(result["additionalContext"]))
    return "\n".join(contexts) if contexts else None
