"""Per-chat turn control: the /stop, /queue, /steer, /pause, /background,
/agents layer. This is backend-agnostic infrastructure — every /ask, for
every backend, runs as a real asyncio.Task this module tracks, so "stop
the thing that's running for this chat" is genuinely possible everywhere.

What differs per backend is what "stop" and "steer" actually reach: for
`api`, BotServer owns the tool loop itself (see bot/backends/api_backend.py
and tools.py/approval.py), so steering can inject a message at the next
tool-call boundary and a cancelled task interrupts mid-loop. For
`cli`/`hermes_cli`, cancelling this task also kills the underlying
subprocess (see those backends' CancelledError handling) — a real stop,
just at whole-request granularity, not a tool-call boundary, because
there is no tool-call boundary BotServer can see inside those programs.
For `ui`/`hermes_gateway`, cancelling only stops *us* from waiting on the
result; the external app keeps doing whatever it was already doing — a
real, documented limit of automating another program's UI/API rather than
running the loop ourselves, not something this module can paper over.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Optional

logger = logging.getLogger("bot.agent_runtime.engine")

Key = tuple[int, Any, Any]  # (instance_id, chat_id, thread_id) — thread_id is None outside a Telegram forum topic


@dataclass
class _Queued:
    prompt: str
    action_type: str
    user_id: Any
    backend_override: Optional[str]
    context: Optional[dict]
    on_result: Optional[Any] = None  # Callable[[str, Any], None] — see run_turn's on_result param


@dataclass
class _ChatState:
    task: Optional[asyncio.Task] = None
    queue: list[_Queued] = field(default_factory=list)
    steer_queue: "asyncio.Queue[str]" = field(default_factory=asyncio.Queue)
    background: bool = False


_chats: dict[Key, _ChatState] = {}
_paused_instances: set[int] = set()


def _state(key: Key) -> _ChatState:
    return _chats.setdefault(key, _ChatState())


def is_running(instance_id: int, chat_id: Any, thread_id: Any = None) -> bool:
    st = _chats.get((instance_id, chat_id, thread_id))
    return bool(st and st.task and not st.task.done())


def is_paused(instance_id: int) -> bool:
    return instance_id in _paused_instances


def pause(instance_id: int) -> None:
    _paused_instances.add(instance_id)


def resume(instance_id: int) -> None:
    _paused_instances.discard(instance_id)


def get_steer_queue(instance_id: int, chat_id: Any, thread_id: Any = None) -> "asyncio.Queue[str]":
    return _state((instance_id, chat_id, thread_id)).steer_queue


def push_steer(instance_id: int, chat_id: Any, text: str, thread_id: Any = None) -> bool:
    if not is_running(instance_id, chat_id, thread_id):
        return False
    get_steer_queue(instance_id, chat_id, thread_id).put_nowait(text)
    return True


async def stop(instance_id: int, chat_id: Any, thread_id: Any = None) -> bool:
    key = (instance_id, chat_id, thread_id)
    st = _chats.get(key)
    if st is None or st.task is None or st.task.done():
        return False
    st.task.cancel()
    return True


def list_running(instance_id: Optional[int] = None) -> list[dict]:
    out = []
    for (iid, cid, tid), st in _chats.items():
        if instance_id is not None and iid != instance_id:
            continue
        if st.task and not st.task.done():
            out.append({"instance_id": iid, "chat_id": cid, "thread_id": tid, "background": st.background})
    return out


async def run_turn(
    prompt: str,
    *,
    action_type: str,
    user_id: Any,
    instance_id: int,
    chat_id: Any,
    thread_id: Any = None,
    backend_override: Optional[str] = None,
    context: Optional[dict] = None,
    background: bool = False,
    on_result=None,
) -> tuple[str, Any]:
    """Returns (outcome, result): outcome is "ran" (result is a
    BackendResult, only when background=False — the normal synchronous
    /ask path), "queued", "paused", or "background".

    `on_result(outcome, result_or_exc)` — outcome one of "ran"/"stopped"/
    "error" — is called once the turn actually finishes, whenever that is:
    immediately for a background turn, or later for one that had to sit in
    the queue first. That's the only way a caller finds out about a queued
    prompt's outcome, since by the time it runs the original request/reply
    cycle that queued it is long over."""
    from bot.router import router  # deferred: avoids a top-level backends -> engine -> router import cycle

    key = (instance_id, chat_id, thread_id)
    st = _state(key)

    if is_paused(instance_id):
        st.queue.append(_Queued(prompt, action_type, user_id, backend_override, context, on_result))
        return "paused", None

    if st.task is not None and not st.task.done():
        st.queue.append(_Queued(prompt, action_type, user_id, backend_override, context, on_result))
        return "queued", None

    ctx = dict(context or {})
    ctx["steer_queue"] = st.steer_queue

    async def _run() -> Any:
        # For a background-run turn, on_result is the ONLY way the outcome
        # reaches anyone (nobody awaits `task` below), so a raised
        # exception must NOT propagate past this function for that case —
        # an unawaited task that raises is a silent "Task exception was
        # never retrieved" asyncio warning, not a real error report to
        # anyone. A foreground turn re-raises instead, since its caller
        # (below) is synchronously awaiting `task` and needs the real
        # exception to build its own "error" outcome.
        try:
            result = await router.ask(
                prompt,
                action_type=action_type,
                user_id=user_id,
                backend_override=backend_override,
                context=ctx,
                instance_id=instance_id,
                chat_id=chat_id,
                thread_id=thread_id,
            )
            if background and on_result is not None:
                await on_result("ran", result)
            return result
        except asyncio.CancelledError:
            if background:
                if on_result is not None:
                    await on_result("stopped", None)
                return None
            raise
        except Exception as exc:  # noqa: BLE001 — the one place a backend/tool-loop error surfaces
            if background:
                if on_result is not None:
                    await on_result("error", exc)
                return None
            raise
        finally:
            st.task = None
            st.background = False
            asyncio.ensure_future(_drain(key))

    task = asyncio.ensure_future(_run())
    st.task = task
    st.background = background

    if background:
        return "background", None

    try:
        result = await task
    except asyncio.CancelledError:
        return "stopped", None
    except Exception as exc:  # noqa: BLE001 — reported to the caller as an "error" outcome, not raised further
        return "error", exc
    return "ran", result


async def _drain(key: Key) -> None:
    """After a turn finishes, run the next queued prompt (if any) the same
    way — recursing through run_turn so a queue longer than one item keeps
    draining until empty or the instance gets paused."""
    st = _chats.get(key)
    if st is None or not st.queue or is_paused(key[0]):
        return
    nxt = st.queue.pop(0)
    await run_turn(
        nxt.prompt,
        action_type=nxt.action_type,
        user_id=nxt.user_id,
        instance_id=key[0],
        chat_id=key[1],
        thread_id=key[2],
        backend_override=nxt.backend_override,
        context=nxt.context,
        background=True,
        on_result=nxt.on_result,
    )
