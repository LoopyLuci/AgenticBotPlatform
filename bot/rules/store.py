"""Rules as stored data: a name, some source, what it decides, and whether it is on.

Storage is one small table created on first use, exactly the way bot/routines.py and
bot/infra_automation.py store theirs - a rule is configuration a person wrote, so it belongs in the
same database as the rest of the configuration, not in a file somewhere.

`target` is what a rule decides about, and it is the only thing that makes one rule reach one place:

    ""                     a rule nobody has bound to anything yet; testable, but inert
    "routine:<name>"       this routine only runs when the rule answers yes (a condition)
    "hook:<event>"         every hook on this event runs only when the rule answers yes (a filter)
    "hook:<event>:<match>" the same, narrowed to hooks whose matcher is <match>
    "backend"              a route: the first one that answers names the backend that answers
    "model"                a route: the first one that answers names the model that answers

`instance_id` scopes a rule to one bot instance the same way `agent_hooks` does: NULL means every
instance's turns are affected by it. Two rules bound to the same target must both agree - a routine
with two conditions runs only when both say yes, and a hook with two filters runs only when both say
yes. One rule that said no is a decision, not a veto nobody can see.

Saving a rule needs the engine, because its source is compiled before it is written: an installation
that has lost its native module refuses to create or edit one (the API answers 503 with
bot.rules.engine.DISABLED_MESSAGE), while every rule it already has stays readable, listable and
turnable-off. It stops enforcing them rather than losing them.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Optional

from bot import db
from bot.rules.engine import KINDS, RuleError, program

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,47}$")
ROUTE_TARGETS = ("backend", "model")
MAX_DESCRIPTION = 300
# `routine:<name>` names a routine; `hook:<event>[:<matcher>]` names hooks. The value after the colon is
# free text (a routine name, an event, a matcher), so only the prefix is validated here.
TARGET_RE = re.compile(r"^(routine:[\w.-]{1,64}|hook:[\w.]{1,48}(:[\w.*-]{1,48})?|backend|model)?$")


class RuleStoreError(Exception):
    """A rule that cannot be stored. `problem` is the engine's own structured failure when the rule
    did not compile - line, column, and the machine-readable reason - so an API can answer 422 with
    a position instead of a flat sentence."""

    def __init__(self, message: str, problem: Optional[dict] = None) -> None:
        super().__init__(message)
        self.problem = problem


@dataclass
class Rule:
    id: int
    name: str
    source: str
    kind: str
    enabled: bool
    description: str = ""
    target: str = ""
    instance_id: Optional[int] = None
    created_at: float = 0.0
    updated_at: float = 0.0
    # Filled in by the listing, never stored: what happened the last time this rule ran.
    last_error: str = ""
    checks: int = field(default=0)

    def as_dict(self) -> dict:
        out = {"id": self.id, "name": self.name, "source": self.source, "kind": self.kind, "enabled": self.enabled,
               "description": self.description, "target": self.target, "instance_id": self.instance_id,
               "created_at": self.created_at, "updated_at": self.updated_at,
               "last_error": self.last_error, "checks": self.checks}
        return out


def _conn():
    conn = db.get_conn()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS rules (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            description TEXT NOT NULL DEFAULT '',
            kind TEXT NOT NULL,               -- condition | filter | route
            source TEXT NOT NULL,
            target TEXT NOT NULL DEFAULT '',  -- what this rule decides about; see the module docstring
            instance_id INTEGER,              -- NULL = every instance, like agent_hooks
            enabled INTEGER NOT NULL DEFAULT 1,
            last_error TEXT NOT NULL DEFAULT '',
            checks INTEGER NOT NULL DEFAULT 0,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_rules_target ON rules (target, enabled);
    """)
    return conn


def _row(row) -> Rule:
    return Rule(
        id=row["id"], name=row["name"], source=row["source"], kind=row["kind"], enabled=bool(row["enabled"]),
        description=row["description"] or "", target=row["target"] or "", instance_id=row["instance_id"],
        created_at=row["created_at"], updated_at=row["updated_at"], last_error=row["last_error"] or "",
        checks=row["checks"],
    )


def check_name(name: str) -> str:
    name = (name or "").strip().lower()
    if not NAME_RE.match(name):
        raise RuleStoreError("a rule name is lowercase letters, digits, - and _ (at most 48)")
    return name


def check_kind(kind: str) -> str:
    kind = (kind or "").strip().lower()
    if kind not in KINDS:
        raise RuleStoreError(f"a rule's kind is {' or '.join(KINDS)}")
    return kind


def check_target(target: str) -> str:
    target = (target or "").strip()
    if not TARGET_RE.match(target):
        raise RuleStoreError("a rule's target is routine:<name>, hook:<event>, hook:<event>:<matcher>, "
                             "backend, model, or empty")
    return target


def check_source(source: str, kind: str) -> str:
    """A rule is compiled before it is stored. A rule that does not compile is not a rule yet, and
    finding that out on the page rather than on the next turn is the entire point."""
    source = (source or "").strip()
    if not source:
        raise RuleStoreError("the rule is empty")
    try:
        program(source, kind)                     # raises RuleError, carrying the position
    except RuleError as exc:
        raise RuleStoreError(f"{exc} ({exc.where})", problem=exc.as_dict()) from exc
    return source


def create(name: str, source: str, kind: str, *, description: str = "", target: str = "",
           instance_id: Optional[int] = None, enabled: bool = True) -> Rule:
    name = check_name(name)
    kind = check_kind(kind)
    target = check_target(target)
    if target in ("backend", "model") and kind != "route":
        raise RuleStoreError(f"only a route can decide the {target}; this one is a {kind}")
    if target.startswith(("routine:", "hook:")) and kind == "route":
        raise RuleStoreError("a route answers which target to use, not whether to act; this target needs a condition or a filter")
    source = check_source(source, kind)
    now = time.time()
    with db._lock:
        conn = _conn()
        try:
            cur = conn.execute(
                "INSERT INTO rules (name, description, kind, source, target, instance_id, enabled, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (name, description[:MAX_DESCRIPTION], kind, source, target, instance_id, int(enabled), now, now))
            conn.commit()
        except Exception as exc:  # sqlite3.IntegrityError on a duplicate name
            raise RuleStoreError(f"could not save the rule: {exc}") from exc
        rule_id = cur.lastrowid
    return get(rule_id)


def update(rule_id: int, **changes) -> Rule:
    """Change one rule. Everything is checked before anything is written, so a rule that would not
    compile leaves the stored one exactly as it was."""
    current = get(rule_id)
    if current is None:
        raise RuleStoreError(f"no rule with id {rule_id}")
    name = check_name(changes["name"]) if "name" in changes else current.name
    kind = check_kind(changes["kind"]) if "kind" in changes else current.kind
    target = check_target(changes["target"]) if "target" in changes else current.target
    source = check_source(changes["source"], kind) if "source" in changes else current.source
    if (target in ("backend", "model") and kind != "route") or (target.startswith(("routine:", "hook:")) and kind == "route"):
        raise RuleStoreError(f"a {kind} cannot decide {target or 'nothing'}")
    description = (changes.get("description", current.description) or "")[:MAX_DESCRIPTION]
    instance_id = changes.get("instance_id", current.instance_id)
    enabled = int(bool(changes.get("enabled", current.enabled)))
    with db._lock:
        conn = _conn()
        try:
            conn.execute("UPDATE rules SET name=?, description=?, kind=?, source=?, target=?, instance_id=?, "
                         "enabled=?, last_error='', updated_at=? WHERE id=?",
                         (name, description, kind, source, target, instance_id, enabled, time.time(), rule_id))
            conn.commit()
        except Exception as exc:  # a duplicate name
            raise RuleStoreError(f"could not save the rule: {exc}") from exc
    return get(rule_id)


def get(rule_id: int) -> Optional[Rule]:
    row = _conn().execute("SELECT * FROM rules WHERE id=?", (rule_id,)).fetchone()
    return _row(row) if row else None


def get_by_name(name: str) -> Optional[Rule]:
    row = _conn().execute("SELECT * FROM rules WHERE name=?", (check_name(name),)).fetchone()
    return _row(row) if row else None


def listing(kind: Optional[str] = None, *, target: Optional[str] = None,
            instance_id: Optional[int] = None, enabled_only: bool = False) -> list[Rule]:
    """Every rule, ordered by name, narrowed by whatever the caller knows."""
    sql = "SELECT * FROM rules WHERE 1=1"
    args: list = []
    if kind:
        sql += " AND kind=?"
        args.append(check_kind(kind))
    if target is not None:
        sql += " AND target=?"
        args.append(target)
    if instance_id is not None:
        sql += " AND (instance_id IS NULL OR instance_id=?)"
        args.append(instance_id)
    if enabled_only:
        sql += " AND enabled=1"
    sql += " ORDER BY name"
    return [_row(r) for r in _conn().execute(sql, args).fetchall()]


def set_enabled(rule_id: int, enabled: bool) -> Rule:
    with db._lock:
        conn = _conn()
        conn.execute("UPDATE rules SET enabled=?, updated_at=? WHERE id=?", (int(enabled), time.time(), rule_id))
        conn.commit()
    rule = get(rule_id)
    if rule is None:
        raise RuleStoreError(f"no rule with id {rule_id}")
    return rule


def delete(rule_id: int) -> Rule:
    rule = get(rule_id)
    if rule is None:
        raise RuleStoreError(f"no rule with id {rule_id}")
    with db._lock:
        conn = _conn()
        conn.execute("DELETE FROM rules WHERE id=?", (rule_id,))
        conn.commit()
    return rule


def note_result(rule_id: int, *, error: str = "") -> None:
    """Record that this rule just ran, and what it said if anything. Best effort: a rule must never
    fail because its own bookkeeping did."""
    try:
        with db._lock:
            conn = db.get_conn()
            conn.execute("UPDATE rules SET checks=checks+1, last_error=? WHERE id=?", (error[:300], rule_id))
            conn.commit()
    except Exception:  # noqa: BLE001
        pass


def enabled_count(kind: str, instance_id: Optional[int] = None) -> int:
    """How many enabled rules of one kind could apply to this instance. Every integration point asks
    this before it builds a context or runs anything, so an install that has never written a rule
    pays one indexed lookup per turn and nothing else - which is what keeps "no rules means exactly
    the old behaviour" true in practice as well as in principle."""
    sql = "SELECT COUNT(*) FROM rules WHERE kind=? AND enabled=1"
    args: list = [check_kind(kind)]
    if instance_id is not None:
        sql += " AND (instance_id IS NULL OR instance_id=?)"
        args.append(instance_id)
    return int(_conn().execute(sql, args).fetchone()[0])


def bound(target: str, kind: str, instance_id: Optional[int] = None) -> list[Rule]:
    """Every enabled rule of one kind bound to one target, for one instance. This is what every
    integration point asks for, and the answer is a list because two rules may both apply."""
    sql = "SELECT * FROM rules WHERE target=? AND kind=? AND enabled=1"
    args: list = [target, kind]
    if instance_id is not None:
        sql += " AND (instance_id IS NULL OR instance_id=?)"
        args.append(instance_id)
    sql += " ORDER BY id"
    return [_row(r) for r in _conn().execute(sql, args).fetchall()]


def history(rule_id: int, limit: int = 50) -> list[dict]:
    """Rules keep one counter and one last error rather than a run log: a rule is consulted on
    every turn, and a table that grows without bound to answer "has this ever failed" is not worth
    what it costs."""
    rule = get(rule_id)
    if rule is None:
        raise RuleStoreError(f"no rule with id {rule_id}")
    return [{"checks": rule.checks, "last_error": rule.last_error, "updated_at": rule.updated_at}]


def describe(rule: Rule) -> dict:
    """The rule plus whether it can actually run here, and why not when it cannot."""
    from bot.rules import engine

    out = rule.as_dict()
    out["engine"] = engine.backend()
    if engine.available():
        try:
            program(rule.source, rule.kind)
            out["usable"] = True
            out["problem"] = ""
        except engine.RuleError as exc:
            out["usable"] = False
            out["problem"] = f"{exc} ({exc.where})"
    else:
        out["usable"] = False
        out["problem"] = engine.DISABLED_MESSAGE
    return out

