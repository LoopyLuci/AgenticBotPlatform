"""Every process and cell ABP starts is written down: the nervous system's memory.

A process that is not recorded cannot be accounted for, and a process that is not accounted
for is how a machine ends up with three orphaned `llama-server`s and no idea who started them.
So one registry records each spawn (pid, the process's own create time, its argv with
secret-looking arguments masked, cwd, which component asked for it, the cell and policy
under it), notices when it exits, and keeps `data/sandbox_ns/live.json` up to date so the
*next* ABP run can find and stop what this one left behind (reaper.py).

**pid alone is not an identity.** Windows reuses pids freely, so every record carries the
process's create time and both must match before anything is killed - a record whose pid
belongs to a different, newer process is never touched.

**live.json holds several runs.** ABP is more than one process (the server, a training
worker, an MCP server), and each writes only its own run's entries under its own key,
merging what is already there. So a worker exiting cannot erase the server's bookkeeping,
and two ABP instances on one machine (a live one and a developer's) do not fight over one
file.

A sampler thread (psutil, every few seconds, only while something is registered) records CPU
and memory per cell, which is what reflexes.py reacts to.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
import uuid
from collections import deque
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

RUN_ID = f"{os.getpid()}-{uuid.uuid4().hex[:8]}"
MAX_EVENTS = 500
MAX_RECORDS = 400
SAMPLE_INTERVAL_S = 3.0

_SECRET_FLAG = re.compile(r"(?i)^--?[a-z0-9_.-]*(token|secret|passw|credential|api[_-]?key|access[_-]?key)[a-z0-9_.-]*")
_SECRET_ASSIGN = re.compile(r"(?i)^([a-z0-9_.-]*(token|secret|passw|credential|api[_-]?key)[a-z0-9_.-]*)=(.*)$")
MASKED = "[secret]"


@dataclass
class Record:
    """One process ABP started. `create_time` is the OS's own value for it (seconds since
    the epoch, as psutil reports it), because that is what makes a pid unambiguous."""
    pid: int
    create_time: float
    argv: list = field(default_factory=list)
    cwd: str = ""
    owner: str = ""
    name: str = ""
    cell: str = ""
    policy: str = ""
    persistent: bool = False
    started: float = field(default_factory=time.time)
    exited: Optional[float] = None
    exit_code: Optional[int] = None

    def alive(self) -> bool:
        return self.exited is None

    def summary(self) -> dict:
        out = asdict(self)
        out["alive"] = self.alive()
        return out


class Registry:
    """The live table. One per process; `registry` below is the singleton everything uses."""

    def __init__(self, path: Optional[Path] = None, run_id: str = RUN_ID):
        self.run_id = run_id
        self._lock = threading.RLock()
        self._records: dict[int, Record] = {}
        self._cells: dict[str, Any] = {}
        self._events: deque = deque(maxlen=MAX_EVENTS)
        self._path = path
        self._samples: dict[str, dict] = {}
        self._sampler: Optional[threading.Thread] = None
        self._stop = threading.Event()

    # ---- where the state file lives -------------------------------------------------------
    @property
    def path(self) -> Path:
        """`<data>/sandbox_ns/live.json`, where `<data>` is ABP's own state root - ABP_HOME
        moves it, bot/envfile.py decides (PROJECT_ROOT, not CODE_ROOT)."""
        if self._path is None:
            from bot.envfile import PROJECT_ROOT

            self._path = PROJECT_ROOT / "data" / "sandbox_ns" / "live.json"
        return self._path

    # ---- events -----------------------------------------------------------------------------
    def event(self, kind: str, *, pid: Optional[int] = None, cell: str = "", detail: str = "") -> dict:
        """One line in the ring buffer. Events are cheap and never touch the disk: the state
        file is written when a process appears or disappears, not when something is noted."""
        row = {"ts": round(time.time(), 3), "kind": kind, "pid": pid, "cell": cell, "detail": detail[:400]}
        with self._lock:
            self._events.append(row)
        return row

    def events(self, limit: int = 100, kind: Optional[str] = None) -> list[dict]:
        with self._lock:
            rows = [e for e in self._events if kind is None or e["kind"] == kind]
        return list(reversed(rows[-limit:]))

    # ---- cells ------------------------------------------------------------------------------
    def add_cell(self, cell) -> None:
        with self._lock:
            self._cells[cell.id] = cell
            self._samples.setdefault(cell.id, {"pid": 0, "rss_mb": 0.0, "cpu_percent": 0.0, "at": 0.0})
        self.start_sampler()

    def remove_cell(self, cell) -> None:
        with self._lock:
            self._cells.pop(cell.id, None)
            self._samples.pop(cell.id, None)
            if not self._cells and not self._records:
                self.stop_sampler()

    def cells(self) -> list:
        with self._lock:
            return list(self._cells.values())

    def close_cells(self, *, reason: str = "ABP is closing its cells", keep_persistent: bool = True) -> int:
        """Close every cell this process still holds - the last step of a clean shutdown, after
        each owner has had its chance to stop its own children. Non-persistent cells take their
        processes down; a persistent one (a daemon meant to outlive ABP) is only released.
        Returns how many were closed."""
        closed = 0
        for cell in self.cells():
            if keep_persistent and cell.policy.persistent:
                cell.close()
            else:
                cell.kill(reason)
            closed += 1
        return closed

    # ---- processes --------------------------------------------------------------------------
    def record(self, *, pid: int, argv: Iterable, cwd: str = "", owner: str = "", name: str = "",
               cell=None, policy=None, create_time: Optional[float] = None) -> Record:
        row = Record(pid=int(pid), create_time=float(create_time if create_time is not None else create_time_of(pid)),
                     argv=list(argv), cwd=str(cwd or ""), owner=owner, name=name or str(argv[0] if argv else pid),
                     cell=getattr(cell, "id", "") or "", policy=getattr(policy, "name", "") or "",
                     persistent=bool(getattr(policy, "persistent", False)))
        with self._lock:
            self._records[row.pid] = row
            if len(self._records) > MAX_RECORDS:      # a runaway spawner must not eat memory
                for old in sorted((r for r in self._records.values() if not r.alive()),
                                  key=lambda r: r.started)[:len(self._records) - MAX_RECORDS]:
                    self._records.pop(old.pid, None)
        self.event("spawn", pid=row.pid, cell=row.cell, detail=" ".join(row.argv)[:400])
        self.write_state()
        self.start_sampler()
        return row

    def finish(self, pid: int, exit_code: Optional[int] = None) -> Optional[Record]:
        """Mark a process gone. Safe to call twice (the second call changes nothing).

        The state file is rewritten *before* the call returns, so "this record says the process is
        gone" and "the file does not offer it to the next run's reaper" are the same moment - not
        two, with a window in between that another thread (or the next ABP) can walk into."""
        with self._lock:
            row = self._records.get(int(pid))
            if row is None or not row.alive():
                return row
            row.exited = time.time()
            row.exit_code = exit_code
            cell = self._cells.get(row.cell) if row.cell else None
            still_live = any(r.alive() and r.cell == row.cell for r in self._records.values())
        self.write_state()
        self.event("exit", pid=row.pid, cell=row.cell, detail=f"exit {exit_code}")
        # A cell whose whole tree is gone has nothing left to contain: closing it here reclaims
        # its job handle instead of leaving one open per command for the life of the process.
        # A persistent cell is left alone - its owner closes it, or lets the process end take it.
        if cell is not None and not still_live and not cell.policy.persistent:
            cell.close()
        with self._lock:
            if not self._records and not self._cells:
                self.stop_sampler()
        return row

    def forget(self, pid: int) -> None:
        with self._lock:
            self._records.pop(int(pid), None)
        self.write_state()

    def records(self, alive_only: bool = False) -> list[Record]:
        with self._lock:
            rows = list(self._records.values())
        return [r for r in rows if r.alive()] if alive_only else rows

    def record_for(self, pid: int) -> Optional[Record]:
        with self._lock:
            return self._records.get(int(pid))

    # ---- the state file ---------------------------------------------------------------------
    def state(self) -> dict:
        with self._lock:
            processes = [r.summary() for r in self._records.values() if r.alive()]
            cells = [{"id": c.id, "policy": c.policy.describe(), "status": c.status()} for c in self._cells.values()]
        return {"runs": {self.run_id: {"pid": os.getpid(), "updated": round(time.time(), 3), "processes": processes}},
                "cells": cells}

    def write_state(self) -> bool:
        """Merge this run's live processes into the file, atomically, keeping every other
        run's entries. A read-only state root (an ABP embedded with no ABP_HOME) must not be
        able to break a spawn, so a failure here is reported as False, not raised."""
        try:
            merged = _read_state(self.path)
            runs = merged.get("runs") if isinstance(merged.get("runs"), dict) else {}
            mine = self.state()
            runs[self.run_id] = mine["runs"][self.run_id]
            payload = {"updated": round(time.time(), 3), "abp": runs[self.run_id], "runs": runs, "cells": mine["cells"]}
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(payload, indent=1), encoding="utf-8")
            os.replace(tmp, self.path)
            return True
        except OSError:
            return False

    def clear_state(self) -> None:
        """This run's entries are removed from the file (used at exit)."""
        try:
            merged = _read_state(self.path)
            runs = merged.get("runs") if isinstance(merged.get("runs"), dict) else {}
            runs.pop(self.run_id, None)
            tmp = self.path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps({"updated": round(time.time(), 3), "runs": runs}, indent=1), encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError:
            pass

    # ---- the sampler -------------------------------------------------------------------------
    def sample(self, *, reflexes: bool = True) -> None:
        """One measurement pass: every registered process, folded into its cell's totals.
        Called by the sampler thread, and directly by tests (so a rule can be checked without
        waiting for a tick)."""
        try:
            import psutil
        except ImportError:  # pragma: no cover - psutil is a hard dependency of ABP
            return
        rows = self.records(alive_only=True)
        per_cell: dict[str, dict] = {}
        for row in rows:
            cell_id = row.cell or f"pid:{row.pid}"
            bucket = per_cell.setdefault(cell_id, {"pid": 0, "rss_mb": 0.0, "cpu_percent": 0.0})
            try:
                p = psutil.Process(row.pid)
                with p.oneshot():
                    bucket["rss_mb"] += p.memory_info().rss / (1024 * 1024)
                    bucket["cpu_percent"] += p.cpu_percent(interval=None)
                    bucket["pid"] += 1
                if not p.is_running():                       # exited while we were looking
                    self.finish(row.pid, exit_code=None)
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess, OSError):
                self.finish(row.pid, exit_code=None)
        now = time.time()
        with self._lock:
            for cell_id, values in per_cell.items():
                self._samples[cell_id] = {**values, "at": now}
            for cell_id in [c for c in self._samples if c not in per_cell]:
                self._samples[cell_id] = {**self._samples[cell_id], "at": 0.0}
        if reflexes:
            try:
                from bot.sandbox_ns import reflexes

                reflexes.apply(self)
            except Exception:  # noqa: BLE001 - a reflex must never take the sampler down
                pass

    def samples(self) -> dict:
        with self._lock:
            return {k: dict(v) for k, v in self._samples.items()}

    def sample_for(self, cell_id: str) -> dict:
        return self.samples().get(cell_id, {"pid": 0, "rss_mb": 0.0, "cpu_percent": 0.0, "at": 0.0})

    def system_cpu_percent(self, interval: Optional[float] = None) -> float:
        import psutil

        return float(psutil.cpu_percent(interval=interval))

    def _run_sampler(self) -> None:
        while not self._stop.wait(SAMPLE_INTERVAL_S):
            try:
                self.sample()
            except Exception:  # noqa: BLE001 - the loop outlives any single bad sample
                pass

    def start_sampler(self) -> None:
        """Start measuring, if something is registered and the thread is not already up."""
        with self._lock:
            if self._sampler is not None and self._sampler.is_alive():
                return
            self._stop.clear()
            self._sampler = threading.Thread(target=self._run_sampler, name="sandbox-ns-sampler", daemon=True)
            self._sampler.start()

    def stop_sampler(self) -> None:
        with self._lock:
            self._stop.set()
            self._sampler = None

    # ---- what a person or a route wants to see ------------------------------------------------
    def status(self) -> dict:
        cells = []
        for cell in self.cells():
            cells.append({"id": cell.id, "name": cell.name, "owner": cell.owner, "policy": cell.policy.describe(),
                          "processes": len([r for r in self.records(alive_only=True) if r.cell == cell.id]),
                          "sample": self.sample_for(cell.id), "limits": cell.status()})
        return {"run_id": self.run_id, "state_file": str(self.path), "guard": _guard_status(),
                "processes": [r.summary() for r in self.records()], "cells": cells,
                "events": self.events(limit=50), "sampler_running": bool(self._sampler and self._sampler.is_alive())}


def _guard_status() -> dict:
    from bot.sandbox_ns import guard

    return {"installed": guard.is_installed(), "converted": guard.converted()}


def create_time_of(pid: int) -> float:
    """The OS's create time for a pid, or 0.0 when the process is already gone (or psutil
    is not there) - 0.0 never matches a real process, so a record with it can never be reaped."""
    try:
        import psutil

        return float(psutil.Process(int(pid)).create_time())
    except Exception:  # noqa: BLE001
        return 0.0


def _read_state(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"runs": {}}
    return data if isinstance(data, dict) else {"runs": {}}


def mask_argv(argv: Iterable) -> list:
    """An argv as it is safe to write down: values that look like credentials, and any value
    that is a known secret, become `[secret]`. The registry writes this to disk and serves it
    on a status route, so `--api-key sk-...` must never survive into it."""
    args = [str(a) for a in argv]
    try:
        from bot.agent_runtime import secrets_guard

        known = set(secrets_guard.known_secrets().values())
    except Exception:  # noqa: BLE001
        known = set()
    out: list[str] = []
    mask_next = False
    for arg in args:
        if mask_next:
            out.append(MASKED)
            mask_next = False
            continue
        assign = _SECRET_ASSIGN.match(arg)
        if assign:
            out.append(f"{assign.group(1)}={MASKED}")
            continue
        if _SECRET_FLAG.match(arg):
            out.append(arg)
            # `--token value` and `--token=value` are both real; a bare `--token` is a flag.
            mask_next = "=" not in arg
            continue
        out.append(MASKED if arg in known else arg)
    return out


#: The one registry this process writes to.
registry = Registry()
