"""Claude Code-style lifecycle hooks for the native agent loop (Phase E
of the Claude API/Claude Code parity plan) — operator-configured local
automation triggered at real points in the tool-calling loop.

Ten events, each at a real call site:
`PreToolUse` / `PostToolUse` / `PostToolUseFailure` (from `tool_loop`, the one
choke point every tool call passes through), `SessionStart` and `UserPromptSubmit`
(top of `NativeAgentBackend.ask()`), `Stop` (the agent has a final answer; a hook may
ask it to keep going), `SubagentStop` (a sub-agent finished), `PreCompact` (history is
about to be summarised; a hook may add instructions for the summary), `Notification`
(the agent needs a person: an approval is waiting, a turn hit a limit) and `SessionEnd`
(`/new` started a fresh session). Not Claude Code's full surface.

Reuses the plugin trust model exactly (`bot/plugins.py`, ADR-0007) rather
than inventing a second "trusted local code" mechanism: a hook is an
operator-configured local shell command, given the event's JSON on
stdin, expected to emit a JSON object on stdout:
`{"decision": "allow"|"deny"|"ask", "reason": ..., "additionalContext": ...}`.
What a hook may say:
- `PreToolUse`: `decision` (allow / deny / ask) with a `reason`, and `updatedInput` - a
  replacement for the tool's arguments. A rewritten call is judged again by the permission
  rules, so a hook cannot use a rewrite to get past a deny rule.
- `Stop`: `{"decision": "block", "reason": "..."}` makes the agent carry on (at most twice
  per turn), with the reason as its next instruction.
- `PreCompact`: `additionalContext` is added to the instructions for the history summary.
- `SessionStart`, `UserPromptSubmit`: `additionalContext` is added to the conversation.
- The rest are notifications: their output is ignored.
- A hook is either a local command (JSON on stdin, JSON on stdout) or, when the "command"
  starts with http:// or https://, a URL that receives the same JSON in a POST and answers
  with the same JSON. Hooks are operator configuration and trusted, so a URL may be on a
  private address.
- Still not built: async / background hook modes, MCP-tool, prompt and agent hook types.
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
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger("bot.agent_runtime.hooks")

HOOK_TIMEOUT_S = 30
MAX_HOOK_OUTPUT_CHARS = 4000

VALID_EVENTS = frozenset({
    "PreToolUse", "PostToolUse", "PostToolUseFailure", "SessionStart", "SessionEnd", "UserPromptSubmit",
    "Stop", "SubagentStop", "PreCompact", "Notification",
})
MAX_HTTP_RESPONSE_BYTES = 65536


async def _run_hook_command(command: str, payload: dict) -> Optional[dict]:
    """Runs one hook's shell command with `payload` as JSON on stdin,
    parses a JSON object from stdout. Returns None (never raises) on any
    failure. A "command" that is an http(s) URL is POSTed the payload instead."""
    if command.startswith(("http://", "https://")):
        return await _run_http_hook(command, payload)
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


async def _run_http_hook(url: str, payload: dict) -> Optional[dict]:
    import httpx

    try:
        async with httpx.AsyncClient(timeout=HOOK_TIMEOUT_S) as client:
            resp = await client.post(url, json=payload)
        if resp.status_code >= 300 or not resp.content:
            if resp.status_code >= 400:
                logger.warning("hook URL %s answered HTTP %s, ignoring", url, resp.status_code)
            return None
        result = resp.json() if len(resp.content) <= MAX_HTTP_RESPONSE_BYTES else None
    except (httpx.HTTPError, ValueError):
        logger.warning("hook URL failed or answered something that is not JSON: %s", url, exc_info=True)
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


@dataclass
class PreToolResult:
    decision: str = "allow"                 # allow | deny | ask
    reason: Optional[str] = None
    updated_input: Optional[dict] = None    # a hook's replacement arguments, if it gave one


async def run_pre_tool_use_full(tool_name: str, tool_input: dict, *, instance_id: Optional[int] = None) -> PreToolResult:
    """The first hook to say "deny" or "ask" wins (later hooks are not consulted); a hook's
    `updatedInput` (a JSON object) replaces the arguments. A deny always beats a rewrite."""
    updated: Optional[dict] = None
    current = tool_input
    for hook in _matching_hooks("PreToolUse", tool_name, instance_id):
        result = await _run_hook_command(
            hook["command"], {"event": "PreToolUse", "tool_name": tool_name, "tool_input": current})
        if result is None:
            continue
        decision = result.get("decision")
        if decision in ("deny", "ask"):
            return PreToolResult(decision, result.get("reason"), updated)
        if isinstance(result.get("updatedInput"), dict):
            updated = current = result["updatedInput"]
    return PreToolResult("allow", None, updated)


async def run_pre_tool_use(tool_name: str, tool_input: dict, *, instance_id: Optional[int] = None) -> tuple[str, Optional[str]]:
    """Returns (decision, reason). decision is "allow" when no hook
    matches, or every matching hook allows; the first hook to return
    "deny" or "ask" wins (later hooks aren't consulted — matches the
    approval-gate's own single-outcome shape this feeds into)."""
    result = await run_pre_tool_use_full(tool_name, tool_input, instance_id=instance_id)
    return result.decision, result.reason


async def run_post_tool_use(tool_name: str, tool_input: dict, output: str, *, instance_id: Optional[int] = None) -> None:
    for hook in _matching_hooks("PostToolUse", tool_name, instance_id):
        await _run_hook_command(
            hook["command"], {"event": "PostToolUse", "tool_name": tool_name, "tool_input": tool_input, "output": output},
        )


async def run_post_tool_failure(tool_name: str, tool_input: dict, error: str, *, instance_id: Optional[int] = None) -> None:
    for hook in _matching_hooks("PostToolUseFailure", tool_name, instance_id):
        await _run_hook_command(hook["command"], {
            "event": "PostToolUseFailure", "tool_name": tool_name, "tool_input": tool_input, "error": error})


async def run_stop(reply: str, *, instance_id: Optional[int] = None, stop_reason: str = "end_turn") -> Optional[str]:
    """The agent has a final answer. Returns a reason to keep going if a hook blocks the stop, else None."""
    for hook in _matching_hooks("Stop", None, instance_id):
        result = await _run_hook_command(hook["command"], {"event": "Stop", "reply": reply[:4000], "stop_reason": stop_reason})
        if result and result.get("decision") == "block":
            return str(result.get("reason") or "A Stop hook asked you to keep going.")
    return None


async def run_subagent_stop(result_text: str, *, instance_id: Optional[int] = None) -> None:
    for hook in _matching_hooks("SubagentStop", None, instance_id):
        await _run_hook_command(hook["command"], {"event": "SubagentStop", "result": result_text[:4000]})


async def run_pre_compact(*, instance_id: Optional[int] = None, messages: int = 0) -> Optional[str]:
    return await _run_context_hooks("PreCompact", {"messages": messages}, instance_id)


async def run_notification(kind: str, message: str, *, instance_id: Optional[int] = None) -> None:
    for hook in _matching_hooks("Notification", None, instance_id):
        await _run_hook_command(hook["command"], {"event": "Notification", "kind": kind, "message": message[:1000]})


async def run_session_end(*, instance_id: Optional[int] = None, reason: str = "new_session") -> None:
    for hook in _matching_hooks("SessionEnd", None, instance_id):
        await _run_hook_command(hook["command"], {"event": "SessionEnd", "reason": reason})


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
