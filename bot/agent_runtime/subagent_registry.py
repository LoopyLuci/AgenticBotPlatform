"""Process-wide registry of live spawn_subagent children — the piece that
makes list_subagents/steer_subagent/stop_subagent possible at all.

bot/agent_runtime/subagents.py's original run_batch() handed anonymous
coroutines straight to asyncio.gather() — once started, nothing about a
child was reachable again until it finished on its own. This module gives
each child a real, addressable handle (its asyncio.Task, its own steer
queue, which ephemeral_sessions row it is) for as long as it's running,
keyed by a short dispatch_id so a later tool call in the SAME turn (or a
later turn, for background dispatches — see subagents.py's background
mode) can act on it.

Deliberately in-memory, not persisted: a handle is only ever useful while
its asyncio.Task is actually alive in this process. If BotServer restarts,
any in-flight children die with it (same as they always would have), and
their ephemeral_sessions rows simply stay at whatever status they last
reached — nothing here needs to survive a restart to be correct.

On the DENYLIST in bot/hotreload.py for exactly that reason from the
other direction: _dispatches holds live asyncio.Task/asyncio.Queue
objects for background dispatches that are specifically meant to survive
past the turn that created them — a hot-reload of this module would
silently orphan any in-flight dispatch (list_subagents/steer_subagent/
stop_subagent could no longer reach it, though the underlying task would
keep running to completion unreachable), the same failure shape that put
bot/agent_runtime/approval.py's pending-approval dict there.
"""

from __future__ import annotations

import asyncio
import secrets
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class ChildHandle:
    task: "asyncio.Task"
    steer_queue: "asyncio.Queue[str]"
    ephemeral_session_id: int
    parent_instance_id: Optional[int]
    goal: str


@dataclass
class Dispatch:
    dispatch_id: str
    parent_instance_id: Optional[int]
    children: dict[int, ChildHandle] = field(default_factory=dict)


# Dispatches are deliberately never removed once every child finishes
# (see describe()'s docstring — a caller should still be able to look
# results up afterward), so without a bound this would grow forever
# across a long-running process's lifetime. An OrderedDict + a simple
# oldest-first eviction cap is a cheap, adequate bound for this project's
# actual scale (a single trusted operator's own bot instances, not a
# multi-tenant service) — a fuller retention.py integration is unneeded
# complexity for that scale.
_MAX_DISPATCHES = 200
_dispatches: "OrderedDict[str, Dispatch]" = OrderedDict()


def new_dispatch(parent_instance_id: Optional[int]) -> Dispatch:
    dispatch_id = secrets.token_hex(4)
    dispatch = Dispatch(dispatch_id=dispatch_id, parent_instance_id=parent_instance_id)
    _dispatches[dispatch_id] = dispatch
    while len(_dispatches) > _MAX_DISPATCHES:
        _dispatches.popitem(last=False)
    return dispatch


def register_child(dispatch: Dispatch, index: int, handle: ChildHandle) -> None:
    dispatch.children[index] = handle


def get_dispatch(dispatch_id: str) -> Optional[Dispatch]:
    return _dispatches.get(dispatch_id)


def list_dispatches(parent_instance_id: Optional[int]) -> list[Dispatch]:
    return [d for d in _dispatches.values() if d.parent_instance_id == parent_instance_id]


def _child_or_raise(dispatch_id: str, child_index: int, parent_instance_id: Optional[int]) -> ChildHandle:
    from bot.agent_runtime.tools import ToolError

    dispatch = _dispatches.get(dispatch_id)
    if dispatch is None:
        raise ToolError(f"no such dispatch {dispatch_id!r} — it may have already finished")
    if dispatch.parent_instance_id != parent_instance_id:
        raise ToolError(f"dispatch {dispatch_id!r} does not belong to this instance")
    handle = dispatch.children.get(child_index)
    if handle is None:
        raise ToolError(f"dispatch {dispatch_id!r} has no child at index {child_index}")
    return handle


def _describe_dispatch(dispatch: Dispatch) -> dict:
    from bot import db

    children = []
    for index, handle in sorted(dispatch.children.items()):
        row = db.get_ephemeral_session(handle.ephemeral_session_id)
        children.append({
            "index": index,
            "goal": handle.goal,
            "status": row["status"] if row is not None else ("running" if not handle.task.done() else "unknown"),
            "result_excerpt": (row["result"] or "")[:500] if row is not None and row["result"] else None,
        })
    return {"dispatch_id": dispatch.dispatch_id, "children": children}


def describe(dispatch_id: str, *, parent_instance_id: Optional[int]) -> dict:
    """Live status for one dispatch — used by list_subagents(dispatch_id).
    Reads each child's current status from its ephemeral_sessions row (the
    single source of truth for "running"/"ok"/"error"/"stopped"), not just
    whether its asyncio.Task object has completed, so this is accurate
    even for a child whose task finished a moment ago but whose result
    hasn't been read yet."""
    from bot.agent_runtime.tools import ToolError

    dispatch = _dispatches.get(dispatch_id)
    if dispatch is None:
        raise ToolError(f"no such dispatch {dispatch_id!r} — it may have already finished and been cleared")
    if dispatch.parent_instance_id != parent_instance_id:
        raise ToolError(f"dispatch {dispatch_id!r} does not belong to this instance")
    return _describe_dispatch(dispatch)


def describe_all(parent_instance_id: Optional[int]) -> list[dict]:
    """Every dispatch this instance currently has registered (i.e. still
    has at least one child that hasn't finished, or hasn't been cleaned
    up yet) — used by list_subagents() with no dispatch_id. A dispatch
    whose every child already finished and was cleaned up (the
    non-background path, right after its own gather() returns) won't
    appear here — its results were already returned directly from that
    spawn_subagent call."""
    return [_describe_dispatch(d) for d in list_dispatches(parent_instance_id)]


def steer(dispatch_id: str, child_index: int, message: str, *, parent_instance_id: Optional[int]) -> None:
    handle = _child_or_raise(dispatch_id, child_index, parent_instance_id)
    if handle.task.done():
        from bot.agent_runtime.tools import ToolError

        raise ToolError(f"child {child_index} of dispatch {dispatch_id!r} has already finished — nothing to steer")
    handle.steer_queue.put_nowait(message)


def stop(dispatch_id: str, child_index: int, *, parent_instance_id: Optional[int]) -> None:
    from bot import db

    handle = _child_or_raise(dispatch_id, child_index, parent_instance_id)
    if handle.task.done():
        from bot.agent_runtime.tools import ToolError

        raise ToolError(f"child {child_index} of dispatch {dispatch_id!r} has already finished")
    handle.task.cancel()
    db.finish_ephemeral_session(handle.ephemeral_session_id, status="stopped", result="stopped by steer_subagent/stop_subagent tool call")
