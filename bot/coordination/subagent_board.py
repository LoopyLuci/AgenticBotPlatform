"""Sub-agent children as tasks on the work board.

Every child of a spawn_subagent / dispatch_native_swarm_goal call is a task on board `abp/<parent session>`, keyed by a
hash of the parent turn, the agent definition and the prompt. So a retried or duplicated spawn finds the existing task:
a finished one hands back its stored result, one somebody is running is waited for, one whose runner vanished (lease
expired) is picked up and run - never twice at once. The child runs inside a WorkSession, which keeps its lease alive,
locks the files it edits and always ends the task. If the board is unreachable at any point the child just runs
unboarded: coordination is never a reason for a child to fail.
"""
from __future__ import annotations

import asyncio
import contextvars
import hashlib
import json
import logging
import time
from typing import Any, Awaitable, Optional

from bot import coordination
from bot.coordination.model import BoardError
from bot.coordination.session import WorkLost, WorkSession, acall

logger = logging.getLogger("bot.coordination.subagent_board")

# The parent's turn, when the caller knows it (otherwise the current agent trace run is used). Only a known turn lets a
# finished child's result answer a repeat of the same spawn; without one, only unfinished children are deduplicated.
turn_var: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar("coordination_turn", default=None)

POLL_S = 0.2


def current_turn() -> Optional[str]:
    explicit = turn_var.get()
    if explicit:
        return explicit
    try:
        from bot.agent_runtime import trace

        cur = trace.current()
        return cur.run_id if cur is not None else None
    except Exception:  # noqa: BLE001
        return None


def board_name_for(parent_instance_id: Optional[int]) -> str:
    try:
        from bot.agent_runtime import toolspec

        session = toolspec.current_session()
    except Exception:  # noqa: BLE001
        session = "default"
    if session == "default":
        session = f"instance-{parent_instance_id or 0}"
    return f"abp/{session}"[:200]


def child_key(turn: Optional[str], agent: str, goal: str, schema: Any = None) -> str:
    blob = json.dumps([turn or "", agent or "", goal, schema], sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:40]


class ChildBoard:
    """The board side of one run_batch call."""

    def __init__(self, board, board_name: str, turn: Optional[str], parent_agent: str):
        self.board = board
        self.board_name = board_name
        self.turn = turn
        self.parent_agent = parent_agent

    @classmethod
    async def open(cls, parent_instance_id: Optional[int]) -> Optional["ChildBoard"]:
        if not coordination.enabled():
            return None
        try:
            board = await coordination.aget_board()
        except Exception as exc:  # noqa: BLE001 - no board, no coordination; the batch still runs
            logger.warning("work board unavailable (%s); sub-agents run unboarded", exc)
            return None
        return cls(board, board_name_for(parent_instance_id), current_turn(), f"main:{parent_instance_id or 0}")

    async def register(self, dispatch_id: str, index: int, task: dict, goal: str) -> Optional["Ticket"]:
        agent_def = task.get("_agent")
        agent_name = getattr(agent_def, "name", "") or str(task.get("agent") or "")
        title = f"subagent{f' {agent_name}' if agent_name else ''}: {' '.join(goal.split())[:200]}"
        try:
            created, deduped = await acall(
                self.board, self.board.create_task, self.board_name, title, key=child_key(self.turn, agent_name, goal, task.get("output_schema")),
                detail=goal[:16000], dedupe_done=bool(self.turn), max_attempts=3, created_by=self.parent_agent)
        except Exception as exc:  # noqa: BLE001
            logger.warning("could not register sub-agent on the work board (%s); running it unboarded", exc)
            return None
        return Ticket(self, created, f"sub:{dispatch_id}:{index}", deduped)


class Ticket:
    """One child's place on the board."""

    def __init__(self, owner: ChildBoard, task: dict, agent: str, deduped: bool):
        self.owner = owner
        self.task = task
        self.agent = agent
        self.deduped = deduped

    def info(self) -> dict:
        return {"board": self.owner.board_name, "task": self.task["id"], "agent": self.agent, "deduped": self.deduped}

    async def run(self, inner: Awaitable[dict], *, session_id: int, index: int, goal: str, model: Optional[str]) -> dict:
        """Run `inner` (the child) under a WorkSession, or hand back what the board already knows."""
        from bot import db

        board = self.owner.board
        deadline = time.monotonic() + 330.0
        started = False
        try:
            while True:
                try:
                    cur = await acall(board, board.get_task, self.task["id"])
                except BoardError as exc:
                    logger.warning("work board refused a status read (%s); running the child unboarded", exc)
                    break
                state = cur["state"]
                if state == "done" and isinstance(cur.get("result"), dict):
                    outcome = dict(cur["result"], index=index, deduped=True)
                    db.finish_ephemeral_session(session_id, status=outcome.get("status", "ok"),
                                                result=str(outcome.get("result_excerpt") or ""))
                    inner.close()  # type: ignore[attr-defined]
                    return outcome
                if state in ("failed", "cancelled", "done"):
                    text = f"the same task already ended as {state} on the work board" + (f": {cur['note']}" if cur.get("note") else "")
                    db.finish_ephemeral_session(session_id, status="error", result=text)
                    inner.close()  # type: ignore[attr-defined]
                    return {"index": index, "goal": goal, "model": model, "status": "error", "result_excerpt": text[:500],
                            "deduped": True}
                if state == "open":
                    try:
                        work = await WorkSession.claim(board, self.task["id"], self.agent)
                    except BoardError as exc:
                        if exc.code not in ("TAKEN", "LOCKED", "DEPS", "VERSION"):
                            logger.warning("work board refused the claim (%s); running the child unboarded", exc)
                            break
                    else:
                        started = True
                        return await self._work(work, inner, session_id, index, goal, model)
                if time.monotonic() > deadline:
                    text = "timed out waiting for the agent that is already doing this task"
                    db.finish_ephemeral_session(session_id, status="error", result=text)
                    inner.close()  # type: ignore[attr-defined]
                    return {"index": index, "goal": goal, "model": model, "status": "error", "result_excerpt": text}
                await asyncio.sleep(POLL_S)
        except Exception:
            if started:
                raise
            logger.warning("work board trouble; running the child unboarded", exc_info=True)
        return await inner

    async def _work(self, work: WorkSession, inner: Awaitable[dict], session_id: int, index: int, goal: str,
                    model: Optional[str]) -> dict:
        from bot import db

        outcome: Optional[dict] = None
        try:
            async with work:
                outcome = await inner
                body = {k: v for k, v in outcome.items() if k != "index"}
                if outcome.get("status") == "ok":
                    work.done(body)
                else:
                    work.fail(str(outcome.get("result_excerpt") or "failed")[:300], body)
        except WorkLost as exc:
            text = f"lost the task to another agent ({exc}); this output was dropped"
            db.finish_ephemeral_session(session_id, status="error", result=text)
            return {"index": index, "goal": goal, "model": model, "status": "error", "result_excerpt": text}
        assert outcome is not None
        return outcome


def describe(ticket: Optional[Ticket]) -> Optional[dict]:
    """The board's view of one child, for list_subagents: task state, owner, attempts."""
    if ticket is None:
        return None
    info = ticket.info()
    try:
        task = ticket.owner.board.get_task(info["task"])
    except Exception:  # noqa: BLE001 - status is best effort
        return {"task": info["task"], "board": info["board"], "state": "unknown"}
    return {"task": task["id"], "board": task["board"], "state": task["state"], "owner": task["owner"],
            "attempts": task["attempts"], "deduped": ticket.deduped}
