"""The digest: a capped summary of the factory since the last digest, for a manager that pays per token.

    build(since=None, mark=False)   -> dict   machine-readable; "tokens" is its own estimated size, never above the cap
    to_text(doc)                    -> str    the short human form (also capped)

`since` defaults to the stored last-digest marker. `mark=True` moves the marker to this digest's time and adds its tokens to the
manager's running total; a peek (mark=False) changes nothing. An identical request inside `digest.cache_s` seconds returns the
cached document. No raw logs and no full diffs: titles, reasons and file:line are clipped, diff excerpts are short, and when the
document is still over the cap it is shrunk step by step (excerpts, then list lengths) and says `truncated`.
"""
from __future__ import annotations

import copy
import json
import sqlite3
import threading
import time
from typing import Any, Optional

from bot import manager
from bot.manager import escalations, store

_lock = threading.Lock()
_cache: dict[tuple, tuple[float, dict]] = {}
LIST_CAPS = {"finished": 10, "failed": 8, "events": 8, "integrations": 5}


def _clip(text: Any, n: int) -> str:
    s = " ".join(str(text or "").split())
    return s if len(s) <= n else s[: n - 3] + "..."


def _capacity(since: float) -> tuple[dict, list[str]]:
    try:
        from bot.capacity import events, governor

        st = governor.get_state()
        ov = st.get("override")
        auto = st.get("auto") or {}
        cap = {"state": st.get("state"), "mode": st.get("mode"),
               "why": _clip(auto.get("reason"), 160) if auto.get("state") != "cloud-led" else "",
               "override": ({"state": ov.get("state"), "reason": _clip(ov.get("reason"), 120)} if ov else None)}
        evs = events.recent(0, 200)
        lines = events.digest_lines(0, 200)
        return cap, [ln for ln, e in zip(lines, evs) if e["ts"] > since]
    except Exception:  # noqa: BLE001
        return {"state": "unknown"}, []


def _board(since: float) -> tuple[dict, list[dict], list[dict]]:
    counts: dict[str, int] = {}
    finished: list[dict] = []
    failed: list[dict] = []
    try:
        from bot import coordination

        board = coordination.get_board()
        for _name, b in (board.summary().get("boards") or {}).items():
            for st, n in (b.get("counts") or {}).items():
                counts[st] = counts.get(st, 0) + int(n)
        for t in board.list_tasks(state="done", limit=500):
            if (t.get("updated_at") or 0) / 1000 > since:
                finished.append({"key": t.get("key") or t["id"], "title": _clip(t["title"], 80)})
        for t in board.list_tasks(state="failed", limit=500):
            if (t.get("updated_at") or 0) / 1000 > since:
                failed.append({"key": t.get("key") or t["id"], "title": _clip(t["title"], 60), "why": _clip(t.get("note"), 160),
                               "attempts": t.get("attempts")})
    except Exception:  # noqa: BLE001
        pass
    return {k: v for k, v in counts.items() if v}, finished, failed


def _integrations(since: float) -> list[dict]:
    try:
        from bot.conductor import store as cstore

        return [{"branch": i.get("branch"), "status": i.get("status"),
                 "why": _clip(i.get("note"), 140) if i.get("status") != "green" else "", "failed": len(i.get("failed") or [])}
                for i in cstore.list_integrations(20) if float(i.get("created") or 0) > since]
    except Exception:  # noqa: BLE001
        return []


def factory_tokens(since: float) -> dict:
    """Tokens the factory's own agents spent: the agent traces (llm.call events; local models split out) plus the conductor's
    worker attempts, which run headless and write no trace (all local: the conductor uses no other models)."""
    out = {"llm_calls": 0, "tokens": 0, "local_tokens": 0, "conductor_attempts": 0, "conductor_tokens": 0}
    sources: list[str] = []
    try:
        from bot.agent_runtime import trace
        from bot.capacity import governor

        path = trace.db_path()
        if path.is_file():
            local = tuple(f"{p}/" for p in governor.settings().get("local_providers") or [])
            conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=5)
            try:
                for (raw,) in conn.execute("SELECT data FROM events WHERE kind = 'llm.call' AND ts > ?", (since,)):
                    try:
                        d = json.loads(raw)
                    except ValueError:
                        continue
                    n = int(d.get("tokens") or 0)
                    out["llm_calls"] += 1
                    out["tokens"] += n
                    if str(d.get("model") or "").startswith(local):
                        out["local_tokens"] += n
            finally:
                conn.close()
            sources.append("agent traces")
    except Exception:  # noqa: BLE001
        sources.append("agent traces unavailable")
    try:
        from bot.conductor import store as cstore

        if cstore.db_path().is_file():
            w = cstore.worker_tokens(since)
            out["conductor_attempts"], out["conductor_tokens"] = w["attempts"], w["tokens"]
            out["tokens"] += w["tokens"]
            out["local_tokens"] += w["tokens"]
            sources.append("conductor")
    except Exception:  # noqa: BLE001
        sources.append("conductor unavailable")
    out["source"] = " + ".join(sources) or "none"
    return out


def _size(doc: dict) -> int:
    return manager.est_tokens(json.dumps(doc, separators=(",", ":"), ensure_ascii=False))


def _fit(doc: dict, cap: int) -> dict:
    """Shrink `doc` until its estimate is within `cap` tokens; mark it truncated and say what was dropped."""
    dropped: list[str] = []

    def done() -> bool:
        doc["tokens"] = _size(doc)
        return doc["tokens"] <= cap

    if done():
        return doc
    for it in doc["escalations"]["items"]:                       # 1. diff excerpts first
        if it.pop("diff_excerpt", None) and "diff_excerpt" not in dropped:
            dropped.append("diff_excerpt")
    for step in (4, 2, 1, 0):                                      # 2. then every list, shorter and shorter
        if done():
            break
        for key in ("finished", "failed", "events", "integrations"):
            if len(doc[key]) > step:
                dropped.append(f"{key}>{step}")
                doc[key] = doc[key][:step]
        if len(doc["escalations"]["items"]) > max(step, 1):
            dropped.append(f"escalations>{max(step, 1)}")
            doc["escalations"]["items"] = doc["escalations"]["items"][: max(step, 1)]
    if not done():                                                 # 3. last resort: strings in the items
        for it in doc["escalations"]["items"]:
            for k in ("question", "failing_check", "title"):
                it[k] = _clip(it.get(k), 60)
            it.pop("decide", None)
    if not done():
        doc["escalations"]["items"] = []
        dropped.append("escalation items (counts kept)")
    doc["truncated"] = sorted(set(dropped))
    done()
    return doc


def build(since: Optional[float] = None, *, mark: bool = False, fresh: bool = False, now: Optional[float] = None) -> dict:
    cfg = manager.settings()["digest"]
    cap = max(300, int(cfg["cap_tokens"]))
    mem = store.load()
    base = float(mem["last_digest_ts"]) if since is None else float(since)
    now = time.time() if now is None else now
    key = (round(base, 3), cap)
    with _lock:
        hit = _cache.get(key)
        if hit and not fresh and 0 <= now - hit[0] < float(cfg["cache_s"]):
            doc = copy.deepcopy(hit[1])
            doc["cached"] = True
        else:
            doc = _compose(base, now, cap, int(cfg["diff_chars"]), mem)
            _cache[key] = (now, copy.deepcopy(doc))
            if len(_cache) > 8:
                _cache.pop(min(_cache, key=lambda k: _cache[k][0]), None)
    if mark:
        mem = store.record_delivery(doc["generated"], doc["tokens"])
        doc["accounting"] = _accounting(mem, doc["factory"])
        _cache.clear()
    return doc


def _accounting(mem: dict, factory: dict) -> dict:
    cks = mem.get("checkins") or []
    return {"manager_tokens_total": int(mem.get("tokens_total", 0)), "digests_delivered": int(mem.get("digests", 0)),
            "checkins": int(mem.get("checkin_count", 0)), "last_checkin_tokens": cks[-1]["tokens"] if cks else 0,
            "factory_tokens_since_digest": factory["tokens"], "factory_local_tokens_since_digest": factory["local_tokens"]}


def _compose(since: float, now: float, cap: int, diff_chars: int, mem: dict) -> dict:
    capacity, event_lines = _capacity(since)
    counts, finished, failed = _board(since)
    esc = escalations.view(30)
    for it in esc["items"]:
        it["diff_excerpt"] = (it.get("diff_excerpt") or "")[:diff_chars]
        if not it["diff_excerpt"]:
            it.pop("diff_excerpt")
        for k in ("where", "failing_check", "plan", "board", "branch", "tasks"):
            if not it.get(k):
                it.pop(k, None)
    factory = factory_tokens(since)
    doc: dict[str, Any] = {
        "generated": now, "since": since or None, "capacity": capacity, "board": counts,
        "finished_total": len(finished), "finished": finished[: LIST_CAPS["finished"]],
        "failed_total": len(failed), "failed": failed[: LIST_CAPS["failed"]],
        "integrations": _integrations(since)[: LIST_CAPS["integrations"]], "events": event_lines[-LIST_CAPS["events"]:],
        "escalations": {"count": esc["count"], "by_kind": esc["by_kind"], "items": esc["items"]},
        "factory": factory, "accounting": _accounting(mem, factory), "cap_tokens": cap, "tokens": 0,
    }
    return _fit(doc, cap)


def quiet(doc: dict) -> bool:
    """Nothing happened worth a manager's tokens: nothing finished or failed, no events, no open decisions."""
    return not (doc["finished_total"] or doc["failed_total"] or doc["events"] or doc["integrations"] or doc["escalations"]["count"])


def to_text(doc: dict, max_chars: int = 3500) -> str:
    cap = doc["capacity"]
    since = time.strftime("%m-%d %H:%M", time.localtime(doc["since"])) if doc.get("since") else "the beginning"
    acc, fac = doc["accounting"], doc["factory"]
    lines = [f"factory digest since {since} (~{doc['tokens']} tokens)",
             f"capacity: {cap.get('state')} ({cap.get('mode')})" + (f" - {cap['why']}" if cap.get("why") else "")
             + (f"; override {cap['override']['state']}" if cap.get("override") else ""),
             "board: " + (", ".join(f"{k} {v}" for k, v in doc["board"].items()) or "empty")]
    if doc["finished_total"]:
        lines.append(f"finished {doc['finished_total']}: " + "; ".join(f"{t['key']} {t['title']}" for t in doc["finished"]))
    for t in doc["failed"]:
        lines.append(f"FAILED {t['key']} ({t['attempts']}x): {t['why'] or t['title']}")
    for i in doc["integrations"]:
        lines.append(f"integration {i['branch']}: {i['status']}" + (f" - {i['why']}" if i["why"] else ""))
    lines += [f"event {e}" for e in doc["events"]]
    esc = doc["escalations"]
    if esc["count"]:
        lines.append(f"NEEDS A DECISION ({esc['count']}): " + ", ".join(f"{k} {v}" for k, v in esc["by_kind"].items()))
    for it in esc["items"]:
        lines.append(f"- [{it['id']}] {it['title']}: {it['question']}" + (f" check: {it['failing_check']}" if it.get("failing_check") else "")
                     + (f" at {it['where']}" if it.get("where") else ""))
        if it.get("diff_excerpt"):
            lines.append("    " + it["diff_excerpt"][:300].replace("\n", "\n    "))
        if it.get("decide"):
            lines.append(f"    decide: {it['decide']}")
    lines.append(f"tokens: factory {fac['tokens']} ({fac['local_tokens']} local, {fac['llm_calls']} calls); manager {acc['manager_tokens_total']} "
                 f"total over {acc['digests_delivered']} digests ({acc['checkins']} check-ins)")
    if doc.get("truncated"):
        lines.append("(shortened: " + ", ".join(doc["truncated"]) + ")")
    text = "\n".join(lines)
    return text if len(text) <= max_chars else text[: max_chars - 20] + "\n(text cut)"
