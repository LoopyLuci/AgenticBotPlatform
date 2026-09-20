"""Shared per-tool-call execution helper for backends that run AgenticBotPlatform's
own tool-use loop (currently ApiBackend and CustomModelBackend) — approval
gating, execution, and auto-checkpointing in one place so the two
backends can't silently drift on this shared, security-relevant path.
"""

from __future__ import annotations

import asyncio
import logging
import time

from bot.agent_runtime import toolspec, trace

logger = logging.getLogger("bot.agent_runtime.tool_loop")


async def run_one_tool(
    name, tool_input, *, workspace, instance_id, chat_id, session_key, notify, agent_tools, agent_approval,
    device_tier=None,
) -> str:
    """Runs one tool call and records it in the agent trace (shape only)."""
    started = time.monotonic()
    outcome = {"status": "ok", "approval": "none", "error": ""}
    output = ""
    try:
        output = await _run_one_tool(
            name, tool_input, workspace=workspace, instance_id=instance_id, chat_id=chat_id,
            session_key=session_key, notify=notify, agent_tools=agent_tools, agent_approval=agent_approval,
            device_tier=device_tier, outcome=outcome,
        )
        return output
    except BaseException as exc:
        outcome["status"] = "cancelled" if isinstance(exc, asyncio.CancelledError) else "failed"
        outcome["error"] = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        trace.active().tool_call(
            name, tool_input, status=outcome["status"], duration_ms=int((time.monotonic() - started) * 1000),
            output=output, approval=outcome["approval"], error=outcome["error"],
            read_only=not agent_tools.is_dangerous(name),
        )


async def _run_one_tool(
    name, tool_input, *, workspace, instance_id, chat_id, session_key, notify, agent_tools, agent_approval,
    device_tier, outcome,
) -> str:
    from bot.agent_runtime import hooks

    try:
        decision, hook_reason = await hooks.run_pre_tool_use(name, tool_input, instance_id=instance_id)
        if decision == "deny":
            outcome["status"], outcome["approval"] = "denied", "hook"
            return f"Denied by hook{f': {hook_reason}' if hook_reason else '.'}"
        # The "unrestricted" device tier (Server Chat/Support Bot only — see
        # the "Admin control surface" plan) skips the per-call approval
        # prompt specifically for run_shell/write_file, since that tier
        # assignment itself already represents standing consent. Never
        # applies to a hook-driven "ask" escalation, and never to any OTHER
        # dangerous tool (admin_* actions stay approval-gated regardless).
        unrestricted_relaxation = device_tier == "unrestricted" and name in ("run_shell", "write_file")
        if (agent_tools.is_dangerous(name) and not unrestricted_relaxation) or decision == "ask":
            if notify is None:
                # No chat to ask — approval.request_approval still waits out
                # its timeout and denies rather than silently running a
                # dangerous tool nobody could actually approve.
                async def _no_notify(_id, _name, _input):
                    logger.warning("tool %r needs approval but no notify channel is set — will time out and deny", name)

                notify_fn = _no_notify
            else:
                notify_fn = notify
            approval = await agent_approval.request_approval(
                instance_id, chat_id, session_key, name, tool_input, notify=notify_fn
            )
            outcome["approval"] = "human" if approval != "deny" else "denied"
            trace.active().approval(name, "deny" if approval == "deny" else "allow")
            if approval == "deny":
                outcome["status"] = "denied"
                return "Denied by user."
        elif unrestricted_relaxation:
            outcome["approval"] = "tier"
        output = await agent_tools.execute_tool(
            name, tool_input, workspace=workspace, instance_id=instance_id, device_tier=device_tier
        )
        if agent_tools.is_dangerous(name):
            try_checkpoint(workspace, name, tool_input)
        output = toolspec.limit_output(name, output)
        await hooks.run_post_tool_use(name, tool_input, output, instance_id=instance_id)
        return output
    except agent_tools.ToolError as exc:
        outcome["status"], outcome["error"] = "failed", str(exc)[:200]
        return f"Error: {exc}"


def try_checkpoint(workspace, name: str, tool_input: dict) -> None:
    """Best-effort auto-checkpoint after a tool call that may have changed
    the workspace — git failures here (no git installed, a workspace
    outside any writable filesystem, etc.) must never break the tool call
    that already succeeded, so this only logs."""
    from bot.agent_runtime import checkpoints

    label = tool_input.get("command") if name == "run_shell" else tool_input.get("path", name)
    try:
        checkpoints.create_checkpoint(workspace, str(label)[:100])
    except checkpoints.CheckpointError:
        logger.warning("auto-checkpoint failed for %s in %s", name, workspace, exc_info=True)
