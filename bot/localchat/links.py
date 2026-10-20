"""Linked Local Chat sessions (PLAN-local-chat.md 4b, S3): work that branches off a session runs in its own session, and the
parent hears back.

    fork(sid)                     a new session with this one's conversation as context, in its own worktree cut from this
                                  session's branch tip (committed work only: uncommitted changes are reported, not carried)
    task(sid, brief, start=True)  a fresh session with just a brief (and its own worktree), started on it
    handoff(sid, plan)            the in-progress task moves to a new session: the remaining plan plus the recent
                                  conversation as its brief; the parent says where it went (and stops, when asked)
    children(sid)                 the sessions linked under this one, with their status
    register_worker(...)          a Factory worker (a conductor task in its own confined worktree) as a read-only linked
                                  session under the session that started the run; worker_update() adds what it did

When a linked session ends a turn, on_turn_end() leaves a short note (its recap) in the parent and tells the sockets, so
the parent can continue from it. The context a child starts with is kept as a system message marked for the agent; its
first turn receives it.
"""
from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable, Optional

from bot.localchat import store

logger = logging.getLogger("bot.localchat.links")

Broadcast = Callable[[dict], Awaitable[None]]
NOTE_CHARS = 600
TRANSCRIPT_CHARS = 24_000
LINK_LABEL = {"fork": "fork", "task": "task", "handoff": "hand-off", "worker": "worker"}


def _session(sid: str) -> dict:
    s = store.get(sid)
    if s is None:
        raise KeyError(sid)
    return s


def _clip(text: str, n: int) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= n else text[: n - 1] + "…"


def transcript(sid: str, limit: int = TRANSCRIPT_CHARS) -> str:
    """The conversation so far (reverted turns left out), newest kept when it is long."""
    lines = []
    for m in store.messages(sid, include_reverted=False):
        if m["role"] in ("user", "assistant") and not (m.get("meta") or {}).get("context_for_agent"):
            lines.append(f"{'User' if m['role'] == 'user' else 'Assistant'}: {m['text']}")
    text = "\n\n".join(lines)
    return text if len(text) <= limit else "…(earlier messages left out)\n\n" + text[-limit:]


def _child(parent: dict, link: str, title: str, *, mode: Optional[str] = None) -> dict:
    return store.create(title=title[:60], model=parent["model"], mode=mode or parent["mode"], autonomy=parent["autonomy"],
                        workspace=parent["workspace"], cloud_assist=parent["cloud_assist"], parent_id=parent["id"], link=link,
                        group_id=parent.get("group_id") or "", effort=parent.get("effort") or "")


def _give_worktree(child: dict, parent: dict, base: Optional[str]) -> dict:
    """The child's own worktree: from the parent's branch tip when the parent has one, else (like any new code session)
    from the repository's current branch. Returns what happened, for the result."""
    from bot import localchat
    from bot.localchat import worktrees

    pwt = parent.get("worktree")
    out: dict[str, Any] = {}
    try:
        if pwt:
            dirty = worktrees.status(parent["id"])["dirty"]
            if dirty:
                out["not_carried"] = dirty
            out["worktree"] = worktrees.create(child["id"], base=base or pwt["branch"])
        elif child["mode"] != "chat" and localchat.settings().get("worktrees") == "auto" and worktrees.repo_of(child["workspace"]):
            out["worktree"] = worktrees.create(child["id"], base=base)
    except worktrees.WorktreeError as e:
        out["worktree_error"] = str(e)
        store.add_message(child["id"], "system", f"No worktree for this session: {e}")
    return out


def _context(child_sid: str, text: str, **meta: Any) -> None:
    store.add_message(child_sid, "system", text, meta={"context_for_agent": True, **meta})


def fork(sid: str, *, title: str = "", base: Optional[str] = None) -> dict:
    parent = _session(sid)
    child = _child(parent, "fork", title or f"Fork of {parent['title'] or 'session'}")
    convo = transcript(sid)
    _context(child["id"], f"This session was forked from \"{parent['title'] or sid}\". The conversation so far:\n\n{convo}"
             if convo else f"This session was forked from \"{parent['title'] or sid}\" (no conversation yet).", parent=sid)
    out = _give_worktree(child, parent, base)
    store.add_message(sid, "system", f"Forked into \"{child['title']}\".", meta={"child": child["id"], "link": "fork"})
    store.add_event(sid, "link", action="fork", child=child["id"])
    if out.get("not_carried"):
        store.add_message(child["id"], "system", f"{len(out['not_carried'])} uncommitted change(s) in the parent were not carried "
                          "over (only committed work is in this worktree): " + ", ".join(out["not_carried"][:20]))
    return {"session": store.get(child["id"]), **out}


def task(sid: str, brief: str, *, title: str = "") -> dict:
    brief = (brief or "").strip()
    if not brief:
        raise ValueError("a task needs a brief")
    parent = _session(sid)
    child = _child(parent, "task", title or _clip(brief, 60))
    _context(child["id"], f"A task started from the session \"{parent['title'] or sid}\". Report back with a short recap when done.",
             parent=sid)
    out = _give_worktree(child, parent, None)
    store.add_message(sid, "system", f"Started the task \"{child['title']}\".", meta={"child": child["id"], "link": "task"})
    store.add_event(sid, "link", action="task", child=child["id"])
    return {"session": store.get(child["id"]), "brief": brief, **out}


def handoff(sid: str, plan: str, *, title: str = "") -> dict:
    plan = (plan or "").strip()
    if not plan:
        raise ValueError("say what is left to do (the remaining plan)")
    parent = _session(sid)
    child = _child(parent, "handoff", title or f"Continued: {parent['title'] or 'session'}")
    recent = transcript(sid, limit=8_000)
    _context(child["id"], f"This task was handed off from the session \"{parent['title'] or sid}\".\n\nWhat is left to do:\n{plan}"
             + (f"\n\nThe recent conversation:\n\n{recent}" if recent else ""), parent=sid)
    out = _give_worktree(child, parent, None)
    store.add_message(sid, "system", f"Handed off to \"{child['title']}\". What was left: {_clip(plan, 300)}",
                      meta={"child": child["id"], "link": "handoff"})
    store.add_event(sid, "link", action="handoff", child=child["id"])
    return {"session": store.get(child["id"]), "plan": plan, **out}


def children(sid: str) -> list[dict]:
    _session(sid)
    rows = store.list_sessions(parent_id=sid, limit=500) + store.list_sessions(parent_id=sid, archived=True, limit=500)
    return [{k: r.get(k) for k in ("id", "title", "status", "unread", "link", "model", "mode", "updated", "archived")}
            | {"branch": (r.get("worktree") or {}).get("branch")} for r in rows]


def pending_context(sid: str) -> str:
    """The context a linked session's first turn starts with ("" for an ordinary session or after its first turn)."""
    if len(store.turns(sid)) > 1:          # the turn being started is already recorded
        return ""
    parts = [m["text"] for m in store.messages(sid) if m["role"] == "system" and (m.get("meta") or {}).get("context_for_agent")]
    return "\n\n".join(parts)


def recap(sid: str) -> str:
    for m in reversed(store.messages(sid, include_reverted=False)):
        if m["role"] in ("assistant", "error"):
            return _clip(m["text"], NOTE_CHARS)
    return ""


async def on_turn_end(sid: str, state: str, broadcast: Broadcast) -> None:
    """A linked session ended a turn: a short note (its recap) lands in the parent."""
    s = store.get(sid)
    if not s or not s.get("parent_id") or s.get("link") not in LINK_LABEL:
        return
    parent = store.get(s["parent_id"])
    if parent is None:
        return
    word = {"done": "finished a turn", "error": "stopped with an error", "cancelled": "was stopped"}.get(state, state)
    text = f"{LINK_LABEL[s['link']].capitalize()} \"{s['title'] or sid}\" {word}." + (f" Recap: {recap(sid)}" if recap(sid) else "")
    store.add_message(parent["id"], "system", text, meta={"child": sid, "note": True, "state": state})
    store.add_event(parent["id"], "child_turn", child=sid, state=state)
    from bot.localchat import chat

    try:
        with_unread = store.set_status(parent["id"], parent["status"] if parent["status"] != "working" else "working", unread=True)
        frame = chat.session_frame(with_unread or store.get(parent["id"]))
        if frame:
            await broadcast(frame)
        await broadcast({"type": "local_chat_note", "session_id": parent["id"], "child_id": sid, "state": state, "text": text})
    except Exception:  # noqa: BLE001 - the note is stored either way
        logger.debug("local chat: note broadcast failed", exc_info=True)


# ---- Factory workers -------------------------------------------------------------------------------------------------------
def register_worker(parent_sid: str, *, task_id: str, title: str, worktree: Optional[dict] = None, model: str = "") -> dict:
    """A conductor worker as a read-only linked session under the session that started the Factory run. Its worktree (the
    conductor's, confined) is recorded with keep on, so nothing here removes it."""
    parent = _session(parent_sid)
    for c in store.list_sessions(parent_id=parent_sid, limit=500):
        if c.get("link") == "worker" and (c.get("worktree") or {}).get("task_id") == task_id:
            return c
    child = store.create(title=title[:60] or f"Worker {task_id}", model=model or parent["model"], mode="review",
                         autonomy=0, workspace="", cloud_assist="off", parent_id=parent_sid, link="worker",
                         group_id=parent.get("group_id") or "")
    if worktree:
        store.set_worktree(child["id"], {**worktree, "keep": True, "task_id": task_id, "worker": True})
    else:
        store.set_worktree(child["id"], {"task_id": task_id, "worker": True, "keep": True, "path": "", "branch": "", "base": "",
                                         "repo": ""})
    store.add_message(child["id"], "system", f"Factory worker for task {task_id}: confined, read-only here. Its progress appears below.")
    store.add_event(parent_sid, "link", action="worker", child=child["id"], task_id=task_id)
    return store.get(child["id"]) or child


def worker_update(child_sid: str, *, status: str, text: str = "") -> dict:
    s = _session(child_sid)
    if s.get("link") != "worker":
        raise ValueError("not a worker session")
    if text:
        store.add_message(child_sid, "assistant" if status in ("done", "error") else "system", text)
    store.set_status(child_sid, status, unread=status in ("done", "error", "needs_you"))
    return store.get(child_sid) or s


def factory_observer(parent_sid: str, broadcast: Optional[Broadcast] = None, *, repo: str = "", base: str = "", model: str = ""):
    """The conductor's on_event observer for a Factory run started from a Local Chat session: every task becomes a linked,
    read-only worker session under it, updated as the task is tried, reviewed, accepted or escalated."""
    from bot.localchat import chat

    async def observe(event: dict) -> None:
        tid = f"{event.get('plan_id', '')}:{event.get('task_id', '')}"
        wt = {"path": str(event.get("worktree") or ""), "branch": str(event.get("branch") or ""), "base": base, "repo": repo}
        child = register_worker(parent_sid, task_id=tid, title=str(event.get("title") or tid), worktree=wt, model=model)
        kind = event.get("kind")
        if kind == "task_start":
            s = worker_update(child["id"], status="working", text=f"Attempt {event.get('attempt', 1)} started"
                              + (" with the reviewer's feedback." if event.get("feedback") else "."))
        elif kind == "task_review":
            found = "; ".join(event.get("findings") or [])
            s = worker_update(child["id"], status="working", text=f"Review: {event.get('verdict')}." + (f" {found}" if found else ""))
        elif kind == "task_accepted":
            s = worker_update(child["id"], status="done", text=f"Accepted on {event.get('branch')}. {event.get('summary') or ''}".strip())
        elif kind == "task_escalated":
            s = worker_update(child["id"], status="needs_you", text=f"Escalated: {event.get('reason')}"
                              + (f" Question: {event['question']}" if event.get("question") else ""))
        else:
            return
        if broadcast is not None:
            frame = chat.session_frame(s)
            if frame:
                await broadcast(frame)
    return observe
