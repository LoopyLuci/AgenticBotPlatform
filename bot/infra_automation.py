"""Automation rules for containers, VMs and Tailscale: "when X, do Y" and "every N, do Y".

A rule is stored data - a trigger and one action from a fixed menu - never a
script. Actions call the same validated bot/docker_mgr.py, bot/vm_mgr.py and
bot/tailscale_mgr.py functions the API uses, so a rule can do nothing a
person with the dashboard token couldn't, and can't run arbitrary commands
except through the audited, argv-only container exec.

Triggers
  {"type": "interval", "every": "6h"}
  {"type": "watch", "resource": "container", "target": "web", "when": "unhealthy"}
       container when: exited | stopped | running | unhealthy | cpu>80 | mem>90
  {"type": "watch", "resource": "vm", "target": "dev", "when": "off" | "running"}
  {"type": "watch", "resource": "tailscale", "when": "disconnected"}

Actions
  {"type": "container", "target": "web", "action": "restart"}
  {"type": "vm", "backend": "qemu|hyperv|libvirt", "target": "dev", "action": "start"}
  {"type": "stack", "target": "web", "action": "up"}
  {"type": "prune", "kind": "image"}
  {"type": "exec", "target": "web", "command": ["nginx", "-s", "reload"]}
  {"type": "tailscale_up"}
  {"type": "tailscale_prefs", "settings": {"shields_up": true}}
  {"type": "notify", "message": "..."}

A watch rule fires when its condition becomes true, then waits `cooldown_s`
(default 300) before it can fire again, so a crash-looping container is
restarted every few minutes, not every tick.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from typing import Any, Optional

from bot import db, docker_mgr as dk, tailscale_mgr as ts, vm_mgr as vm

logger = logging.getLogger(__name__)
TICK_S = 30
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.\-]{0,63}$")
_WHEN_RE = re.compile(r"^(exited|stopped|running|unhealthy|off|disconnected|(cpu|mem)>\d{1,3})$")


class RuleError(Exception):
    pass


def _conn():
    conn = db.get_conn()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS infra_rules (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            enabled INTEGER NOT NULL DEFAULT 1,
            trigger TEXT NOT NULL,
            action TEXT NOT NULL,
            cooldown_s INTEGER NOT NULL DEFAULT 300,
            last_run REAL,
            last_result TEXT NOT NULL DEFAULT '',
            runs INTEGER NOT NULL DEFAULT 0,
            created_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS infra_rule_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            rule_id INTEGER NOT NULL,
            at REAL NOT NULL,
            ok INTEGER NOT NULL,
            detail TEXT NOT NULL DEFAULT ''
        );
    """)
    return conn


def _interval_s(text: str) -> int:
    m = re.match(r"^(\d+)\s*([smhd])$", str(text).strip().lower())
    if not m:
        raise RuleError("interval must look like 30s, 15m, 6h or 1d")
    secs = int(m.group(1)) * {"s": 1, "m": 60, "h": 3600, "d": 86400}[m.group(2)]
    if secs < 30:
        raise RuleError("interval must be at least 30s")
    return secs


def validate(trigger: dict, action: dict) -> None:
    t = trigger.get("type")
    if t == "interval":
        _interval_s(trigger.get("every", ""))
    elif t == "watch":
        res = trigger.get("resource")
        if res not in ("container", "vm", "tailscale"):
            raise RuleError("watch resource must be container, vm or tailscale")
        if not _WHEN_RE.match(str(trigger.get("when", ""))):
            raise RuleError("unsupported 'when' condition")
        if res != "tailscale" and not trigger.get("target"):
            raise RuleError("watch needs a target")
    else:
        raise RuleError("trigger type must be interval or watch")
    a = action.get("type")
    if a not in ("container", "vm", "stack", "prune", "exec", "tailscale_up", "tailscale_prefs", "notify"):
        raise RuleError("unsupported action type")
    if a in ("container", "vm", "stack", "exec") and not action.get("target"):
        raise RuleError("action needs a target")
    if a == "vm" and action.get("backend", "qemu") not in vm.BACKENDS:
        raise RuleError("bad VM backend")
    if a == "exec" and not (isinstance(action.get("command"), list) and action["command"]):
        raise RuleError("exec needs a command list")
    if a == "tailscale_prefs":
        unknown = set(action.get("settings") or {}) - set(ts.PREFS)
        if unknown or not action.get("settings"):
            raise RuleError(f"unknown Tailscale settings: {sorted(unknown)}")


def create(name: str, trigger: dict, action: dict, cooldown_s: int = 300, enabled: bool = True) -> dict:
    if not _NAME_RE.match(name):
        raise RuleError("rule name: letters, digits, space, . _ -")
    validate(trigger, action)
    conn = _conn()
    try:
        cur = conn.execute(
            "INSERT INTO infra_rules (name, enabled, trigger, action, cooldown_s, created_at) VALUES (?,?,?,?,?,?)",
            (name, int(enabled), json.dumps(trigger), json.dumps(action), max(30, int(cooldown_s)), time.time()))
        conn.commit()
    except Exception as exc:  # sqlite3.IntegrityError on a duplicate name
        raise RuleError(f"could not create rule: {exc}") from exc
    return get(cur.lastrowid)


def _row(r) -> dict:
    d = dict(r)
    d["trigger"], d["action"] = json.loads(d["trigger"]), json.loads(d["action"])
    d["enabled"] = bool(d["enabled"])
    return d


def get(rule_id: int) -> dict:
    r = _conn().execute("SELECT * FROM infra_rules WHERE id=?", (rule_id,)).fetchone()
    if not r:
        raise RuleError("no such rule")
    return _row(r)


def list_rules() -> list[dict]:
    return [_row(r) for r in _conn().execute("SELECT * FROM infra_rules ORDER BY id")]


def set_enabled(rule_id: int, enabled: bool) -> dict:
    conn = _conn()
    conn.execute("UPDATE infra_rules SET enabled=? WHERE id=?", (int(enabled), rule_id))
    conn.commit()
    return get(rule_id)


def delete(rule_id: int) -> None:
    conn = _conn()
    conn.execute("DELETE FROM infra_rules WHERE id=?", (rule_id,))
    conn.execute("DELETE FROM infra_rule_runs WHERE rule_id=?", (rule_id,))
    conn.commit()


def history(rule_id: int, limit: int = 50) -> list[dict]:
    return [dict(r) for r in _conn().execute(
        "SELECT at, ok, detail FROM infra_rule_runs WHERE rule_id=? ORDER BY id DESC LIMIT ?", (rule_id, limit))]


# ---------------------------------------------------------------- evaluation
def _pct(text: str) -> float:
    try:
        return float(str(text).strip().rstrip("%"))
    except ValueError:
        return 0.0


def condition_met(trigger: dict, now: float, last_run: Optional[float]) -> bool:
    if trigger["type"] == "interval":
        return last_run is None or now - last_run >= _interval_s(trigger["every"])
    res, when, target = trigger["resource"], trigger["when"], trigger.get("target", "")
    if res == "container":
        rows = [c for c in dk.containers(True) if target in (c.get("Names"), c.get("ID"), c.get("ID", "")[:12])]
        if not rows:
            return False
        c = rows[0]
        state, status = (c.get("State") or "").lower(), (c.get("Status") or "").lower()
        if when == "exited":
            return state == "exited"
        if when == "stopped":
            return state != "running"
        if when == "running":
            return state == "running"
        if when == "unhealthy":
            return "(unhealthy)" in status
        if when.startswith(("cpu>", "mem>")):
            s = dk.container_stats(target)
            if not s:
                return False
            key = "CPUPerc" if when.startswith("cpu") else "MemPerc"
            return _pct(s[0].get(key, "0")) > float(when.split(">")[1])
    if res == "vm":
        match = [v for v in vm.qemu_list() if v["name"] == target]
        if match:
            return (match[0].get("state") == "off") == (when == "off") if when in ("off", "running") else False
    if res == "tailscale" and when == "disconnected":
        return ts.status().get("BackendState") != "Running"
    return False


def _do(action: dict) -> str:
    a = action["type"]
    if a == "container":
        return dk.container_action(action["target"], action.get("action", "restart"))["output"]
    if a == "vm":
        backend = action.get("backend", "qemu")
        act = action.get("action", "start")
        if backend == "qemu":
            if act == "start":
                return str(vm.qemu_start(action["target"]))
            if act == "stop":
                return str(vm.qemu_stop(action["target"], bool(action.get("force"))))
            return str(vm.qemu_control(action["target"], act))
        return str((vm.hv_action if backend == "hyperv" else vm.lv_action)(action["target"], act))
    if a == "stack":
        return dk.stack_action(action["target"], action.get("action", "up"))["output"]
    if a == "prune":
        return dk.prune(action.get("kind", "image"))["output"]
    if a == "exec":
        return dk.container_exec(action["target"], action["command"])["output"]
    if a == "tailscale_up":
        return str(ts.up())
    if a == "tailscale_prefs":
        return str(ts.set_prefs(action["settings"]))
    if a == "notify":
        return str(action.get("message", ""))
    raise RuleError("unsupported action")


def run_rule(rule_id: int, *, force: bool = False) -> dict:
    """Evaluate one rule now; executes its action if the trigger holds (or force)."""
    rule = get(rule_id)
    now = time.time()
    if not force:
        if not rule["enabled"]:
            return {"fired": False, "reason": "disabled"}
        if rule["trigger"]["type"] == "watch" and rule["last_run"] and now - rule["last_run"] < rule["cooldown_s"]:
            return {"fired": False, "reason": "cooldown"}
        try:
            if not condition_met(rule["trigger"], now, rule["last_run"]):
                return {"fired": False, "reason": "condition not met"}
        except (dk.DockerError, vm.VMError, ts.TailscaleError) as exc:
            return {"fired": False, "reason": f"could not check: {exc}"}
    ok, detail = True, ""
    try:
        detail = _do(rule["action"])
    except (RuleError, dk.DockerError, vm.VMError, ts.TailscaleError) as exc:
        ok, detail = False, str(exc)
    conn = _conn()
    conn.execute("UPDATE infra_rules SET last_run=?, last_result=?, runs=runs+1 WHERE id=?",
                 (now, ("ok: " if ok else "failed: ") + detail[:300], rule_id))
    conn.execute("INSERT INTO infra_rule_runs (rule_id, at, ok, detail) VALUES (?,?,?,?)",
                 (rule_id, now, int(ok), detail[:1000]))
    conn.commit()
    db.log_audit(actor="automation", action=f"infra_rule_{'ok' if ok else 'failed'}",
                 detail=f"{rule['name']}: {detail[:200]}")
    return {"fired": True, "ok": ok, "detail": detail}


def tick() -> list[dict]:
    fired = []
    for rule in list_rules():
        if rule["enabled"]:
            res = run_rule(rule["id"])
            if res.get("fired"):
                fired.append({"rule": rule["name"], **res})
    return fired


async def run_forever(stop_event: asyncio.Event) -> None:
    while not stop_event.is_set():
        try:
            await asyncio.to_thread(tick)
        except Exception:  # a bad tick must never kill the loop
            logger.exception("infra automation tick failed")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=TICK_S)
        except asyncio.TimeoutError:
            pass
