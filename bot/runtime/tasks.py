"""The task supervisor: every background coroutine of every unit instance runs here, in a per-instance group.

Stopping an instance cancels its whole group within a deadline. Tasks are held in the group (asyncio keeps only weak
references) and a failure is logged with its traceback instead of vanishing. No bare threads.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable, Optional

logger = logging.getLogger("bot.runtime.tasks")


class TaskSupervisor:
    def __init__(self) -> None:
        self._groups: dict[str, set[asyncio.Task]] = {}

    def spawn(self, group: str, coro_fn: Callable[[], Awaitable[Any]], name: Optional[str] = None) -> asyncio.Task:
        label = name or getattr(coro_fn, "__name__", "task")
        task = asyncio.get_running_loop().create_task(coro_fn(), name=f"{group}:{label}")
        self._groups.setdefault(group, set()).add(task)
        task.add_done_callback(lambda t, g=group: self._done(g, t))
        return task

    def _done(self, group: str, task: asyncio.Task) -> None:
        tasks = self._groups.get(group)
        if tasks is not None:
            tasks.discard(task)
            if not tasks:
                self._groups.pop(group, None)
        if not task.cancelled() and task.exception() is not None:
            logger.error("task %s failed", task.get_name(), exc_info=task.exception())

    def count(self, group: str) -> int:
        return len(self._groups.get(group, ()))

    def total(self) -> int:
        return sum(len(t) for t in self._groups.values())

    def groups(self) -> list[str]:
        return sorted(self._groups)

    async def cancel_group(self, group: str, deadline: float = 5.0) -> int:
        """Cancel every task of `group` and wait up to `deadline` seconds. Returns how many did not finish."""
        tasks = list(self._groups.get(group, ()))
        for task in tasks:
            task.cancel()
        if not tasks:
            return 0
        _, pending = await asyncio.wait(tasks, timeout=max(deadline, 0.0))
        for task in pending:
            logger.warning("task %s did not stop within %.1fs", task.get_name(), deadline)
        return len(pending)

    async def wait_group(self, group: str) -> None:
        """Wait for a group's tasks to finish on their own (used to wait out drains)."""
        while True:
            tasks = list(self._groups.get(group, ()))
            if not tasks:
                return
            await asyncio.wait(tasks)

    async def cancel_all(self, deadline: float = 5.0) -> int:
        left = 0
        for group in self.groups():
            left += await self.cancel_group(group, deadline)
        return left
