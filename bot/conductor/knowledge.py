"""Knowledge from earlier Factory runs (LF7): what the planner and the workers should know before they start.

Every task outcome the conductor sees (accepted, rejected by the reviewer, escalated, stuck) is recorded as a lesson in
conductor.db, next to the plans and reviews it came from. Before a new run:

    planner_block(repo, goal)            earlier plans with a similar goal and how their tasks ended, the review rules that
                                         keep failing, and the owner's standing rules (memory fabric) -> the planner's context
    task_block(repo, title, detail, files) earlier outcomes of tasks on the same files or with a similar title, failures
                                         first with the reviewer's exact words -> the worker's prompt
    record(...)                          one outcome; also handed to the memory fabric (source "factory") so the memory graph
                                         links runs, files and rules, and to the context vault through navctx as before
    export_dataset(path)                 rejected-then-fixed attempts as JSONL rows (the GEN-D path for later fine-tunes)

It reuses what exists: the reviews and plans in conductor.store, memoryfabric's rules and knowledge base, the vault through
navctx. Nothing here is a new store of its own beyond the lessons table. Best effort throughout: a failure returns "" / {}
and the run behaves as before. `conductor.knowledge.*` switches each part off (the Workspace shows a toggle for each).
"""
from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("bot.conductor.knowledge")

OUTCOMES = ("accepted", "rejected", "escalated", "stuck")
DEFAULTS: dict[str, Any] = {"enabled": True,          # record lessons at all
                            "for_planner": True,      # the planner gets earlier plans, outcomes and failing rules
                            "for_workers": True,      # a worker's prompt gets earlier outcomes of similar tasks
                            "owner_rules": True,      # the memory fabric's standing rules go to the planner too
                            "to_memoryfabric": True,  # lessons also go into the memory fabric's knowledge base (the graph)
                            "planner_chars": 2500, "task_chars": 1500, "max_lessons": 6}
SWITCHES = ("enabled", "for_planner", "for_workers", "owner_rules", "to_memoryfabric")
_SCHEMA = """
CREATE TABLE IF NOT EXISTS lessons (id INTEGER PRIMARY KEY AUTOINCREMENT, repo TEXT NOT NULL DEFAULT '', plan_id TEXT NOT NULL,
    task_key TEXT NOT NULL, goal TEXT NOT NULL DEFAULT '', title TEXT NOT NULL, files TEXT NOT NULL DEFAULT '[]',
    outcome TEXT NOT NULL, attempt INTEGER NOT NULL DEFAULT 1, rules TEXT NOT NULL DEFAULT '[]', text TEXT NOT NULL DEFAULT '',
    created REAL NOT NULL);
CREATE INDEX IF NOT EXISTS lessons_repo ON lessons(repo, created);
"""
_WORD = re.compile(r"[a-z][a-z0-9_]{2,}")
_STOP = frozenset("the and for with that this from into add adds make makes use uses new task test tests file files should when "
                  "not are its their them then than one two all any can will been have has was were".split())


def settings(cfg: Any = None) -> dict[str, Any]:
    """`conductor.knowledge` over its defaults (cfg = the conductor settings dict, or None to read them)."""
    if cfg is None:
        from bot import conductor

        cfg = conductor.settings()
    own = cfg.get("knowledge") if isinstance(cfg, dict) else None
    return {**DEFAULTS, **(own if isinstance(own, dict) else {})}


def _db():
    from bot.conductor import store

    return store._db()


def _ensure(conn) -> None:
    conn.executescript(_SCHEMA)


def words(text: str) -> set[str]:
    return {w for w in _WORD.findall(str(text or "").lower()) if w not in _STOP}


def _similar(a: set[str], b: set[str]) -> float:
    return len(a & b) / max(1, min(len(a), len(b))) if a and b else 0.0


# ------------------------------------------------------------------ record
def record(repo: str, plan_id: str, task_key: str, title: str, outcome: str, *, goal: str = "", files: Optional[list] = None,
           attempt: int = 1, rules: Optional[list] = None, text: str = "", cfg: Any = None) -> int:
    """One task outcome. Returns the lesson id (0 when recording is off or failed)."""
    s = settings(cfg)
    if not s["enabled"] or outcome not in OUTCOMES:
        return 0
    try:
        with _db() as c:
            _ensure(c)
            cur = c.execute("INSERT INTO lessons(repo, plan_id, task_key, goal, title, files, outcome, attempt, rules, text, created) "
                            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                            (repo, plan_id, task_key, str(goal)[:500], str(title)[:300], json.dumps(list(files or [])[:30]), outcome,
                             int(attempt), json.dumps(sorted({str(r) for r in rules or [] if r})), str(text)[:2000], time.time()))
            lid = int(cur.lastrowid or 0)
    except Exception as exc:  # noqa: BLE001 - knowledge never breaks a run
        logger.debug("lesson not recorded: %s", exc)
        return 0
    if s["to_memoryfabric"]:
        _to_fabric(lid, repo, title, outcome, files or [], text)
    return lid


def _to_fabric(lid: int, repo: str, title: str, outcome: str, files: list, text: str) -> None:
    try:
        from bot.memoryfabric import knowledge as kb

        body = f"Factory task '{title}' in {repo}: {outcome}.\nFiles: {', '.join(map(str, files)) or '-'}\n{text}"
        kb.ingest("factory", f"lesson-{lid}", f"Factory: {title}"[:200], body, kind="factory_lesson",
                  tags=["factory", outcome, repo])
    except Exception as exc:  # noqa: BLE001
        logger.debug("memory fabric ingest skipped: %s", exc)


def lessons(repo: str = "", limit: int = 200, outcome: str = "") -> list[dict]:
    try:
        with _db() as c:
            _ensure(c)
            q, args = "SELECT * FROM lessons WHERE 1=1", []
            if repo:
                q, args = q + " AND repo=?", [*args, repo]
            if outcome:
                q, args = q + " AND outcome=?", [*args, outcome]
            rows = c.execute(q + " ORDER BY id DESC LIMIT ?", (*args, int(limit))).fetchall()
    except Exception:  # noqa: BLE001
        return []
    return [{**dict(r), "files": json.loads(r["files"] or "[]"), "rules": json.loads(r["rules"] or "[]")} for r in rows]


def stats(repo: str = "") -> dict:
    rows = lessons(repo, limit=5000)
    by: dict[str, int] = {o: 0 for o in OUTCOMES}
    for r in rows:
        by[r["outcome"]] = by.get(r["outcome"], 0) + 1
    return {"lessons": len(rows), "by_outcome": by, "plans": len({r["plan_id"] for r in rows}),
            "failing_rules": failing_rules(rows)[:8]}


def failing_rules(rows: list[dict], min_count: int = 2) -> list[dict]:
    """Review rules that rejected tasks more than once: the same mistake the models keep making."""
    counts: dict[str, int] = {}
    for r in rows:
        if r["outcome"] != "accepted":
            for rule in r["rules"]:
                counts[rule] = counts.get(rule, 0) + 1
    return [{"rule": k, "count": v} for k, v in sorted(counts.items(), key=lambda kv: -kv[1]) if v >= min_count]


# ------------------------------------------------------------------ recall
def related(repo: str, text: str, files: Optional[list] = None, *, limit: int = 6, exclude_plan: str = "") -> list[dict]:
    """Earlier lessons for a task: same files score highest, then title/goal word overlap; failures before successes."""
    want, fset = words(text), {str(f) for f in files or []}
    scored = []
    for r in lessons(repo, limit=1000):
        if exclude_plan and r["plan_id"] == exclude_plan:
            continue
        shared = fset & set(r["files"])
        sim = _similar(want, words(f"{r['title']} {r['goal']}"))
        score = (1.0 if shared else 0.0) + sim
        if score >= 0.5:
            scored.append((score + (0.3 if r["outcome"] != "accepted" else 0.0), r))
    scored.sort(key=lambda sr: (-sr[0], -sr[1]["id"]))
    return [r for _, r in scored[:limit]]


def _line(r: dict, cap: int = 400) -> str:
    head = f"- [{r['outcome']}] {r['title']}" + (f" (files: {', '.join(r['files'][:4])})" if r["files"] else "")
    body = " ".join(str(r["text"] or "").split())[:cap]
    return head + (f": {body}" if body else "")


def task_block(repo: str, title: str, detail: str = "", files: Optional[list] = None, *, plan_id: str = "", cfg: Any = None) -> str:
    s = settings(cfg)
    if not (s["enabled"] and s["for_workers"]):
        return ""
    rows = related(repo, f"{title} {str(detail)[:400]}", files, limit=int(s["max_lessons"]), exclude_plan=plan_id)
    if not rows:
        return ""
    out = "What earlier Factory runs learned about similar tasks (avoid the failures, reuse what was accepted):\n"
    for r in rows:
        line = _line(r) + "\n"
        if len(out) + len(line) > int(s["task_chars"]):
            break
        out += line
    return out.strip()


def planner_block(repo: str, goal: str, cfg: Any = None) -> str:
    s = settings(cfg)
    if not (s["enabled"] and s["for_planner"]):
        return ""
    parts: list[str] = []
    rows = lessons(repo, limit=1000)
    want = words(goal)
    plans: dict[str, list[dict]] = {}
    for r in rows:
        if _similar(want, words(r["goal"] or r["title"])) >= 0.4:
            plans.setdefault(r["plan_id"], []).append(r)
    if plans:
        lines = ["Earlier Factory plans for similar goals and how their tasks ended:"]
        for pid, rs in list(plans.items())[:4]:
            ends = ", ".join(f"{x['title'][:60]} = {x['outcome']}" for x in rs[:5])
            lines.append(f"- {rs[0]['goal'][:120] or pid}: {ends}")
        parts.append("\n".join(lines))
    bad = failing_rules(rows)
    if bad:
        parts.append("Review rules earlier tasks kept failing (plan tasks that respect them): " +
                     ", ".join(f"{b['rule']} ({b['count']}x)" for b in bad[:6]))
    if s["owner_rules"]:
        try:
            from bot.memoryfabric import rules as fabric_rules

            owner = fabric_rules.rules_for_prompt().strip()
            if owner:
                parts.append(owner)
        except Exception as exc:  # noqa: BLE001
            logger.debug("owner rules unavailable: %s", exc)
    return "\n\n".join(parts)[: int(s["planner_chars"])].strip()


# ------------------------------------------------------------------ dataset
def export_dataset(path: Path, repo: str = "") -> dict:
    """Each rejected attempt with the reviewer's feedback, and the accepted outcome when the same task later passed: JSONL rows
    {"task", "files", "rejection", "rules", "fixed"} for later fine-tunes of the local coder (native models P3, GEN-D)."""
    rows = sorted(lessons(repo, limit=100000), key=lambda r: r["id"])
    accepted = {(r["plan_id"], r["task_key"]): r for r in rows if r["outcome"] == "accepted"}
    n = 0
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for r in rows:
            if r["outcome"] == "accepted":
                continue
            ok = accepted.get((r["plan_id"], r["task_key"]))
            fh.write(json.dumps({"repo": r["repo"], "task": r["title"], "goal": r["goal"], "files": r["files"], "outcome": r["outcome"],
                                 "rejection": r["text"], "rules": r["rules"], "fixed": ok["text"] if ok else ""}) + "\n")
            n += 1
    return {"path": str(path), "rows": n}
