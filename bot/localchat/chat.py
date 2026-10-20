"""One Local Chat turn: ABP's native agent on the session's model (on the ModelMistress hub) and workspace.

Every turn runs through one internal "Local Chat" app instance (platform "app": no external chat platform, reachable only
from ABP's own UIs), so tool approvals, traces, the memory fabric, the e-stop and the privacy check work exactly as they do
for every other bot. The session picks the model per turn (router.ask(model_override=...)).

    send(sid, text, broadcast)   starts the turn in the background and returns its id at once; the reply streams to the
                                 dashboard sockets as chat_stream frames (chat_id "localchat:<sid>"), approvals as
                                 local_chat_approval frames, the end as local_chat_turn, status changes as local_chat_session
    cancel(sid)                  stops the session's running turn
    steer(sid, text, how)        while a turn runs: "steer" adds the message to the running turn, "queue" keeps it for the
                                 next turn (started by itself when the current one finishes)
    rewind(sid, turn_id)         puts the workspace's files back as they were before that turn's tools ran (a checkpoint is
                                 taken at the start of every Build / Plan turn); the turn's messages stay, marked reverted

Modes: chat (no tools), build (the agent's tools; edits ask as usual), plan (proposes a plan for approval first),
review (read-only tools), factory (the message is a goal: the conductor plans it and nothing runs until the owner's Run -
bot/localchat/factory.py). Autonomy 0-5 sets the turn's permission mode and rules (bot/localchat/autonomy.py). Cloud assist:
"off" never leaves the machine; "ask" lets a single message go to the configured cloud backend (send(..., cloud=True));
"auto" sends to the cloud when the capacity governor is cloud-led and keeps everything local otherwise. A turn works in the session's worktree when it has one (bot/localchat/worktrees.py); Build and
Plan never work directly in the live ABP checkout.
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
_steer: dict[str, asyncio.Queue] = {}
WRITES = ("build", "plan")          # modes whose tools can change files: checkpointed, kept out of the live checkout


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


def _is_live_checkout(path: str) -> bool:
    try:
        from bot.modules import registry

        live = Path(registry.abp_root()).resolve()
        here = Path(path).resolve()
        return here == live or live in here.parents
    except Exception:  # noqa: BLE001
        return False


def model_ref(model: str) -> str:
    return model if model.startswith("modelmistress/") else f"modelmistress/{model}"


def is_running(sid: str) -> bool:
    t = _running.get(sid)
    return t is not None and not t.done()


def session_frame(session: Optional[dict]) -> Optional[dict]:
    """The local_chat_session frame for a changed session (status, unread, worktree, title...)."""
    if not session:
        return None
    keep = ("id", "title", "status", "unread", "pinned", "archived", "group_id", "parent_id", "link", "model", "mode", "updated",
            "workspace")
    return {"type": "local_chat_session", "session_id": session["id"], "session": {k: session.get(k) for k in keep},
            "worktree": (session.get("worktree") or {}).get("branch")}


async def _status(broadcast: Broadcast, sid: str, status: str, **kw: Any) -> None:
    try:
        s = await asyncio.to_thread(store.set_status, sid, status, **kw)
    except KeyError:
        return
    frame = session_frame(s)
    if frame:
        await _safe(broadcast, frame)


def wants_cloud(session: dict, cloud: bool) -> bool:
    """Whether this turn goes to the cloud backend. "off" refuses a cloud request (and never decides on its own)."""
    assist = session.get("cloud_assist") or "ask"
    if assist == "off":
        if cloud:
            raise ValueError("cloud assist is off for this session: switch it to Ask to send a message to a cloud model")
        return False
    if cloud:
        return True
    if assist == "auto":
        try:
            from bot.capacity import governor

            return governor.effective_state() == governor.CLOUD_LED
        except Exception:  # noqa: BLE001 - no governor: stay local
            return False
    return False


async def send(sid: str, text: str, broadcast: Broadcast, *, cloud: bool = False) -> dict[str, Any]:
    text = (text or "").strip()
    if not text:
        raise ValueError("nothing to send")
    session = store.get(sid)
    if session is None:
        raise KeyError(sid)
    if is_running(sid):
        raise RuntimeError("this session is still answering; steer it, queue the message, or cancel it first")
    if session.get("link") == "worker":
        raise ValueError("a Factory worker's session is read-only: talk to the session that started the run")
    if session["mode"] == "factory":
        return await _factory_send(sid, text, session, broadcast)
    to_cloud = wants_cloud(session, cloud)
    from bot.localchat import worktrees

    cwd = worktrees.cwd_of(session)
    if session["mode"] in WRITES and cwd and not session.get("worktree") and _is_live_checkout(cwd):
        raise ValueError("Build and Plan never work in the live ABP checkout: give this session a worktree first")
    model = session["model"]
    if not model and not to_cloud:
        from bot.localchat import catalog

        model = catalog.default_model(await asyncio.to_thread(catalog.get))
        if not model:
            raise LookupError("no local chat model is available: load or download one on ModelMistress")
        store.update(sid, model=model)
    if not to_cloud:
        gpu_check(model)
    turn_id = store.start_turn(sid)
    store.add_message(sid, "user", text, turn_id=turn_id, model=CLOUD if to_cloud else model, meta={"cloud": True} if to_cloud else None)
    _steer[sid] = asyncio.Queue()
    task = asyncio.create_task(_turn(sid, turn_id, text, model, store.get(sid) or session, broadcast, to_cloud),
                               name=f"localchat-{sid}")
    _running[sid] = task
    return {"turn_id": turn_id, "model": CLOUD if to_cloud else model, "chat_id": chat_id(sid), "cloud": to_cloud}


def gpu_check(model: str) -> None:
    """One GPU, shared: a chat turn may not load a model that would unload the one a running Factory run works on (the
    hub's plan says what loading it unloads). The Factory's own model, or one that fits beside it, is fine."""
    from bot.localchat import factory

    busy = factory.busy_models() - {model}
    if not busy:
        return
    try:
        from bot.localchat import catalog

        row = next((r for r in catalog.get().get("models", []) if r["id"] == model), None)
    except Exception:  # noqa: BLE001 - no catalog: nothing known to unload
        return
    unload = set(((row or {}).get("plan") or {}).get("unload") or [])
    clash = sorted(unload & busy)
    if clash and not (row or {}).get("loaded"):
        raise RuntimeError(f"loading {model.split(':')[-1]} would unload {', '.join(c.split(':')[-1] for c in clash)}, which a "
                           "Factory run is using: pick that model for this session, or wait for the run to finish")


CLOUD = "cloud"            # the model a cloud-assisted message is recorded under (the router's configured cloud backend)


async def _factory_send(sid: str, goal: str, session: dict, broadcast: Broadcast) -> dict[str, Any]:
    """Factory mode: the message is a goal. The conductor plans it in the background; the plan arrives as a message with a
    Run button. Nothing runs here."""
    from bot.localchat import factory

    async def plan_it() -> None:
        state = "done"
        await _status(broadcast, sid, "working", factory="plan")
        try:
            await factory.preview(sid, goal)
        except asyncio.CancelledError:
            state = "cancelled"
            store.add_message(sid, "system", "Stopped planning.")
        except Exception as e:  # noqa: BLE001 - the session shows why there is no plan
            state = "error"
            store.add_message(sid, "user", goal)
            store.add_message(sid, "error", f"No plan: {type(e).__name__}: {e}"[:2000])
        finally:
            _running.pop(sid, None)
            await _status(broadcast, sid, {"done": "done", "cancelled": "idle", "error": "error"}[state], unread=True)
            await _safe(broadcast, {"type": "local_chat_turn", "session_id": sid, "turn_id": "", "state": state, "factory": "plan"})
    _running[sid] = asyncio.create_task(plan_it(), name=f"localchat-plan-{sid}")
    return {"turn_id": "", "model": session["model"], "chat_id": chat_id(sid), "factory": "plan"}


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


def steer(sid: str, text: str, how: str = "steer") -> dict[str, Any]:
    """A message while the session works: "steer" reaches the running turn at its next step; "queue" (or a session that is
    not running) keeps it for the next turn."""
    text = (text or "").strip()
    if not text:
        raise ValueError("nothing to send")
    if store.get(sid) is None:
        raise KeyError(sid)
    if how not in ("steer", "queue"):
        raise ValueError("how is steer or queue")
    if how == "steer" and is_running(sid) and sid in _steer:
        _steer[sid].put_nowait(text)
        turn = store.running_turns(sid)
        store.add_message(sid, "user", text, turn_id=turn[0]["id"] if turn else "", meta={"steered": True})
        return {"steered": True, "queued": False}
    qid = store.queue_message(sid, text)
    return {"steered": False, "queued": True, "id": qid}


async def _checkpoint(sid: str, turn_id: str, cwd: Optional[str], mode: str) -> None:
    if not cwd or mode not in WRITES:
        return
    from bot.agent_runtime import checkpoints

    try:
        commit = await asyncio.to_thread(checkpoints.mark, Path(cwd), f"localchat {sid} before turn {turn_id}")
        store.add_checkpoint(turn_id, sid, cwd, commit)
    except Exception as e:  # noqa: BLE001 - no checkpoint means no rewind for this turn, not a failed turn
        logger.info("local chat: no checkpoint for turn %s: %s", turn_id, e)


def rewind(sid: str, turn_id: str) -> dict[str, Any]:
    """Undo a turn: the workspace's files go back to the checkpoint taken before it (and every later turn), the messages of
    those turns are marked reverted (nothing is deleted), and the state being left is itself checkpointed."""
    if store.get(sid) is None:
        raise KeyError(sid)
    if is_running(sid):
        raise RuntimeError("stop the running turn before rewinding")
    cp = next((c for c in store.checkpoints(sid) if c["turn_id"] == turn_id), None)
    if cp is None:
        raise LookupError("that turn has no checkpoint (Chat and Review turns change no files)")
    from bot.agent_runtime import checkpoints

    undo_point = checkpoints.restore(Path(cp["workspace"]), cp["commit_id"])
    n = store.mark_rewound(sid, turn_id)
    store.add_message(sid, "system", f"Rewound: the files are back as they were before this turn. {n} message(s) are marked "
                      f"reverted. The state before the rewind is kept as checkpoint {undo_point[:10]}.",
                      meta={"rewind": turn_id, "undo_point": undo_point})
    store.add_event(sid, "rewind", turn_id=turn_id, undo_point=undo_point)
    return {"rewound": turn_id, "messages_reverted": n, "undo_point": undo_point, "workspace": cp["workspace"]}


async def _turn(sid: str, turn_id: str, text: str, model: str, session: dict, broadcast: Broadcast, to_cloud: bool = False) -> None:
    from bot.dashboard.chat_stream import ChatStreamer
    from bot.localchat import worktrees
    from bot.router import router

    instance_id = await asyncio.to_thread(ensure_instance)
    cid = chat_id(sid)
    streamer = ChatStreamer(broadcast, instance_id, cid, turn_id)
    waiting = {"on": False}
    await _status(broadcast, sid, "working", turn_id=turn_id)

    async def approval_notify(approval_id: int, tool_name: str, tool_input: dict) -> None:
        from bot import approvals_view

        try:
            pv = approvals_view.preview(tool_name, tool_input)
        except Exception:  # noqa: BLE001
            pv = {"summary": tool_name, "kind": "text", "body": ""}
        waiting["on"] = True
        await _status(broadcast, sid, "needs_you", approval_id=approval_id, tool=tool_name)
        await _safe(broadcast, {"type": "local_chat_approval", "session_id": sid, "turn_id": turn_id,
                                "approval_id": approval_id, "tool": tool_name, "preview": pv})

    async def stream_notify(event: Any) -> None:
        if waiting["on"]:                   # the agent moved on: whatever it waited for was decided
            waiting["on"] = False
            await _status(broadcast, sid, "working", turn_id=turn_id)
        await streamer.notify(event)

    context: dict[str, Any] = {"stream_notify": stream_notify, "approval_notify": approval_notify,
                               "steer_queue": _steer.get(sid) or asyncio.Queue()}
    allowed = tools_for(session["mode"])
    if allowed is not None:
        context["allowed_tools"] = allowed
    if session["mode"] == "plan":
        context["plan_first"] = True
    from bot.localchat import autonomy

    pol = autonomy.policy(session.get("autonomy", 2))
    context["permission_mode"] = "plan" if session["mode"] == "plan" else pol["permission_mode"]
    context["permission_rules"] = pol["rules"]
    if session.get("effort"):
        context["effort"] = session["effort"]
    ws = _workspace(worktrees.cwd_of(session))
    if ws:
        context["cwd"] = ws
    wt_root = (session.get("worktree") or {}).get("path")
    await _checkpoint(sid, turn_id, wt_root if wt_root and Path(wt_root).is_dir() else ws, session["mode"])   # the whole worktree
    from bot.localchat import housekeeping, links

    if not to_cloud:
        await _maybe_compact(sid, model, broadcast)
    prompt = text
    brief = links.pending_context(sid)              # a fork / task / hand-off starts with its parent's context
    summary = housekeeping.pending_summary(sid)     # the first turn after a compaction starts from the summary
    for head in (summary, brief):
        if head:
            prompt = f"{head}\n\n---\n\n{prompt}"
    if session["mode"] in ("build", "plan", "review") and localchat.settings().get("next_step_box"):
        prompt = f"{prompt}\n\n{housekeeping.NEXT_STEP_INSTRUCTIONS}"
    agent_cid = housekeeping.agent_chat_id(sid)
    from bot.agent_runtime.transports.base import speed_observer

    speeds: list[float] = []
    observer = speed_observer.set(lambda m, tps: speeds.append(tps) if m == model else None)
    started = time.monotonic()
    state, reply, tokens = "done", "", 0
    try:
        if to_cloud:                       # the configured cloud backend (the router's default), same tools and approvals
            result = await router.ask(prompt, action_type="local_chat", context=context, instance_id=instance_id, chat_id=agent_cid)
        else:
            result = await router.ask(prompt, action_type="local_chat", backend_override="native_agent", context=context,
                                      instance_id=instance_id, chat_id=agent_cid, model_override=model_ref(model))
        reply, tokens = result.text or "", int(result.tokens or 0)
        reply, extra = housekeeping.steps_and_next(reply)
        tps = 0.0 if to_cloud else _record_speed(model, speeds)
        store.add_message(sid, "assistant", reply, turn_id=turn_id, model=CLOUD if to_cloud else model, tokens=tokens,
                          tokens_per_s=tps, meta={**({"cloud": True} if to_cloud else {}), **extra} or None)
    except asyncio.CancelledError:
        state = "cancelled"
        store.add_message(sid, "system", "Stopped.", turn_id=turn_id, model=model)
    except Exception as e:  # noqa: BLE001 - the session shows the error instead of hanging
        state = "error"
        logger.warning("local chat turn %s failed: %s", turn_id, e)
        store.add_message(sid, "error", _explain(e, to_cloud), turn_id=turn_id, model=model)
    finally:
        speed_observer.reset(observer)
        store.finish_turn(turn_id, state)
        _running.pop(sid, None)
        _steer.pop(sid, None)
        await _safe_finish(streamer)
        await _status(broadcast, sid, {"done": "done", "cancelled": "idle", "error": "error"}[state], unread=True, turn_id=turn_id)
        await _safe(broadcast, {"type": "local_chat_turn", "session_id": sid, "turn_id": turn_id, "state": state,
                                "model": model, "tokens": tokens, "seconds": round(time.monotonic() - started, 1)})
        await _after_turn(sid, state, broadcast)


async def _after_turn(sid: str, state: str, broadcast: Broadcast) -> None:
    """A finished turn: the next queued message starts (only after a clean finish), and a linked session's parent hears about
    it (bot/localchat/links.py, when present)."""
    try:
        from bot.localchat import links

        await links.on_turn_end(sid, state, broadcast)
    except Exception:  # noqa: BLE001 - a note that could not be delivered must not break the session
        logger.debug("local chat: parent note failed for %s", sid, exc_info=True)
    if state != "done":
        return
    nxt = store.pop_queued(sid)
    if nxt:
        try:
            await send(sid, nxt, broadcast)
        except Exception as e:  # noqa: BLE001 - say why the queued message did not start, keep it
            store.queue_message(sid, nxt)
            store.add_message(sid, "system", f"A queued message did not start: {e}")


def _explain(e: Exception, to_cloud: bool) -> str:
    """The error a turn ended with, said plainly: a hub that is down or degraded says so (and what to do)."""
    raw = f"{type(e).__name__}: {e}"[:1500]
    if to_cloud:
        return raw + " (the cloud backend did not answer: Local Chat still works locally with cloud assist Off)"
    try:
        from bot.modelmistress import supervisor

        hub = supervisor.status()
    except Exception:  # noqa: BLE001
        return raw
    if not hub.get("up"):
        return ("ModelMistress (the local model hub) is not running, so the local model could not answer. Start it from "
                "Modules -> ModelMistress (ABP restarts it by itself when autostart is on), then press Retry. " + f"({raw})")
    if hub.get("degraded"):
        return f"ModelMistress is running but degraded ({hub['degraded']}). " + raw
    return raw


def context_limit(model: str) -> Optional[int]:
    """The tokens the hub serves the model per request (its plan), or None when the hub does not say."""
    try:
        from bot.localchat import catalog

        row = next((r for r in catalog.get().get("models", []) if r["id"] == model), None)
        v = ((row or {}).get("plan") or {}).get("n_ctx_per_slot")
        return int(v) if v else None
    except Exception:  # noqa: BLE001
        return None


async def summarize_with(model: str, sid: str, text: str) -> str:
    """The session's local model, no tools, summarizing older turns for a compaction."""
    from bot.router import router

    instance_id = await asyncio.to_thread(ensure_instance)
    prompt = ("Summarize this conversation for yourself so you can continue the work: the goal, decisions made, files touched, "
              "what is done and what is left, open questions. Be concise (under 400 words).\n\n" + text[-60000:])
    res = await router.ask(prompt, action_type="local_chat", backend_override="native_agent", instance_id=instance_id,
                           chat_id=f"localchat:{sid}:summary", model_override=model_ref(model),
                           context={"allowed_tools": frozenset()})
    return res.text or ""


async def _maybe_compact(sid: str, model: str, broadcast: Broadcast) -> None:
    from bot.localchat import housekeeping

    limit = await asyncio.to_thread(context_limit, model)
    if not housekeeping.needs_compaction(sid, limit):
        return
    try:
        res = await housekeeping.compact(sid, lambda t: summarize_with(model, sid, t))
    except Exception:  # noqa: BLE001 - a turn goes ahead even if compaction could not run
        logger.info("local chat: compaction failed for %s", sid, exc_info=True)
        return
    if res.get("compacted"):
        await _safe(broadcast, {"type": "local_chat_compacted", "session_id": sid, **res})


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
