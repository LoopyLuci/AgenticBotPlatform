"""Fire-and-forget background tasks that can't vanish.

asyncio keeps only a weak reference to a task, so a bare
`asyncio.create_task(coro)` whose result nobody stores can be
garbage-collected mid-run, and an exception it raises is never logged
("Task exception was never retrieved" at best, at interpreter exit). Every
fire-and-forget call site goes through spawn() instead: the task is held in
a module-level set until it finishes, and a failure is logged with its
traceback and counted, so the self-preservation layer (bot/sentinel) sees it.

drain() lets shutdown give in-flight notifications a moment to finish.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import logging
from typing import Any, Awaitable, Callable, Optional

logger = logging.getLogger("bot.tasks")

_tasks: set[asyncio.Future] = set()
_failures = 0
_main_loop: Optional[asyncio.AbstractEventLoop] = None


def _done(task: asyncio.Future) -> None:
    global _failures
    _tasks.discard(task)
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        _failures += 1
        name = task.get_name() if isinstance(task, asyncio.Task) else "future"
        logger.error("background task %s failed", name, exc_info=(type(exc), exc, exc.__traceback__))


def spawn(aw: Awaitable[Any], *, name: Optional[str] = None) -> "asyncio.Future | concurrent.futures.Future":
    """Schedules `aw` on the running loop, keeps it alive until it finishes,
    and logs (never swallows) its failure. Must be called from inside a
    running event loop — or from a worker thread while the app's main loop is
    running, in which case the work is handed to that loop."""
    global _main_loop
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        loop = _main_loop
        if loop is None or loop.is_closed() or not loop.is_running():
            if asyncio.iscoroutine(aw):
                aw.close()
            raise RuntimeError("bg.spawn() called with no running event loop") from None
        return asyncio.run_coroutine_threadsafe(_adopt(aw, name), loop)  # type: ignore[return-value]
    task = asyncio.ensure_future(aw)
    if _main_loop is None or _main_loop.is_closed():
        _main_loop = task.get_loop()
    if name and isinstance(task, asyncio.Task):
        task.set_name(name)
    _tasks.add(task)
    task.add_done_callback(_done)
    return task


async def _adopt(aw: Awaitable[Any], name: Optional[str]) -> Any:
    """Runs on the main loop for a spawn() that came from a worker thread,
    so the work gets the same keep-alive and failure logging."""
    return await spawn(aw, name=name)


def bind_loop(loop: asyncio.AbstractEventLoop) -> None:
    """Records the app's main loop so spawn_soon() can reach it from a worker
    thread (bot.main calls this at startup; spawn() also binds lazily)."""
    global _main_loop
    _main_loop = loop


def spawn_soon(factory: Callable[[], Awaitable[Any]], *, name: Optional[str] = None) -> bool:
    """spawn() from ANY thread: on the loop thread it spawns right away; from
    a worker thread (a sync FastAPI handler, asyncio.to_thread) it hands the
    work to the main loop. `factory` builds the awaitable on the loop thread,
    so nothing is created that could be dropped un-awaited. Returns False
    only when no live loop exists (the caller's work is then skipped)."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        loop = _main_loop
        if loop is None or loop.is_closed() or not loop.is_running():
            return False
        loop.call_soon_threadsafe(lambda: spawn(factory(), name=name))
        return True
    spawn(factory(), name=name)
    return True


def pending() -> int:
    return len(_tasks)


def failures() -> int:
    return _failures


async def drain(timeout: float = 5.0) -> int:
    """Waits up to `timeout` seconds for outstanding background tasks, then
    cancels the rest. Returns how many had to be cancelled."""
    live = [t for t in _tasks if not t.done()]
    if not live:
        return 0
    _, still = await asyncio.wait(live, timeout=timeout)
    for t in still:
        t.cancel()
    return len(still)
