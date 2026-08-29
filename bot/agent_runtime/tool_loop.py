"""Shared per-tool-call execution helper for backends that run BotServer's
own tool-use loop (currently ApiBackend and CustomModelBackend) — approval
gating, execution, and auto-checkpointing in one place so the two
backends can't silently drift on this shared, security-relevant path.
"""

from __future__ import annotations

import logging

logger = logging.getLogger("bot.agent_runtime.tool_loop")


async def run_one_tool(name, tool_input, *, workspace, instance_id, chat_id, session_key, notify, agent_tools, agent_approval) -> str:
    try:
        if agent_tools.is_dangerous(name):
            if notify is None:
                # No chat to ask (e.g. a call with no Telegram context at
                # all) — approval.request_approval still waits out its
                # timeout and denies rather than silently running a
                # dangerous tool nobody could actually approve.
                async def _no_notify(_id, _name, _input):
                    logger.warning("tool %r needs approval but no notify channel is set — will time out and deny", name)

                notify_fn = _no_notify
            else:
                notify_fn = notify
            outcome = await agent_approval.request_approval(
                instance_id, chat_id, session_key, name, tool_input, notify=notify_fn
            )
            if outcome == "deny":
                return "Denied by user."
        output = await agent_tools.execute_tool(name, tool_input, workspace=workspace, instance_id=instance_id)
        if agent_tools.is_dangerous(name):
            try_checkpoint(workspace, name, tool_input)
        return output
    except agent_tools.ToolError as exc:
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
