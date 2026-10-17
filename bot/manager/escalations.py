"""The escalation queue: one list of everything that needs a human (or Claude) decision.

Sources: conductor escalations (two review rejections, a blocked task) with their context; board tasks in `blocked` or `review`
the conductor does not already list; push-queue entries awaiting approval; a capacity override in effect; tasks that failed
repeatedly. Every item carries only what is needed to decide - the question, the failing check, the file:line, and a diff excerpt
capped at DIFF_CAP characters. Raw logs and full diffs are never included.
"""
from __future__ import annotations

import time
from typing import Any, Optional

DIFF_CAP = 1500
FAIL_ATTEMPTS = 2          # a task that failed this many times is a repeated failure


def _clip(text: Any, n: int) -> str:
    s = " ".join(str(text or "").split()) if n < 400 else str(text or "")
    return s if len(s) <= n else s[: n - 1] + "..."


PREFIX = "abp/swarm-conductor-"


def _plan_of(board: str) -> str:
    return board[len(PREFIX):] if board.startswith(PREFIX) else board


def _where(report: Optional[dict]) -> str:
    for f in (report or {}).get("findings") or []:
        if isinstance(f, dict) and f.get("file"):
            return f"{f['file']}:{f['line']}" if f.get("line") else str(f["file"])
    return ""


def _conductor_items(now: float) -> list[dict]:
    from bot.conductor import store

    out = []
    for e in store.list_escalations(True, 50):
        data = e.get("data") or {}
        ctx = data.get("blocked") or {}
        out.append({
            "id": f"esc:{e['id']}", "kind": ctx.get("kind") or "rejections", "title": _clip(data.get("title") or e["task_key"], 100),
            "plan": e["plan_id"], "task": e["task_key"], "age_s": int(now - e["created"]),
            "question": _clip(ctx.get("question") or e["reason"], 300),
            "failing_check": _clip(ctx.get("failing_check") or e["reason"], 300),
            "where": _where(data.get("report")), "diff_excerpt": str(ctx.get("diff_excerpt") or "")[:DIFF_CAP],
            "branch": data.get("branch", ""),
            "decide": f"abp conductor unblock {e['plan_id']} {e['task_key']} --by <who>"})
    return out


def _board_items(now: float, listed: set[tuple[str, str]]) -> list[dict]:
    try:
        from bot import coordination

        board = coordination.get_board()
        blocked = board.list_tasks(state="blocked", limit=200)
        review = board.list_tasks(state="review", limit=200)
        failed = board.list_tasks(state="failed", limit=200)
    except Exception:  # noqa: BLE001 - a board that cannot answer must not hide the other sources
        return []
    out = []
    for t, kind in [(t, "blocked") for t in blocked] + [(t, "review") for t in review]:
        if (_plan_of(t["board"]), str(t.get("key"))) in listed:
            continue                                   # the conductor's own entry for it says more
        out.append({"id": f"task:{t['id']}", "kind": kind, "title": _clip(t["title"], 100), "board": t["board"], "task": t.get("key") or t["id"],
                    "age_s": int(now - (t.get("updated_at") or 0) / 1000), "question": f"Task is {kind}: what should happen next?",
                    "failing_check": _clip(t.get("note"), 300), "where": "", "diff_excerpt": "",
                    "decide": f"decide on work board task {t['id']}"})
    for t in failed:
        if int(t.get("attempts") or 0) >= FAIL_ATTEMPTS and not str(t.get("note", "")).startswith("ESCALATED") \
                and (_plan_of(t["board"]), str(t.get("key"))) not in listed:
            out.append({"id": f"fail:{t['id']}", "kind": "repeated_failure", "title": _clip(t["title"], 100), "board": t["board"],
                        "task": t.get("key") or t["id"], "age_s": int(now - (t.get("updated_at") or 0) / 1000),
                        "question": f"Failed {t['attempts']} times: retry, change the plan, or drop it?",
                        "failing_check": _clip(t.get("note"), 300), "where": "", "diff_excerpt": "",
                        "decide": f"work board task {t['id']}"})
    return out


def _queue_items(now: float) -> list[dict]:
    from bot.conductor import store

    out = []
    for q in store.list_queue("awaiting_approval", 20):
        flaky = len(q.get("flaky") or [])
        out.append({"id": f"queue:{q['id']}", "kind": "push_approval", "title": _clip(q["branch"], 100), "age_s": int(now - q["created"]),
                    "question": f"Approve pushing {q['branch']} at {q['head_sha'][:10]}? The suite is green"
                                + (f" ({flaky} flaky test(s) re-run)." if flaky else "."),
                    "failing_check": "", "where": "", "diff_excerpt": "",
                    "tasks": len(q.get("merged") or q.get("tasks") or []),
                    "decide": f"abp conductor approve {q['id']} --by <who>   (or reject)"})
    return out


def _capacity_items(now: float) -> list[dict]:
    try:
        from bot.capacity import governor

        st = governor.get_state()
    except Exception:  # noqa: BLE001
        return []
    ov = st.get("override")
    if not ov:
        return []
    until = ov.get("expires_at") or ov.get("until")
    return [{"id": "capacity:override", "kind": "capacity_override", "title": f"capacity pinned to {ov.get('state')}",
             "age_s": int(now - float(ov.get("since") or ov.get("ts") or now)),
             "question": "A manual capacity override is in effect. Keep it" + (" until it lapses" if until else " (it never lapses)") + "?",
             "failing_check": _clip(ov.get("reason"), 200), "where": "", "diff_excerpt": "",
             "decide": "abp capacity clear"}]


def collect(now: Optional[float] = None) -> list[dict]:
    """Every open item, oldest first. Each source is read independently: one that cannot answer is skipped."""
    now = time.time() if now is None else now
    items: list[dict] = []
    listed: set[tuple[str, str]] = set()
    try:
        items += _conductor_items(now)
        listed = {(i["plan"], i["task"]) for i in items}
    except Exception:  # noqa: BLE001
        pass
    for fn in (lambda: _board_items(now, listed), lambda: _queue_items(now), lambda: _capacity_items(now)):
        try:
            items += fn()
        except Exception:  # noqa: BLE001
            pass
    return sorted(items, key=lambda i: -i.get("age_s", 0))


def view(limit: int = 50) -> dict:
    items = collect()
    counts: dict[str, int] = {}
    for i in items:
        counts[i["kind"]] = counts.get(i["kind"], 0) + 1
    return {"count": len(items), "by_kind": counts, "items": items[: max(1, limit)]}
