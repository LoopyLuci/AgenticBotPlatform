"""Keeping a long Local Chat session healthy (PLAN-local-chat.md 4b, S4).

    needs_compaction(sid, limit)   whether the conversation since the last compaction is near the model's context per request
    compact(sid, summarize)        summarize the older turns into one visible "compacted" marker; nothing is deleted. The agent
                                   then continues on a fresh history (generation N) that starts from that summary
    generation(sid) / agent_chat_id(sid)   which agent history a turn uses
    pending_summary(sid)           the summary the first turn after a compaction starts with
    steps_and_next(reply)          the task list and next-step box the agent ended its reply with (fenced abp-steps /
                                   abp-next blocks), parsed out of the text
    add_chapter(sid, title)        a chapter marker in the conversation
    export(sid, fmt)               the whole session as Markdown or JSON
    auto_archive(days)             archive sessions untouched for `days` (never pinned, running or ones needing you;
                                   a worktree with unmerged work is kept, as with any archive)

Settings: local_chat.compact_at (fraction of the context per request, default 0.7; 0 = never), local_chat.auto_archive_days
(0 = off), local_chat.next_step_box (ask the agent for a task list and a next-step question in Build / Plan / Review).
"""
from __future__ import annotations

import json
import re
import time
from typing import Any, Awaitable, Callable, Optional

from bot.localchat import store

DEFAULT_LIMIT = 32768            # the coder's context per request when the hub does not say
KEEP_RECENT = 4                  # the newest messages stay verbatim after a compaction
NEXT_STEP_INSTRUCTIONS = (
    "When you finish this turn, end your reply with two short fenced blocks: ```abp-steps``` listing the steps of the task, "
    "one per line, each starting with [x] (done) or [ ] (to do); then ```abp-next``` holding JSON "
    '{"recap": "two sentences on what is now true", "ask": "one question for me, or empty", "options": ["up to 2 short answers"]}.')
_BLOCK = re.compile(r"```abp-(steps|next)\s*\n(.*?)```", re.S)

Summarize = Callable[[str], Awaitable[str]]


def _compactions(sid: str) -> list[dict]:
    return [m for m in store.messages(sid) if m["role"] == "system" and (m.get("meta") or {}).get("compaction")]


def generation(sid: str) -> int:
    return len(_compactions(sid))


def agent_chat_id(sid: str) -> str:
    g = generation(sid)
    return f"localchat:{sid}" + (f":g{g}" if g else "")


def _since_last(sid: str) -> list[dict]:
    comps = _compactions(sid)
    upto = comps[-1]["meta"]["compaction"]["upto"] if comps else 0
    return [m for m in store.messages(sid, include_reverted=False) if m["id"] > upto and m["role"] in ("user", "assistant")]


def estimate(sid: str) -> int:
    """Estimated tokens the agent's history holds since the last compaction (characters / 4, plus the summary it starts from)."""
    comps = _compactions(sid)
    base = len(comps[-1]["meta"]["compaction"]["summary"]) if comps else 0
    return (base + sum(len(m["text"]) for m in _since_last(sid))) // 4


def compact_at() -> float:
    from bot import localchat

    try:
        return float(localchat.settings().get("compact_at", 0.7))
    except (TypeError, ValueError):
        return 0.7


def needs_compaction(sid: str, limit: Optional[int]) -> bool:
    frac = compact_at()
    if frac <= 0:
        return False
    return estimate(sid) > frac * int(limit or DEFAULT_LIMIT) and len(_since_last(sid)) > KEEP_RECENT


def _fallback_summary(msgs: list[dict]) -> str:
    """When no model can summarize: the gist of each older message, clipped (labelled as such)."""
    lines = ["(Summary made without a model: the start of each earlier message.)"]
    for m in msgs:
        lines.append(f"- {'User' if m['role'] == 'user' else 'Assistant'}: {' '.join(m['text'].split())[:240]}")
    return "\n".join(lines)[:12000]


async def compact(sid: str, summarize: Optional[Summarize] = None, *, reason: str = "near the context limit") -> dict:
    """Summarize everything since the last compaction except the newest KEEP_RECENT messages. The messages stay; a system
    marker holds the summary, and the agent's next turn starts a fresh history (the next generation) from it."""
    msgs = _since_last(sid)
    older = msgs[:-KEEP_RECENT] if len(msgs) > KEEP_RECENT else []
    if not older:
        return {"compacted": False, "why": "nothing old enough to summarize"}
    comps = _compactions(sid)
    prior = comps[-1]["meta"]["compaction"]["summary"] if comps else ""
    convo = "\n\n".join(f"{'User' if m['role'] == 'user' else 'Assistant'}: {m['text']}" for m in older)
    source = "model"
    summary = ""
    if summarize is not None:
        try:
            summary = (await summarize((f"Earlier summary:\n{prior}\n\n" if prior else "") + convo)).strip()
        except Exception:  # noqa: BLE001 - a failed summary falls back, it never loses the conversation
            summary = ""
    if not summary:
        source = "extract"
        summary = ((prior + "\n\n") if prior else "") + _fallback_summary(older)
    keep = msgs[-KEEP_RECENT:]
    recent = "\n\n".join(f"{'User' if m['role'] == 'user' else 'Assistant'}: {m['text']}" for m in keep)
    upto = older[-1]["id"]
    g = generation(sid) + 1
    store.add_message(sid, "system", f"Compacted ({reason}): {len(older)} earlier message(s) summarized; they are still here above. "
                      "The agent continues from the summary.",
                      meta={"compaction": {"generation": g, "upto": upto, "summary": summary, "recent": recent, "source": source,
                                           "messages": len(older), "at": time.time()}})
    store.add_event(sid, "compaction", generation=g, messages=len(older), source=source)
    return {"compacted": True, "generation": g, "messages": len(older), "source": source}


def pending_summary(sid: str) -> str:
    """The summary the first turn of a new generation starts with ("" once that turn has run)."""
    comps = _compactions(sid)
    if not comps:
        return ""
    last = comps[-1]
    if any(m["role"] == "assistant" and m["id"] > last["id"] for m in store.messages(sid, after=last["id"])):
        return ""
    c = last["meta"]["compaction"]
    return f"Summary of our conversation so far:\n{c['summary']}" + (f"\n\nThe most recent messages:\n\n{c['recent']}" if c.get("recent") else "")


def steps_and_next(reply: str) -> tuple[str, dict]:
    """(the reply without the blocks, {"steps": [{"done", "text"}], "next": {"recap", "ask", "options"}})."""
    found: dict[str, Any] = {}
    for kind, body in _BLOCK.findall(reply or ""):
        if kind == "steps":
            steps = []
            for line in body.strip().splitlines():
                m = re.match(r"\s*[-*]?\s*\[( |x|X)\]\s*(.+)", line)
                if m:
                    steps.append({"done": m.group(1).lower() == "x", "text": m.group(2).strip()[:200]})
            if steps:
                found["steps"] = steps[:30]
        else:
            try:
                nxt = json.loads(body.strip())
            except ValueError:
                continue
            if isinstance(nxt, dict):
                opts = [str(o)[:60] for o in (nxt.get("options") or []) if str(o).strip()][:2]
                found["next"] = {"recap": str(nxt.get("recap") or "")[:400], "ask": str(nxt.get("ask") or "")[:200], "options": opts}
    text = _BLOCK.sub("", reply or "").rstrip()
    return (text or (reply or "")), found


def add_chapter(sid: str, title: str) -> int:
    title = " ".join(str(title or "").split())[:80]
    if not title:
        raise ValueError("a chapter needs a title")
    if store.get(sid) is None:
        raise KeyError(sid)
    return store.add_message(sid, "system", f"Chapter: {title}", meta={"chapter": title})


def export(sid: str, fmt: str = "md") -> tuple[str, str]:
    """(content, media type)."""
    s = store.get(sid)
    if s is None:
        raise KeyError(sid)
    msgs = store.messages(sid)
    if fmt == "json":
        return json.dumps({"session": s, "messages": msgs}, indent=1, default=str), "application/json"
    lines = [f"# {s['title'] or 'Local Chat session'}", "", f"Model: {s['model'] or '-'} · mode: {s['mode']} · workspace: "
             f"{s['workspace'] or '-'}" + (f" · branch: {s['worktree']['branch']}" if s.get("worktree") else ""), ""]
    for m in msgs:
        meta = m.get("meta") or {}
        if meta.get("chapter"):
            lines += [f"## {meta['chapter']}", ""]
            continue
        who = {"user": "You", "assistant": "Assistant", "system": "ABP", "error": "Error"}[m["role"]]
        tag = " (reverted)" if m.get("reverted") else ""
        lines += [f"**{who}**{tag}:", "", m["text"], ""]
    return "\n".join(lines), "text/markdown"


def auto_archive(days: Optional[float] = None) -> list[str]:
    from bot import localchat
    from bot.localchat import chat, worktrees

    if days is None:
        try:
            days = float(localchat.settings().get("auto_archive_days") or 0)
        except (TypeError, ValueError):
            days = 0
    if not days or days <= 0:
        return []
    cutoff = time.time() - days * 86400
    done = []
    for s in store.list_sessions(limit=500):
        if s["pinned"] or s["status"] in ("working", "needs_you") or s["updated"] > cutoff or chat.is_running(s["id"]):
            continue
        store.update(s["id"], archived=True)
        try:
            worktrees.on_archive(s["id"])
        except worktrees.WorktreeError:
            pass
        store.add_event(s["id"], "auto_archive", days=days)
        done.append(s["id"])
    return done
