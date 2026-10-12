"""Runs a swarm's steps through the work board (bot/coordination) instead of loose asyncio.gather calls.

Each step of a strategy is a task on board `abp/swarm-<swarm id>`: `deps` carry the ordering (a relay's next link, a
custom graph's depends_on, an aggregator after its planner), `needs` carry the capability (`instance:<id>` for the bot
that must run it, `run:<swarm run id>` so only this run's workers pick it up), and the key is a hash of the swarm, the
prompt and the step, so dispatching the same run twice joins the tasks already there instead of doing the work twice.
Workers pull with `next`, so no worker idles while a runnable step exists; each step runs inside a WorkSession (lease,
heartbeat, always ended) and its result is recorded on the task. A step whose lease runs out - its worker died - is
requeued by the board and taken by another worker; a stale worker's late output is refused by the fence.

Strategies keep their own logic (what the prompts are, how results merge); this module only runs `Step`s.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

from bot import coordination
from bot.coordination.model import TERMINAL, BoardError, Unavailable
from bot.coordination.session import WorkLost, WorkSession, acall

logger = logging.getLogger("bot.swarm.board_run")

POLL_S = 0.25
RESULT_TEXT_CAP = 40_000


@dataclass
class Step:
    id: str
    instance_id: int
    role: str
    label: str                                           # the step's name in the run's output (instance label)
    prompt: Callable[[dict[str, dict]], str]             # given the finished steps it depends on, by id
    deps: list[str] = field(default_factory=list)
    tolerate: bool = False                               # a failed step still unblocks its dependents
    extra: dict = field(default_factory=dict)            # copied into the step's output (e.g. the subtask)


class SwarmBoard:
    def __init__(self, swarm_row: dict, prompt: str, swarm_run_id: str,
                 call: Callable[[int, str], Awaitable[str]]):
        self.swarm_id = swarm_row.get("id") or 0
        self.prompt = prompt
        self.run_id = swarm_run_id
        self.call = call                                  # (instance_id, prompt) -> text; raises on failure
        self.board_name = f"abp/swarm-{self.swarm_id}"
        self.run_token = f"run:{swarm_run_id}"
        self.board: Any = None
        self.task_of: dict[str, str] = {}                 # step id -> task id
        self.step_of: dict[str, Step] = {}                # task id -> step
        self.results: dict[str, dict] = {}                # step id -> full output of a step this process ran
        self._changed = asyncio.Event()

    # --------------------------------------------------------------- public
    async def run(self, steps: list[Step]) -> dict[str, dict]:
        """Run `steps` (their deps may name steps from an earlier call) and return each finished step's output,
        by step id. Steps that never ran (a dependency failed) are missing."""
        if self.board is None:
            self.board = await coordination.aget_board()
        pending = await self._create(steps)
        workers = max(1, min(len(pending), coordination.settings()["swarm_workers"]))
        caps = sorted({self.run_token, *(f"instance:{s.instance_id}" for s in steps)})
        try:
            await asyncio.gather(*[self._worker(f"swarm:{self.run_id}:w{i}", caps, pending) for i in range(workers)])
        finally:
            await self._cancel_open(pending)
        return await self._collect(steps)

    # -------------------------------------------------------------- creating
    async def _create(self, steps: list[Step]) -> dict[str, Step]:
        pending: dict[str, Step] = {}
        todo = list(steps)
        while todo:                                       # deps first, whatever order the strategy listed them in
            progressed = False
            for step in list(todo):
                if all(d in self.task_of for d in step.deps):
                    todo.remove(step)
                    progressed = True
                    task = await self._create_one(step)
                    self.task_of[step.id] = task["id"]
                    self.step_of[task["id"]] = step
                    pending[task["id"]] = step
            if not progressed:
                raise ValueError(f"steps depend on unknown or circular steps: {[s.id for s in todo]}")
        return pending

    async def _create_one(self, step: Step) -> dict:
        key = hashlib.sha256(f"{self.swarm_id}\0{self.prompt}\0{step.id}".encode("utf-8")).hexdigest()[:40]
        kwargs = dict(
            key=key, detail=f"role {step.role}; bot {step.label}", needs=[f"instance:{step.instance_id}", self.run_token],
            deps=[self.task_of[d] for d in step.deps], max_attempts=2, created_by=f"swarm-run:{self.run_id}")
        title = f"{step.role}: {step.label}" + (f" - {step.extra['subtask'][:120]}" if step.extra.get("subtask") else "")
        try:
            task, _deduped = await acall(self.board, self.board.create_task, self.board_name, title, **kwargs)
        except Unavailable as exc:
            logger.warning("work board unreachable (%s); this swarm run uses the local board", exc)
            self.board = coordination.local_board()
            task, _deduped = await acall(self.board, self.board.create_task, self.board_name, title, **kwargs)
        return task

    # --------------------------------------------------------------- workers
    async def _worker(self, agent: str, caps: list[str], pending: dict[str, Step]) -> None:
        lease = coordination.settings()["lease_s"]
        while True:
            work = await WorkSession.next(self.board, self.board_name, agent, capabilities=caps, lease_s=lease)
            if work is None:
                if await self._idle(agent, caps, pending, lease):
                    return
                continue
            step = self.step_of.get(work.task_id)
            if step is None:                              # not one of ours (cannot happen with the run token)
                await acall(self.board, self.board.set_state, work.task_id, agent, work.fence, "released", note="unknown step")
                await asyncio.sleep(POLL_S)
                continue
            await self._do(work, step)

    async def _do(self, work: WorkSession, step: Step) -> None:
        try:
            async with work:
                deps_out = await self._dep_outputs(step)
                out: dict[str, Any] = {"step": step.label, "instance_id": step.instance_id, "role": step.role, **step.extra}
                try:
                    text = await self.call(step.instance_id, step.prompt(deps_out))
                    out.update(status="success", result=text)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 - one member's failure (any kind) is that step's, never the swarm's
                    out.update(status="failed", error=str(exc))
                self.results[step.id] = out
                record = dict(out)
                if isinstance(record.get("result"), str) and len(record["result"]) > RESULT_TEXT_CAP:
                    record["result"] = record["result"][:RESULT_TEXT_CAP]
                    record["truncated"] = True
                if out["status"] == "success" or step.tolerate:
                    work.done(record)
                else:
                    work.fail(str(out.get("error") or "failed")[:300], record)
        except WorkLost:
            self.results.pop(step.id, None)                # someone else owns it now; their output counts
        finally:
            ev, self._changed = self._changed, asyncio.Event()
            ev.set()

    async def _dep_outputs(self, step: Step) -> dict[str, dict]:
        out: dict[str, dict] = {}
        for dep in step.deps:
            if dep in self.results:
                out[dep] = self.results[dep]
                continue
            got = await self._stored(dep)
            if got is not None:
                out[dep] = got
        return out

    async def _stored(self, step_id: str) -> Optional[dict]:
        task = await acall(self.board, self.board.get_task, self.task_of[step_id])
        res = task.get("result")
        return res if isinstance(res, dict) else None

    async def _idle(self, agent: str, caps: list[str], pending: dict[str, Step], lease: float) -> bool:
        """Nothing runnable for this worker. True when its work is over; else it has waited (or adopted a step)."""
        tasks = {t["id"]: t for t in await acall(self.board, self.board.list_tasks, self.board_name, limit=5000)}
        mine = {tid: tasks[tid] for tid in pending if tid in tasks}
        if len(mine) < len(pending) or all(t["state"] in TERMINAL for t in mine.values()):
            return True
        active = [t for t in mine.values() if t["state"] in ("claimed", "running", "review")]
        open_ = [t for t in mine.values() if t["state"] == "open"]
        # A step another run created first (same key) needs that run's workers; if they are gone, take it over.
        for t in open_:
            if self.run_token not in t["needs"] and all(mine.get(d, {}).get("state") == "done" for d in t["deps"]):
                try:
                    work = await WorkSession.claim(self.board, t["id"], agent, lease_s=lease, capabilities=caps)
                except BoardError:
                    continue
                await self._do(work, pending[t["id"]])
                return False
        if not active:
            stuck = self._blocked_by_failure(mine)
            if stuck and len(stuck) == len(open_):
                for tid in stuck:
                    await acall(self.board, self.board.cancel, tid, agent=agent, note="a step it depends on failed")
                self._wake()
                return False
        try:
            await asyncio.wait_for(self._changed.wait(), POLL_S)
        except asyncio.TimeoutError:
            pass
        return False

    @staticmethod
    def _blocked_by_failure(mine: dict[str, dict]) -> set[str]:
        """Open steps that can never run because something they depend on failed or was cancelled."""
        dead = {tid for tid, t in mine.items() if t["state"] in ("failed", "cancelled")}
        stuck: set[str] = set()
        grew = True
        while grew:
            grew = False
            for tid, t in mine.items():
                if t["state"] == "open" and tid not in stuck and any(d in dead or d in stuck for d in t["deps"]):
                    stuck.add(tid)
                    grew = True
        return stuck

    def _wake(self) -> None:
        ev, self._changed = self._changed, asyncio.Event()
        ev.set()

    async def _cancel_open(self, pending: dict[str, Step]) -> None:
        for tid in pending:
            try:
                task = await acall(self.board, self.board.get_task, tid)
                if task["state"] not in TERMINAL and self.run_token in task["needs"]:
                    await acall(self.board, self.board.cancel, tid, agent=f"swarm-run:{self.run_id}", note="the run ended")
            except Exception:  # noqa: BLE001 - tidying up must not hide the run's own outcome
                logger.debug("could not tidy task %s", tid, exc_info=True)

    async def _collect(self, steps: list[Step]) -> dict[str, dict]:
        out: dict[str, dict] = {}
        for step in steps:
            if step.id in self.results:
                out[step.id] = self.results[step.id]
                continue
            task = await acall(self.board, self.board.get_task, self.task_of[step.id])
            if isinstance(task.get("result"), dict):
                out[step.id] = task["result"]
            elif task["state"] in ("failed", "done"):
                out[step.id] = {"step": step.label, "instance_id": step.instance_id, "role": step.role, **step.extra,
                                "status": "failed", "error": task.get("note") or "the step did not finish"}
        return out
