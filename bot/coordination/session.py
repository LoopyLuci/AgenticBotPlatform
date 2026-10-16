"""WorkSession: one claimed task's lifetime, as an async context manager.

    async with await WorkSession.claim(board, task_id, "agent-1") as work:
        ... do the work; await work.acquire(["path:ws:src/a.py"]) before touching something shared ...
        work.result = {...}          # recorded on the task when the block ends well

It keeps the lease alive (a heartbeat every lease/3, the task's locks with it), tells the board who is busy, and always
ends the task: done when the block finishes (or `work.done(result)`), failed after `work.fail(note)`, released when the
block raises, is cancelled or the process shuts down, so someone else can take it. If the board says the fence is stale
or the task is no longer ours (the lease expired and another agent took it), the work is cancelled at once, nothing is
written back, and the block ends with WorkLost: whatever it produced must be dropped.
"""
from __future__ import annotations

import asyncio
import contextvars
import logging
from typing import Any, Callable, Optional

from bot.coordination.base import Board
from bot.coordination.model import BoardError, Unavailable, clamp_lease

logger = logging.getLogger("bot.coordination.session")

current_work: contextvars.ContextVar[Optional["WorkSession"]] = contextvars.ContextVar("coordination_work", default=None)


class WorkLost(Exception):
    """The task is no longer ours (stale fence / not the owner): its output must be dropped."""


async def acall(board: Board, fn: Callable, *args: Any, **kwargs: Any) -> Any:
    """Call a Board method without blocking the event loop (network boards run in a thread)."""
    if board.blocking:
        return await asyncio.to_thread(lambda: fn(*args, **kwargs))
    return fn(*args, **kwargs)


class WorkSession:
    def __init__(self, board: Board, claim: dict, agent: str, *, lease_s: Optional[float] = None,
                 capabilities: Optional[list[str]] = None):
        self.board = board
        self.agent = agent
        self.task: dict = claim["task"]
        self.task_id: str = self.task["id"]
        self.fence: int = claim["fence"]
        self.lease_s = float(lease_s) if lease_s else 120.0
        self.capabilities = list(capabilities or [])
        self.lost = False
        self.lost_reason = ""
        self.result: Any = None
        self.lock_fences: set[int] = set()
        self._final: Optional[tuple[str, Any, str]] = None
        self._owner: Optional[asyncio.Task] = None
        self._self_cancelled = False
        self._beat_task: Optional[asyncio.Task] = None
        self._token: Optional[contextvars.Token] = None

    # ---------------------------------------------------------------- start
    @classmethod
    async def claim(cls, board: Board, task_id: str, agent: str, *, lease_s: Optional[float] = None,
                    capabilities: Optional[list[str]] = None) -> "WorkSession":
        """Claim a specific task; BoardError TAKEN / LOCKED / DEPS / VERSION when it cannot be had."""
        lease = lease_s or 120.0
        got = await acall(board, board.claim, task_id, agent, lease_s=lease)
        return cls(board, got, agent, lease_s=lease, capabilities=capabilities)

    @classmethod
    async def next(cls, board: Board, board_name: str, agent: str, *, capabilities: Optional[list[str]] = None,
                   lease_s: Optional[float] = None) -> Optional["WorkSession"]:
        """Claim the best runnable task for this agent, or None when nothing is runnable now."""
        lease = lease_s or 120.0
        got = await acall(board, board.next, board_name, agent, capabilities=capabilities, lease_s=lease)
        return None if got is None else cls(board, got, agent, lease_s=lease, capabilities=capabilities)

    # ----------------------------------------------------------- the block
    async def __aenter__(self) -> "WorkSession":
        self._owner = asyncio.current_task()
        self._token = current_work.set(self)
        await self._safely(self.board.presence, self.agent, "busy", task=self.task_id, capabilities=self.capabilities or None)
        self._beat_task = asyncio.create_task(self._beat(), name=f"work-beat-{self.task_id[:8]}")
        return self

    async def __aexit__(self, et, ev, tb) -> bool:
        if self._beat_task is not None:
            self._beat_task.cancel()
            try:
                await self._beat_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001 - the heartbeat's own end is not the block's
                pass
        if self._token is not None:
            try:
                current_work.reset(self._token)
            except ValueError:
                pass
        if self.lost:
            if et is not None and issubclass(et, asyncio.CancelledError) and self._self_cancelled and self._owner:
                self._owner.uncancel()
            await self._safely(self.board.presence, self.agent, "idle")
            raise WorkLost(self.lost_reason or "the task is no longer ours") from None
        state, result, note = self._final or self._default_final(et, ev)
        try:
            # Cancelled from outside: finish the write even so, or the task stays claimed until its lease runs out.
            await asyncio.shield(acall(self.board, self.board.set_state, self.task_id, self.agent, self.fence, state,
                                       result=result, note=note))
        except asyncio.CancelledError:
            pass
        except BoardError as exc:
            if exc.code in ("STALE_FENCE", "NOT_OWNER"):
                logger.warning("work %s: could not end as %s (%s)", self.task_id, state, exc)
                self.lost, self.lost_reason = True, exc.message
                if et is None:
                    raise WorkLost(exc.message) from None
            else:
                logger.warning("work %s: could not end as %s (%s); the lease will requeue it", self.task_id, state, exc)
        await self._safely(self.board.presence, self.agent, "idle")
        return False

    def _default_final(self, et, ev) -> tuple[str, Any, str]:
        if et is None:
            return "done", self.result, ""
        if issubclass(et, asyncio.CancelledError):
            return "released", None, "cancelled"
        if issubclass(et, Exception):
            return "released", None, f"{et.__name__}: {ev}"[:500]
        return "released", None, f"interrupted ({et.__name__})"

    # ------------------------------------------------------ ending it early
    def done(self, result: Any = None) -> None:
        self._final = ("done", self.result if result is None else result, "")

    def fail(self, note: str, result: Any = None) -> None:
        self._final = ("failed", result, note)

    def block(self, note: str, result: Any = None) -> None:
        """End the task as `blocked`: it needs a decision (its context is the result); it is not retried or requeued."""
        self._final = ("blocked", result, note)

    def release(self, note: str = "") -> None:
        self._final = ("released", None, note)

    async def progress(self, state: str = "running", note: str = "") -> None:
        """Tell the board the task is `running` (or `review`)."""
        await self._safely(self.board.set_state, self.task_id, self.agent, self.fence, state, note=note)

    # ----------------------------------------------------------------- locks
    async def acquire(self, resources: list, *, lease_s: Optional[float] = None) -> dict:
        """Take shared things for this task (all or nothing); LOCKED carries the holders. Kept alive and freed with the task."""
        if self.lost:
            raise WorkLost(self.lost_reason)
        got = await acall(self.board, self.board.acquire, self.agent, resources, task=self.task_id,
                          lease_s=lease_s or self.lease_s)
        self.lock_fences.add(got["fence"])
        return got

    # --------------------------------------------------------------- liveness
    def _lose(self, reason: str) -> None:
        if self.lost:
            return
        self.lost, self.lost_reason = True, reason
        logger.warning("work %s lost (%s): cancelling it", self.task_id, reason)
        owner = self._owner
        if owner is not None and not owner.done() and owner is not asyncio.current_task():
            self._self_cancelled = True
            owner.cancel()

    async def _beat(self) -> None:
        interval = max(0.02, self.lease_s / 3.0)
        while True:
            await asyncio.sleep(interval)
            try:
                await acall(self.board, self.board.heartbeat, self.task_id, self.agent, self.fence, lease_s=self.lease_s)
                for fence in list(self.lock_fences):
                    try:
                        await acall(self.board, self.board.lock_heartbeat, self.agent, fence, lease_s=self.lease_s)
                    except BoardError as exc:
                        if isinstance(exc, Unavailable):
                            raise
                        self.lock_fences.discard(fence)         # released or expired; the file guard takes it again
            except Unavailable as exc:
                logger.warning("work %s: heartbeat could not reach the board (%s); retrying", self.task_id, exc)
            except BoardError as exc:
                if exc.code in ("STALE_FENCE", "NOT_OWNER"):
                    self._lose(exc.message)
                    return
                logger.warning("work %s: heartbeat refused: %s", self.task_id, exc)

    async def _safely(self, fn: Callable, *args: Any, **kwargs: Any) -> None:
        try:
            await acall(self.board, fn, *args, **kwargs)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - bookkeeping must not break the work
            logger.debug("work %s: %s failed: %s", self.task_id, getattr(fn, "__name__", fn), exc)


__all__ = ["WorkLost", "WorkSession", "acall", "clamp_lease", "current_work"]
