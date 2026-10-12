"""Edit locks for the native agent's file tools: two agents never write the same file at once.

`guard()` wraps write_file / edit_file / multi_edit / apply_patch. Inside a WorkSession (a sub-agent child, a swarm
worker) the file is locked exclusively for that task - `path:<workspace id>:<relative path>`, kept alive by the task's
heartbeat, freed when the task ends - and a second agent that tries the same file gets a ToolError naming the holder
(agent and task title) instead of a write. Reads are never locked.

Outside a session (the main agent) nothing is locked while it is alone, so single-agent use costs nothing; when it has
live sub-agents it takes the lock for the duration of the one write only. Without a reachable board, edits proceed
(the board must never be the reason an edit fails).
"""
from __future__ import annotations

import hashlib
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Optional

from bot import coordination
from bot.coordination.model import BoardError, Unavailable
from bot.coordination.session import acall, current_work

logger = logging.getLogger("bot.coordination.fileguard")

EDIT_TOOLS = frozenset({"write_file", "edit_file", "multi_edit", "apply_patch"})


def workspace_id(workspace: Path) -> str:
    text = str(Path(workspace).resolve())
    if os.name == "nt":
        text = text.lower()
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:10]


def resource_for(workspace: Path, rel: str) -> str:
    rel = rel.replace("\\", "/").strip("/")
    if os.name == "nt":
        rel = rel.lower()
    return f"path:{workspace_id(workspace)}:{rel}"


def edit_targets(name: str, tool_input: Any, workspace: Path) -> list[str]:
    """Workspace-relative paths a call would write. Anything unreadable or outside the workspace is left to the tool's own error."""
    if not isinstance(tool_input, dict):
        return []
    rels: list[str] = []
    if name == "apply_patch":
        try:
            from bot.agent_runtime import coding_tools

            for fp in coding_tools.parse_patch(tool_input.get("patch") or ""):
                rels += [n for n in (fp.old_name, fp.new_name) if n]
        except Exception:  # noqa: BLE001 - a bad patch is the tool's error to report
            return []
    elif tool_input.get("path"):
        rels.append(str(tool_input["path"]))
    out: list[str] = []
    root = Path(workspace).resolve()
    for rel in rels:
        try:
            from bot.agent_runtime import coding_tools

            path = coding_tools.safe_path(Path(workspace), rel)
            out.append(path.resolve().relative_to(root).as_posix())
        except Exception:  # noqa: BLE001
            continue
    return list(dict.fromkeys(out))


def _others_active() -> bool:
    try:
        from bot.agent_runtime import subagent_registry

        return subagent_registry.count_live_children() > 0
    except Exception:  # noqa: BLE001
        return False


def _refusal(tool: str, exc: BoardError, rels: list[str]) -> str:
    h = exc.holders[0] if exc.holders else {}
    what = h.get("title") or h.get("task") or "its current task"
    who = h.get("agent") or "another agent"
    return (f"{tool} did not write: {', '.join(rels)} is being edited by {who} (task: {what}). Two agents must not "
            "edit the same file at once, so nothing was changed. Work on something else and come back after it "
            "finishes, or leave this file to that agent.")


@asynccontextmanager
async def guard(name: str, tool_input: Any, *, workspace: Path, instance_id: Optional[int] = None):
    from bot.agent_runtime.errors import ToolError

    work = current_work.get()
    if name not in EDIT_TOOLS or (work is None and not _others_active()) or not coordination.enabled():
        yield
        return
    rels = edit_targets(name, tool_input, workspace)
    if not rels:
        yield
        return
    resources = [resource_for(workspace, r) for r in rels]
    lease = coordination.settings()["file_lock_lease_s"]
    if work is not None:
        try:
            await work.acquire(resources, lease_s=lease)
        except BoardError as exc:
            if exc.code == "LOCKED":
                raise ToolError(_refusal(name, exc, rels)) from exc
            if not isinstance(exc, Unavailable):
                raise
            logger.warning("edit locks unavailable (%s); writing without them", exc)
        yield
        return
    agent = f"main:{instance_id or 0}"
    board = None
    fence = None
    try:
        board = await coordination.aget_board()
        got = await acall(board, board.acquire, agent, resources, lease_s=lease)
        fence = got["fence"]
    except BoardError as exc:
        if exc.code == "LOCKED":
            raise ToolError(_refusal(name, exc, rels)) from exc
        logger.warning("edit locks unavailable (%s); writing without them", exc)
    try:
        yield
    finally:
        if fence is not None and board is not None:
            try:
                await acall(board, board.release_locks, agent, fence)
            except Exception as exc:  # noqa: BLE001 - the lease frees it anyway
                logger.debug("could not release %s: %s", resources, exc)
