"""Tool-scoped rules and the person's goals: two small, durable lists every model ABP runs is held to.

Tool rules (per tool: what to do or never do with it)
    priority  critical / high   in every system prompt ("Tool-scoped rules"), so compaction can never drop them
              normal            kept; returned by memory_search and tool_rules()
    captured  from what the person says outright ("never email Sarah", "don't use the shell to delete files": a critical
              rule on the matching tool), and from a tool failing twice or more in one turn (a normal observation)

Goals (what the person is working towards)
    a short list with stable ids, kept in the vault as goals.md (edit it there or through the API); every system
    prompt carries it, so any model knows what the work is for.
"""
from __future__ import annotations

import json
import re
import time
import uuid
from typing import Optional

from bot import db

PRIORITIES = ("critical", "high", "normal")
MAX_GOALS = 12
# What people call a tool -> ABP's tool names (the first that exists is used).
ALIASES = {"email": ["send_email", "gmail_send", "email_send"], "mail": ["send_email"], "shell": ["run_shell"],
           "terminal": ["run_shell"], "command": ["run_shell"], "file": ["write_file", "edit_file"], "files": ["write_file", "edit_file"],
           "git": ["run_shell", "git_diff"], "push": ["run_shell"], "commit": ["run_shell"], "browser": ["browser_navigate"],
           "web": ["web_fetch", "web_search"], "internet": ["web_fetch", "web_search"], "schedule": ["schedule_command"],
           "memory": ["save_memory"], "message": ["delegate_to_instance"], "bot": ["delegate_to_instance"],
           "subagent": ["spawn_subagent"], "docker": ["run_shell"], "delete": ["run_shell", "write_file"]}
_EDICT = re.compile(r"\b(?:never|don't|do not|stop)\s+(?P<rest>.{4,200})", re.I)
_ready_for: Optional[str] = None


def _conn():
    global _ready_for
    c = db.get_conn()
    if _ready_for != str(db.DB_PATH):
        with db._lock:
            c.executescript("""
            CREATE TABLE IF NOT EXISTS mf_tool_rules (id TEXT PRIMARY KEY, tool TEXT NOT NULL, rule TEXT NOT NULL,
                priority TEXT NOT NULL, source TEXT NOT NULL, tags TEXT NOT NULL, created REAL NOT NULL, updated REAL NOT NULL);
            CREATE INDEX IF NOT EXISTS mf_tool_rules_tool ON mf_tool_rules(tool);
            CREATE TABLE IF NOT EXISTS mf_goals (id TEXT PRIMARY KEY, text TEXT NOT NULL, status TEXT NOT NULL,
                created REAL NOT NULL, updated REAL NOT NULL);
            """)
            c.commit()
        _ready_for = str(db.DB_PATH)
    return c


def _known_tools() -> set[str]:
    try:
        from bot.agent_runtime import tools
        return {s["name"] for s in tools.all_tool_schemas()}
    except Exception:  # noqa: BLE001
        return set()


# ---- tool rules ---------------------------------------------------------------------------------------------------- #

def put_rule(tool: str, rule: str, priority: str = "normal", source: str = "programmatic", tags: Optional[list[str]] = None,
             rule_id: str = "") -> dict:
    if priority not in PRIORITIES:
        raise ValueError(f"priority is {', '.join(PRIORITIES)}")
    tool, rule = tool.strip(), rule.strip()
    if not tool or not rule:
        raise ValueError("a rule needs a tool and its text")
    c = _conn()
    now = time.time()
    same = c.execute("SELECT id, created FROM mf_tool_rules WHERE tool=? AND lower(rule)=lower(?)", (tool, rule)).fetchone()
    rid = rule_id or (same[0] if same else uuid.uuid4().hex[:12])
    created = same[1] if same else now
    with db._lock:
        c.execute("INSERT OR REPLACE INTO mf_tool_rules VALUES (?,?,?,?,?,?,?,?)",
                  (rid, tool, rule, priority, source, json.dumps(tags or []), created, now))
        c.commit()
    return get_rule(rid)


def get_rule(rule_id: str) -> dict:
    r = _conn().execute("SELECT * FROM mf_tool_rules WHERE id=?", (rule_id,)).fetchone()
    if not r:
        raise ValueError(f"no tool rule {rule_id!r}")
    return _rule(r)


def _rule(r) -> dict:
    return {"id": r[0], "tool": r[1], "rule": r[2], "priority": r[3], "source": r[4], "tags": json.loads(r[5]),
            "created": r[6], "updated": r[7]}


def list_rules(tool: str = "") -> list[dict]:
    q = "SELECT * FROM mf_tool_rules" + (" WHERE tool=?" if tool else "")
    rows = [_rule(r) for r in _conn().execute(q, (tool,) if tool else ())]
    return sorted(rows, key=lambda r: (PRIORITIES.index(r["priority"]), -r["updated"]))


def delete_rule(rule_id: str) -> bool:
    c = _conn()
    with db._lock:
        n = c.execute("DELETE FROM mf_tool_rules WHERE id=?", (rule_id,)).rowcount
        c.commit()
    return bool(n)


def rules_for_prompt() -> str:
    pinned = [r for r in list_rules() if r["priority"] in ("critical", "high")]
    if not pinned:
        return ""
    lines = ["## Tool-scoped rules (obey these every time you consider the tool)"]
    for r in pinned:
        lines.append(f"- [{r['priority']}] {r['tool']}: {r['rule']}")
    return "\n".join(lines)


def capture_edicts(text: str, tools_used: Optional[list[str]] = None) -> list[dict]:
    """The person's "never / don't / do not / stop ..." sentences, as critical rules on the tool they name (by name or
    alias), else on the first tool used in the turn. Questions are not edicts."""
    known = _known_tools()
    out = []
    for sentence in re.split(r"(?<=[.!?])\s+|\n+", text or ""):
        s = sentence.strip()
        m = _EDICT.search(s)
        if not m or s.endswith("?"):
            continue
        words = re.findall(r"[a-z_]+", s.lower())
        tool = next((w for w in words if w in known), None)
        if not tool:
            for w in words:
                tool = next((t for t in ALIASES.get(w, []) if t in known), None)
                if tool:
                    break
        if not tool and tools_used:
            tool = tools_used[0]
        if not tool:
            continue
        rule = s if s.endswith((".", "!")) else s + "."
        out.append(put_rule(tool, rule[0].upper() + rule[1:], "critical", "user_explicit", ["edict"]))
    return out


def note_failures(failures: dict[str, list[str]]) -> list[dict]:
    """A tool that failed twice or more in one turn: a normal-priority observation with what went wrong."""
    out = []
    for tool, errors in failures.items():
        if len(errors) >= 2:
            kinds = "; ".join(sorted({e.split(":")[0][:80] for e in errors})[:3])
            out.append(put_rule(tool, f"Failed {len(errors)} times in one turn ({kinds}); check the inputs before calling it again.",
                                "normal", "post_turn", ["failure"]))
    return out


# ---- goals --------------------------------------------------------------------------------------------------------- #

def goals(include_done: bool = False) -> list[dict]:
    rows = _conn().execute("SELECT id, text, status, created, updated FROM mf_goals ORDER BY created").fetchall()
    out = [{"id": r[0], "text": r[1], "status": r[2], "created": r[3], "updated": r[4]} for r in rows]
    return out if include_done else [g for g in out if g["status"] != "done"]


def put_goal(text: str, status: str = "active", goal_id: str = "") -> dict:
    if status not in ("active", "paused", "done"):
        raise ValueError("status is active, paused or done")
    text = text.strip()
    if not text:
        raise ValueError("a goal needs its text")
    c = _conn()
    if not goal_id and len(goals()) >= MAX_GOALS:
        raise ValueError(f"at most {MAX_GOALS} open goals: finish or remove one first")
    gid = goal_id or "g" + uuid.uuid4().hex[:6]
    now = time.time()
    row = c.execute("SELECT created FROM mf_goals WHERE id=?", (gid,)).fetchone()
    with db._lock:
        c.execute("INSERT OR REPLACE INTO mf_goals VALUES (?,?,?,?,?)", (gid, text, status, row[0] if row else now, now))
        c.commit()
    return next(g for g in goals(True) if g["id"] == gid)


def delete_goal(goal_id: str) -> bool:
    c = _conn()
    with db._lock:
        n = c.execute("DELETE FROM mf_goals WHERE id=?", (goal_id,)).rowcount
        c.commit()
    return bool(n)


def goals_for_prompt() -> str:
    g = goals()
    if not g:
        return ""
    return "The person's goals (what the work is for):\n" + "\n".join(
        f"- {x['text']}" + (" (paused)" if x["status"] == "paused" else "") for x in g)


_GOAL_LINE = re.compile(r"^- \[(?P<done>[ xX])\] (?P<text>.+?)(?:\s*<!--\s*(?P<id>g[0-9a-f]+)(?:\s+(?P<paused>paused))?\s*-->)?\s*$")


def write_goals_file() -> None:
    from bot.memoryfabric import vault
    lines = ["# Goals", "", "One per line. [x] marks a goal done; add `paused` inside the marker to pause one.", ""]
    for g in goals(True):
        lines.append(f"- [{'x' if g['status'] == 'done' else ' '}] {g['text']} <!-- {g['id']}{' paused' if g['status'] == 'paused' else ''} -->")
    path = vault.root() / "goals.md"
    vault._atomic(path, "\n".join(lines) + "\n")
    vault._mark_written(path)


def read_goals_file() -> dict:
    from bot.memoryfabric import vault
    path = vault.root() / "goals.md"
    if not path.exists() or vault._state().get(str(path)) == path.stat().st_mtime:
        return {"changed": 0}
    seen, changed = set(), 0
    current = {g["id"]: g for g in goals(True)}
    for line in path.read_text(encoding="utf-8").splitlines():
        m = _GOAL_LINE.match(line)
        if not m:
            continue
        status = "done" if m.group("done").lower() == "x" else "paused" if m.group("paused") else "active"
        gid = m.group("id") or ""
        cur = current.get(gid)
        if cur and (cur["text"], cur["status"]) == (m.group("text").strip(), status):
            seen.add(gid)
            continue
        g = put_goal(m.group("text"), status, gid if cur else "")
        seen.add(g["id"])
        changed += 1
    for gid in set(current) - seen:
        delete_goal(gid)
        changed += 1
    write_goals_file()
    return {"changed": changed}
