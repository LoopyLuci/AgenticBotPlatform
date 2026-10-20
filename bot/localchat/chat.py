"""One Local Chat turn: ABP's native agent on the session's model (on the ModelMistress hub) and workspace.

Every turn runs through one internal "Local Chat" app instance (platform "app": no external chat platform, reachable only
from ABP's own UIs), so tool approvals, traces, the memory fabric, the e-stop and the privacy check work exactly as they do
for every other bot. The session picks the model per turn (router.ask(model_override=...)).

    send(sid, text, broadcast)   starts the turn in the background and returns its id at once; the reply streams to the
                                 dashboard sockets as chat_stream frames (chat_id "localchat:<sid>"), approvals as
                                 local_chat_approval frames, the end as local_chat_turn
    cancel(sid)                  stops the session's running turn

Modes: chat (no tools), build (the agent's tools; edits ask as usual), plan (proposes a plan for approval first),
review (read-only tools).
"""
from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from bot import localchat
from bot.localchat import store

logger = logging.getLogger("bot.localchat.chat")

Broadcast = Callable[[dict], Awaitable[None]]
_running: dict[str, asyncio.Task] = {}


def chat_id(sid: str) -> str:
    return f"localchat:{sid}"


def ensure_instance() -> int:
    """The internal app instance every Local Chat turn runs through (created on first use)."""
    from bot import bot_instances

    name = str(localchat.settings()["instance_name"])
    for inst in bot_instances.list_instances(platform="app"):
        if inst.get("name") == name:
            return int(inst["id"])
    try:
        return bot_instances.create_instance(name, "app", "native_agent", {}, [], actor="local-chat",
                                             custom_instructions=None)
    except bot_instances.ValidationError:      # created meanwhile by another request
        for inst in bot_instances.list_instances(platform="app"):
            if inst.get("name") == name:
                return int(inst["id"])
        raise


def tools_for(mode: str) -> Optional[frozenset]:
    """The tools a mode offers: None = the agent's full set, an empty set = none."""
    if mode == "chat":
        return frozenset()
    if mode == "review":
        from bot.agent_runtime import toolspec

        names = set(toolspec.builtin_names()) | set(getattr(toolspec, "_registered", {}) or {})
        return frozenset(n for n in names if toolspec.is_read_only(n))
    return None


def _workspace(path: str) -> Optional[str]:
    if not path:
        return None
    p = Path(path).expanduser()
    return str(p) if p.is_dir() else None


def model_ref(model: str) -> str:
    return model if model.startswith("modelmistress/") else f"modelmistress/{model}"


def is_running(sid: str) -> bool:
    t = _running.get(sid)
    return t is not None and not t.done()


async def send(sid: str, text: str, broadcast: Broadcast) -> dict[str, Any]:
    text = (text or "").strip()
    if not text:
        raise ValueError("nothing to send")
    session = store.get(sid)
    if session is None:
        raise KeyError(sid)
    if is_running(sid):
        raise RuntimeError("this session is still answering; wait or cancel it first")
    model = session["model"]
    if not model:
        from bot.localchat import catalog

        model = catalog.default_model(await asyncio.to_thread(catalog.get))
        if not model:
            raise LookupError("no local chat model is available: load or download one on ModelMistress")
        store.update(sid, model=model)
    turn_id = store.start_turn(sid)
    store.add_message(sid, "user", text, turn_id=turn_id, model=model)
    task = asyncio.create_task(_turn(sid, turn_id, text, model, session, broadcast), name=f"localchat-{sid}")
    _running[sid] = task
    return {"turn_id": turn_id, "model": model, "chat_id": chat_id(sid)}


async def cancel(sid: str) -> bool:
    task = _running.get(sid)
    if task is None or task.done():
        return False
    task.cancel()
    try:
        await asyncio.wait_for(asyncio.shield(task), timeout=10)
    except (asyncio.CancelledError, asyncio.TimeoutError, Exception):  # noqa: BLE001 - the turn reports its own end
        pass
    return True


async def _turn(sid: str, turn_id: str, text: str, model: str, session: dict, broadcast: Broadcast) -> None:
    from bot.dashboard.chat_stream import ChatStreamer
    from bot.router import router

    instance_id = await asyncio.to_thread(ensure_instance)
    cid = chat_id(sid)
    streamer = ChatStreamer(broadcast, instance_id, cid, turn_id)

    async def approval_notify(approval_id: int, tool_name: str, tool_input: dict) -> None:
        from bot import approvals_view

        try:
            pv = approvals_view.preview(tool_name, tool_input)
        except Exception:  # noqa: BLE001
            pv = {"summary": tool_name, "kind": "text", "body": ""}
        await _safe(broadcast, {"type": "local_chat_approval", "session_id": sid, "turn_id": turn_id,
                                "approval_id": approval_id, "tool": tool_name, "preview": pv})

    context: dict[str, Any] = {"stream_notify": streamer.notify, "approval_notify": approval_notify}
    allowed = tools_for(session["mode"])
    if allowed is not None:
        context["allowed_tools"] = allowed
    if session["mode"] == "plan":
        context["plan_first"] = True
    ws = _workspace(session.get("workspace") or "")
    if ws:
        context["cwd"] = ws
    from bot.agent_runtime.transports.base import speed_observer

    speeds: list[float] = []
    observer = speed_observer.set(lambda m, tps: speeds.append(tps) if m == model else None)
    started = time.monotonic()
    state, reply, tokens = "done", "", 0
    try:
        result = await router.ask(text, action_type="local_chat", backend_override="native_agent", context=context,
                                  instance_id=instance_id, chat_id=cid, model_override=model_ref(model))
        reply, tokens = result.text or "", int(result.tokens or 0)
        tps = _record_speed(model, speeds)
        store.add_message(sid, "assistant", reply, turn_id=turn_id, model=model, tokens=tokens, tokens_per_s=tps)
    except asyncio.CancelledError:
        state = "cancelled"
        store.add_message(sid, "system", "Stopped.", turn_id=turn_id, model=model)
    except Exception as e:  # noqa: BLE001 - the session shows the error instead of hanging
        state = "error"
        logger.warning("local chat turn %s failed: %s", turn_id, e)
        store.add_message(sid, "error", f"{type(e).__name__}: {e}"[:2000], turn_id=turn_id, model=model)
    finally:
        speed_observer.reset(observer)
        store.finish_turn(turn_id, state)
        _running.pop(sid, None)
        await _safe_finish(streamer)
        await _safe(broadcast, {"type": "local_chat_turn", "session_id": sid, "turn_id": turn_id, "state": state,
                                "model": model, "tokens": tokens, "seconds": round(time.monotonic() - started, 1)})


def _record_speed(model: str, speeds: list[float]) -> float:
    """The turn's generation speed: llama.cpp's own figure for each model call of the turn (the transport reports it; no
    hub tracing needed), averaged and kept for the picker. 0 when the server did not say."""
    if not speeds:
        return 0.0
    tps = sum(speeds) / len(speeds)
    try:
        from bot.localchat import speed

        speed.record(model, _device(), tps, "reply", **_size(model))
    except Exception:  # noqa: BLE001 - speed is a nicety; the reply is what matters
        logger.debug("local chat: could not keep the speed for %s", model, exc_info=True)
    return round(tps, 2)


def _device() -> str:
    try:
        from bot.localchat import catalog

        return str(catalog.get().get("device") or "cpu")
    except Exception:  # noqa: BLE001
        return "cpu"


def _size(model: str) -> dict:
    try:
        from bot.localchat import catalog, speed

        for r in catalog.get().get("models", []):
            if r["id"] == model:
                md = r["metadata"]
                row = {"size_gb": md.get("size_gb"), "size_label": md.get("size_label"), "experts": md.get("experts"),
                       "experts_used": md.get("experts_used")}
                return {"size_gb": float(md.get("size_gb") or 0), "active_fraction": speed.active_fraction(row)}
    except Exception:  # noqa: BLE001
        pass
    return {}


async def _safe(broadcast: Broadcast, frame: dict) -> None:
    try:
        await broadcast(frame)
    except Exception:  # noqa: BLE001 - a dead socket must never fail the turn
        logger.debug("local chat broadcast failed", exc_info=True)


async def _safe_finish(streamer: Any) -> None:
    try:
        await streamer.finish()
    except Exception:  # noqa: BLE001
        logger.debug("local chat stream finish failed", exc_info=True)


async def benchmark(model: str) -> dict[str, Any]:
    """Measure the model's generation speed on this machine (the hub's bench.run) and keep it for the picker."""
    from bot.localchat import catalog, speed
    from bot.modelmistress import client

    res = await asyncio.to_thread(lambda: client.call("bench.run", {"model": model}, timeout=900.0))
    tps = 0.0
    for r in (res or {}).get("results") or [] if isinstance(res, dict) else []:
        if r.get("test") == "generate" and r.get("tokens_per_s"):
            tps = float(r["tokens_per_s"])
            break
    if tps > 0:
        speed.record(model, _device(), tps, "bench", **_size(model))
        catalog.forget()
    return {"model": model, "tokens_per_s": tps or None, "raw": res}
