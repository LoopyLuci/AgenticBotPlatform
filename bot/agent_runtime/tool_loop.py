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
    from bot.agent_runtime import hooks, permissions, secrets_guard, taint

    try:
        # 1. Credentials never leave in a request: refuse a network / external call whose arguments
        #    contain a value from the server's secrets.
        spec = toolspec.spec_for(name)
        if spec.permission in ("network", "external"):
            leaked = secrets_guard.find_secret(tool_input)
            if leaked:
                outcome["status"], outcome["approval"], outcome["error"] = "failed", "guard", "credential in arguments"
                return (f"Error: refused - the arguments contain a value that looks like a credential ({leaked}). "
                        "Credentials are never sent to external tools.")

        # 2. Standing rules and the permission mode.
        verdict = permissions.evaluate(name, tool_input, workspace=workspace, instance_id=instance_id,
                                       session=session_key or "")
        if verdict.decision == "deny":
            outcome["status"], outcome["approval"] = "denied", "policy"
            trace.active().approval(name, "deny", verdict.reason[:200])
            return f"Denied by policy: {verdict.reason}"

        # 3. Hooks (a hook may deny, ask, or rewrite the input).
        hook = await hooks.run_pre_tool_use_full(name, tool_input, instance_id=instance_id)
        if hook.decision == "deny":
            outcome["status"], outcome["approval"] = "denied", "hook"
            return f"Denied by hook{f': {hook.reason}' if hook.reason else '.'}"
        if hook.updated_input is not None and hook.updated_input != tool_input:
            tool_input = hook.updated_input
            trace.active().note(f"a hook rewrote the input of {name}")
            # the rewritten call is judged afresh
            verdict = permissions.evaluate(name, tool_input, workspace=workspace, instance_id=instance_id,
                                           session=session_key or "")
            if verdict.decision == "deny":
                outcome["status"], outcome["approval"] = "denied", "policy"
                return f"Denied by policy: {verdict.reason}"

        # 4. Does a person have to approve this call?
        # The "unrestricted" device tier (Server Chat/Support Bot only) skips the per-call prompt for
        # run_shell / file changes, since the tier itself is standing consent. Never for a hook "ask",
        # never for any other dangerous tool, and never once the session has read untrusted content.
        tainted = taint.is_tainted(session_key or "")
        unrestricted_relaxation = device_tier == "unrestricted" and not tainted and name in (
            "run_shell", "write_file", "edit_file", "multi_edit", "apply_patch")
        dangerous = agent_tools.is_dangerous(name)
        if verdict.decision == "allow":
            needs_approval = False                       # a rule or mode allowed it (already downgraded if tainted)
        elif verdict.decision == "ask":
            needs_approval = True
        else:
            needs_approval = dangerous and not unrestricted_relaxation
        needs_approval = needs_approval or hook.decision == "ask"
        # A session that read untrusted content ignores standing approvals: a person sees each change.
        fresh = tainted and (dangerous or verdict.decision == "ask")
        if needs_approval:
            if notify is None:
                # No chat to ask - request_approval still waits out its timeout and denies rather than
                # silently running something nobody could actually approve.
                async def _no_notify(_id, _name, _input):
                    logger.warning("tool %r needs approval but no notify channel is set - will time out and deny", name)

                notify_fn = _no_notify
            else:
                notify_fn = notify
            kwargs = {"force": True} if fresh else {}
            # Tell any Notification hook a person is needed; never make the approval wait on it.
            asyncio.ensure_future(hooks.run_notification(
                "permission_request", f"{name} is waiting for approval", instance_id=instance_id))
            approval = await agent_approval.request_approval(
                instance_id, chat_id, session_key, name, tool_input, notify=notify_fn, **kwargs
            )
            outcome["approval"] = "human" if approval != "deny" else "denied"
            trace.active().approval(name, "deny" if approval == "deny" else "allow",
                                    "untrusted content in session" if fresh else "")
            if approval == "deny":
                outcome["status"] = "denied"
                return "Denied by user."
        elif verdict.decision == "allow":
            outcome["approval"] = "policy"
        elif unrestricted_relaxation:
            outcome["approval"] = "tier"

        # 5. Run it.
        session_token = toolspec.session_var.set(session_key)
        try:
            output = await agent_tools.execute_tool(
                name, tool_input, workspace=workspace, instance_id=instance_id, device_tier=device_tier
            )
        finally:
            toolspec.session_var.reset(session_token)
        if dangerous:
            try_checkpoint(workspace, name, tool_input)
        # 6. What comes back: credentials removed, size capped, and a session that just read
        #    untrusted content is marked as such.
        output = secrets_guard.redact(output)
        output = toolspec.limit_output(name, output, workspace)
        source = taint.note_result(session_key or "", name)
        if source:
            trace.active().note(f"session marked as having read untrusted content ({source})")
        await hooks.run_post_tool_use(name, tool_input, output, instance_id=instance_id)
        return output
    except agent_tools.ToolError as exc:
        outcome["status"], outcome["error"] = "failed", str(exc)[:200]
        await hooks.run_post_tool_failure(name, tool_input, str(exc), instance_id=instance_id)
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
