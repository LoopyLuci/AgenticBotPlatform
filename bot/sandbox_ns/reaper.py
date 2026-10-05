"""What the last run of ABP left behind, stopped before this one starts.

A cell that was never closed - the server was killed, the laptop slept, an update replaced the
build - is still running: a `llama-server` holding 8 GB, a dev server on a port, a build's
compiler. Its job object died with the process that held it, so nothing is going to clean it up
by itself. At start-up the registry's `live.json` says what was alive; this module kills it.

**Both a pid and a create time, or nothing.** Windows hands the same pid out again quickly, so
a record whose pid now belongs to a process created *after* the record was written is a
completely different process and is never touched. Comparing only the pid is how a reaper like
this kills somebody's editor.

**And only another run whose owner is gone.** Two ABPs can share one machine (a live install and
a developer's checkout), and they share one `live.json`. A run whose owning pid is still alive is
somebody's running ABP, not a leftover: it is skipped whole, process by process.

**A leftover is a whole tree, not one pid.** A run recorded the processes its spawns started as
well as the spawns themselves (registry.py's module docstring), so a leftover is reaped process by
process rather than by walking down from a launcher that may itself be gone - and each of them is
still matched on pid *and* create time, so the tree is only ever this run's own.

`persistent` entries (daemons - a module hub, the Hermes bridge) are recorded but not killed:
they are supposed to outlive ABP, and the code that owns them is what stops them. Everything
else is a leak by definition.
"""

from __future__ import annotations

import json
import os
from typing import Optional

#: How far apart two readings of the same process's create time may be and still be the same
#: process. It is the same kernel counter read twice, so it should be exact; the slack only absorbs
#: a rounding difference, never a genuinely different process. It is the registry's, because that is
#: where the record it is compared against is written - reaping is the same identity question.
from bot.sandbox_ns.registry import CREATE_TIME_SLACK_S


def _runs(path=None, *, run_id: Optional[str] = None) -> dict:
    """The runs in the state file, minus this one, minus any whose owning pid is still alive."""
    from bot.sandbox_ns import registry as registry_mod

    state = registry_mod._read_state(path or registry_mod.registry.path)
    runs = state.get("runs") if isinstance(state.get("runs"), dict) else {}
    out = {}
    for run, entry in runs.items():
        if run_id is not None and run == run_id:
            continue
        owner = int((entry or {}).get("pid") or 0)
        if owner > 0 and _alive(owner):
            continue                       # another ABP is running right now: not ours to reap
        out[run] = entry or {}
    return out


def leftover_records(path=None, *, run_id: Optional[str] = None) -> list[dict]:
    """Every live process other (dead) runs recorded, as plain dicts."""
    out = []
    for run, entry in _runs(path, run_id=run_id).items():
        for row in entry.get("processes") or []:
            out.append({**row, "run": run})
    return out


def reap(path=None, *, run_id: Optional[str] = None, log=None) -> dict:
    """Kill what a previous run left running. Returns what happened, so a caller can log or
    show it: `killed` (with each pid and what it was), `kept` (persistent daemons), `gone`
    (already dead) and `reused` (the pid belongs to a different, newer process - never
    touched, and that is the whole point of matching the create time).

    A run whose owning process is still alive is not in any of those: it belongs to another ABP."""
    report = {"killed": [], "kept": [], "gone": [], "reused": []}
    dead_runs = _runs(path, run_id=run_id)
    for row in leftover_records(path, run_id=run_id):
        if row.get("persistent"):
            report["kept"].append(row)
            continue
        outcome = _reap_one(row)
        report[outcome].append(row)
        if log and outcome in ("killed", "reused"):
            log(f"sandbox-ns: {outcome} pid {row.get('pid')} ({' '.join(row.get('argv') or [])[:120]})")
    _prune(path, [run for run, entry in dead_runs.items()
                  if not any(p.get("persistent") for p in (entry.get("processes") or []))])
    return report


def _prune(path, runs: list) -> None:
    """Forget runs that were dead before we got here, so the file does not grow a line per boot
    forever. A run with a live daemon in it is kept: its daemon is still recorded as alive."""
    from bot.sandbox_ns import registry as registry_mod

    if not runs:
        return
    target = path or registry_mod.registry.path
    try:
        state = registry_mod._read_state(target)
        remaining = {run: entry for run, entry in (state.get("runs") or {}).items() if run not in set(runs)}
        state["runs"] = remaining
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(state, indent=1), encoding="utf-8")
        os.replace(tmp, target)
    except OSError:
        pass


def _reap_one(row: dict) -> str:
    from bot.sandbox_ns import registry as registry_mod

    pid = int(row.get("pid") or 0)
    if pid <= 0:
        return "gone"
    if not _same_process(pid, float(row.get("create_time") or 0.0)):
        # Either it is gone, or the pid was handed to a different process. Which of the two
        # only decides which bucket it goes in - in neither case is anything killed. (A pid we
        # may not even look at lands in "reused", which is also nothing being killed.)
        return "gone" if not _alive(pid) else "reused"
    _kill(pid)
    registry_mod.registry.event("reap", pid=pid, cell=row.get("cell", ""),
                                detail=f"left by run {row.get('run', '?')}: {' '.join(row.get('argv') or [])[:200]}")
    return "killed"


def _alive(pid: int) -> bool:
    try:
        import psutil

        return psutil.pid_exists(pid) and psutil.Process(pid).is_running()
    except ImportError:  # pragma: no cover - psutil is a hard dependency of ABP
        return False
    except Exception:  # noqa: BLE001 - a zombie or a permission problem: treat as gone
        return False


def _same_process(pid: int, create_time: float) -> bool:
    """True only if this pid is the same process that was recorded. A record with no create
    time (0.0, e.g. written by an ABP too old to record one) never matches: better a leftover
    nobody reaps than somebody else's process killed by mistake."""
    if create_time <= 0:
        return False
    try:
        import psutil

        return abs(psutil.Process(pid).create_time() - create_time) <= CREATE_TIME_SLACK_S
    except Exception:  # noqa: BLE001 - gone, or not ours to look at
        return False


def _kill(pid: int) -> None:
    """The tree, children first, then the process - cell.kill_tree(), the same implementation a
    cell's own close uses, so "stop this and everything it started" means one thing here too."""
    from bot.sandbox_ns.cell import kill_tree

    kill_tree(pid)
