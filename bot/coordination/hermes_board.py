"""A Hermes-external swarm dispatch as a task on the work board.

`dispatch_swarm_goal` hands a goal to a Hermes gateway instance and waits, possibly for minutes, while Hermes's own agent
fans it out to children ABP cannot see. ABP's part is that one wait, so that is the task: board `abp/hermes-<instance id>`,
key = hash of goal + target (instance, provider, model) + params (max children). The dispatcher holds a WorkSession while it
waits (lease + heartbeat), and the answer (or the failure and its note) is recorded on the task. Consequences:

- a second dispatch of the same thing while the first is unfinished does not call Hermes again; it waits for the first and
  hands back its stored answer (`deduped`); a finished one is reused for `REUSE_DONE_S`, after that it is dispatched anew;
- if ABP dies mid-run the task keeps a lease that runs out: it goes back to `open` (the next dispatch of the same goal picks
  it up and runs it - the retry) or, after `max_attempts` expiries, to `failed` with "lease expired N times" - visible on the
  Coordination screen, never silently lost;
- a failed dispatch is a failed task with the error as its note; the next dispatch is a new task.

If the board is off or unreachable the call simply runs unboarded: coordination is never a reason for a dispatch to fail.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

from bot import coordination
from bot.coordination.model import BoardError
from bot.coordination.session import WorkLost, WorkSession, acall

logger = logging.getLogger("bot.coordination.hermes_board")

POLL_S = 0.5
WAIT_S = 3600.0          # how long a second dispatch waits for the first one's answer
REUSE_DONE_S = 600.0     # a finished identical dispatch younger than this answers a repeat without calling Hermes
RESULT_TEXT_CAP = 40_000


class DispatchBusy(Exception):
    """An identical dispatch is still running somewhere else and did not finish within the wait."""


class DispatchFailed(Exception):
    """The identical dispatch ended failed/cancelled on the board (note says why)."""


@dataclass
class Outcome:
    result: Any                                   # what `call` returned (or the stored copy)
    deduped: bool = False                         # True: Hermes was not called by this dispatch
    board: str = ""
    task: str = ""
    agent: str = ""
    info: dict = field(default_factory=dict)


def board_name(instance_id: int) -> str:
    return f"abp/hermes-{instance_id}"


def dispatch_key(instance_id: int, goal: str, worker_provider: Optional[str], worker_model: Optional[str],
                 max_children: Any) -> str:
    blob = json.dumps([instance_id, goal, worker_provider or "", worker_model or "", max_children], sort_keys=True,
                      default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:40]


def _cap(result: Any) -> Any:
    if isinstance(result, dict) and isinstance(result.get("text"), str) and len(result["text"]) > RESULT_TEXT_CAP:
        return dict(result, text=result["text"][:RESULT_TEXT_CAP], truncated=True)
    return result


async def run(instance_id: int, goal: str, worker_provider: Optional[str], worker_model: Optional[str],
              max_children: Any, call: Callable[[], Awaitable[Any]], *, lease_s: Optional[float] = None,
              wait_s: float = WAIT_S, poll_s: float = POLL_S, dispatch_id: str = "") -> Outcome:
    """Run `call` (the Hermes round trip; returns a JSON-able result, raises on failure) as a board task, or hand back the
    stored answer of an identical dispatch. Raises what `call` raises; DispatchBusy / DispatchFailed for the other cases."""
    if not coordination.enabled():
        return Outcome(await call())
    try:
        board = await coordination.aget_board()
        name = board_name(instance_id)
        key = dispatch_key(instance_id, goal, worker_provider, worker_model, max_children)
        title = f"hermes dispatch to instance {instance_id}: {' '.join(goal.split())[:200]}"
        detail = json.dumps({"goal": goal[:12000], "worker_provider": worker_provider, "worker_model": worker_model,
                             "max_children": max_children}, default=str)
        task, deduped = await acall(board, board.create_task, name, title, key=key, detail=detail[:16000],
                                    dedupe_done=True, max_attempts=2, created_by="dispatch_swarm_goal")
        if deduped and task["state"] == "done" and _age_s(task) > REUSE_DONE_S:
            task, deduped = await acall(board, board.create_task, name, title, key=key, detail=detail[:16000],
                                        max_attempts=2, created_by="dispatch_swarm_goal")
    except Exception as exc:  # noqa: BLE001 - no board, no coordination; the dispatch still runs
        logger.warning("work board unavailable (%s); the Hermes dispatch runs unboarded", exc)
        return Outcome(await call())

    agent = f"hermes-dispatch:{dispatch_id or task['id'][:8]}"
    lease = lease_s or coordination.settings()["lease_s"]
    deadline = time.monotonic() + wait_s
    while True:
        try:
            cur = await acall(board, board.get_task, task["id"])
        except BoardError as exc:
            logger.warning("work board refused a status read (%s); the Hermes dispatch runs unboarded", exc)
            return Outcome(await call())
        state = cur["state"]
        if state == "done":
            return Outcome(cur.get("result"), deduped=True, board=name, task=task["id"], agent=agent)
        if state in ("failed", "cancelled"):
            raise DispatchFailed(f"the same dispatch ended {state} on the work board"
                                 + (f": {cur['note']}" if cur.get("note") else ""))
        if state == "open":
            try:
                work = await WorkSession.claim(board, task["id"], agent, lease_s=lease)
            except BoardError as exc:
                if exc.code not in ("TAKEN", "LOCKED", "DEPS", "VERSION"):
                    logger.warning("work board refused the claim (%s); the Hermes dispatch runs unboarded", exc)
                    return Outcome(await call())
            else:
                return await _work(work, call, Outcome(None, board=name, task=task["id"], agent=agent))
        if time.monotonic() > deadline:
            raise DispatchBusy("an identical dispatch is already running and did not finish in time")
        await asyncio.sleep(poll_s)


async def _work(work: WorkSession, call: Callable[[], Awaitable[Any]], out: Outcome) -> Outcome:
    error: Optional[BaseException] = None
    try:
        async with work:
            await work.progress("running")
            try:
                out.result = await call()
                work.done(_cap(out.result))
            except asyncio.CancelledError:
                raise                                     # released by the session: the next dispatch retries it
            except Exception as exc:  # noqa: BLE001 - recorded on the task, then re-raised to the caller below
                error = exc
                work.fail(f"{type(exc).__name__}: {exc}"[:300])
    except WorkLost as exc:
        raise DispatchBusy(f"the dispatch lost its place on the work board ({exc}); another dispatch owns it now") from None
    if error is not None:
        raise error
    return out


def _age_s(task: dict) -> float:
    stamp = task.get("updated_at") or task.get("created_at") or 0
    return max(0.0, time.time() - stamp / 1000.0)


__all__ = ["DispatchBusy", "DispatchFailed", "Outcome", "board_name", "dispatch_key", "run"]
