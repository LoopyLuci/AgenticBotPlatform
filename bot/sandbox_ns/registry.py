"""Every process and cell ABP starts is written down: the nervous system's memory.

A process that is not recorded cannot be accounted for, and a process that is not accounted
for is how a machine ends up with three orphaned `llama-server`s and no idea who started them.
So one registry records each spawn (pid, the process's own create time, its argv with
secret-looking arguments masked, cwd, which component asked for it, the cell and policy
under it), notices when it exits, and keeps `data/sandbox_ns/live.json` up to date so the
*next* ABP run can find and stop what this one left behind (reaper.py).

**The process ABP starts is often not the process that does the work.** On Windows a venv's
`Scripts/python.exe` is a launcher: it starts the base interpreter as a child and waits for it,
and that child is the one running the training worker, the module hub or the build step. A
`cargo` under `npm`, Playwright's browser under its driver and `ssh` under `git` are the same
shape. So a record is a *tree*, not a process: every descendant gets its own record, under the
same owner, cell and policy as the one that started it (a job object, a cgroup and a session are
all inherited, so it really is contained), with `parent_pid` pointing back up to it. A registry
of launchers alone would be a list of what ABP asked for rather than of what is running, and the
reaper, the status page and a "who is using 8 GB" question would all be reading the wrong pid.
Measured on this machine: `sys.executable` inside a venv-launched child is the *base*
interpreter, and every Python process ABP starts is two of them.

**pid alone is not an identity.** Windows reuses pids freely, so every record carries the
process's create time and both must match before anything is killed - a record whose pid
belongs to a different, newer process is never touched.

**live.json holds several runs.** ABP is more than one process (the server, a training
worker, an MCP server), and each writes only its own run's entries under its own key,
merging what is already there. So a worker exiting cannot erase the server's bookkeeping,
and two ABP instances on one machine (a live one and a developer's) do not fight over one
file.

A sampler thread (psutil, every few seconds, only while something is registered) records CPU
and memory per cell, which is what reflexes.py reacts to, and takes one extra pass a moment
after each spawn: the processes a spawn starts exist within milliseconds of it returning, and
that is the only moment the accounting sees them as promptly.
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
#: One spawn is rarely one process any more (a venv launcher and the interpreter behind it, a
#: build's compilers), so the table has room for a few trees rather than a few dozen commands.
MAX_RECORDS = 1000
SAMPLE_INTERVAL_S = 3.0
#: How long after a spawn the tree it started is looked at. A launcher's child is not there yet in
#: the microsecond after CreateProcess returns, and nowhere else in that process's life is the
#: accounting this prompt; the sampler's own tick after that is the backstop.
SETTLE_DELAY_S = 0.3
#: How far apart two readings of the same process's create time may be and still be the same
#: process (seconds). It is the same kernel counter read twice, so it should be exact; the slack
#: only absorbs a rounding difference, never a genuinely different process.
CREATE_TIME_SLACK_S = 1.0

_SECRET_FLAG = re.compile(r"(?i)^--?[a-z0-9_.-]*(token|secret|passw|credential|api[_-]?key|access[_-]?key)[a-z0-9_.-]*")
_SECRET_ASSIGN = re.compile(r"(?i)^([a-z0-9_.-]*(token|secret|passw|credential|api[_-]?key)[a-z0-9_.-]*)=(.*)$")
MASKED = "[secret]"


@dataclass
class Record:
    """One process ABP started, or one that a process it started started. `create_time` is the
    OS's own value for it (seconds since the epoch, as psutil reports it), because that is what
    makes a pid unambiguous; `parent_pid` is the record above it in the tree, and 0 for the process
    ABP spawned itself."""
    pid: int
    create_time: float
    argv: list = field(default_factory=list)
    cwd: str = ""
    owner: str = ""
    name: str = ""
    cell: str = ""
    policy: str = ""
    persistent: bool = False
    parent_pid: int = 0
    started: float = field(default_factory=time.time)
    exited: Optional[float] = None
    exit_code: Optional[int] = None

    def alive(self) -> bool:
        return self.exited is None

    def spawned(self) -> bool:
        """True for the process ABP started; False for one of its descendants."""
        return not self.parent_pid

    def summary(self) -> dict:
        out = asdict(self)
        out["alive"] = self.alive()
        out["role"] = "spawned" if self.spawned() else "descendant"
        return out


@dataclass
class _Snapshot:
    """The machine's process table from one pass, as `{parent pid: [its children]}`. One pass for a
    whole sampling pass whatever the number of records, which is what makes the walk affordable -
    `Process.children()`, the public way to ask the same question, builds this map again for every
    root it is called on."""
    children: dict = field(default_factory=dict)

    @classmethod
    def take(cls) -> "_Snapshot":
        out = cls()
        for pid, parent in _parent_map().items():
            if parent:
                out.children.setdefault(parent, []).append(pid)
        return out

    def is_same(self, pid: int, create_time: float) -> bool:
        """True only when `pid` is still the process that was recorded: Windows recycles pids, and
        whatever answers to a record's pid after that process died is not its tree - attributing a
        stranger's children to a cell is how a reaper ends up killing somebody's editor. A record
        with no create time to check against is let through: there is nothing to compare."""
        if create_time <= 0:
            return True
        now = create_time_of(pid)
        return now > 0 and abs(now - create_time) <= CREATE_TIME_SLACK_S


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
        self._wake = threading.Event()          # a spawn happened: the sampler comes back early
        self._spawned_at = 0.0                 # ... and when, so that pass comes back after SETTLE_DELAY_S

    # ---- where the state file lives -------------------------------------------------------
    @property
    def path(self) -> Path:
        """`<data>/sandbox_ns/live.json`, where `<data>` is ABP's own state root - ABP_HOME
        moves it, bot/envfile.py decides (PROJECT_ROOT, not CODE_ROOT). ABP_SANDBOX_NS_FILE overrides it
        (tests: parallel workers must never share, or reap from, the developer's real file)."""
        override = os.environ.get("ABP_SANDBOX_NS_FILE", "").strip()
        if override:
            return Path(override)
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

    def estop(self, *, reason: str = "the emergency stop was pressed") -> list[str]:
        """Stop the work, leave the services: every non-persistent cell is killed, and a daemon is
        left exactly as it is - still running *and* still registered, unlike close_cells(), which
        releases the daemons because APB's own exit is what is about to happen. Returns the cell ids
        that were killed."""
        killed = []
        for cell in self.cells():
            if cell.policy.persistent:
                continue
            cell.kill(reason)
            killed.append(cell.id)
        return killed

    # ---- processes --------------------------------------------------------------------------
    def record(self, *, pid: int, argv: Iterable, cwd: str = "", owner: str = "", name: str = "",
               cell=None, policy=None, create_time: Optional[float] = None, parent_pid: int = 0,
               write: bool = True) -> Record:
        """Write down one process ABP started. `parent_pid` is 0 for the spawn itself and the pid of
        the record above it for one of its descendants (see the module docstring); `write=False`
        leaves the state file to the caller, for recording a whole tree at once."""
        row = Record(pid=int(pid), create_time=float(create_time if create_time is not None else create_time_of(pid)),
                     argv=list(argv), cwd=str(cwd or ""), owner=owner, name=name or str(argv[0] if argv else pid),
                     cell=getattr(cell, "id", "") or "", policy=getattr(policy, "name", "") or "",
                     persistent=bool(getattr(policy, "persistent", False)), parent_pid=int(parent_pid or 0))
        self._add(row)
        if write:
            self.write_state()
        self._spawned()
        return row

    def _add(self, row: Record) -> None:
        """Put a row in the table and say so in the ring buffer. Split out of record() so that a
        whole tree can be written down with one state-file write at the end."""
        with self._lock:
            self._records[row.pid] = row
            if len(self._records) > MAX_RECORDS:      # a runaway spawner must not eat memory
                for old in sorted((r for r in self._records.values() if not r.alive()),
                                  key=lambda r: r.started)[:len(self._records) - MAX_RECORDS]:
                    self._records.pop(old.pid, None)
        self.event("descendant" if row.parent_pid else "spawn", pid=row.pid, cell=row.cell,
                   detail=" ".join(row.argv)[:400] or row.name)

    def _spawned(self) -> None:
        """A process has been recorded, so the tree it starts is worth a look in a moment (see
        SETTLE_DELAY_S). One flag, not a queue: a burst of spawns costs one pass, not one each."""
        self._spawned_at = time.monotonic()
        self._wake.set()
        self.start_sampler()

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

    # ---- the tree below a recorded process -----------------------------------------------------
    def record_descendants(self, pid: int, *, wait: float = 0.0, interval: float = 0.05) -> list[Record]:
        """Record every process below `pid` that has no record yet, and return what was added.

        This is what makes a record a tree rather than a single process (see the module docstring):
        the venv launcher and the interpreter behind it, a build's compilers, the browser under
        Playwright's driver. `wait` keeps looking until a pass finds nothing new or the time is up,
        for a caller that needs the answer now - a single pass is usually too early, because a
        launcher's child does not exist yet a microsecond after CreateProcess returns.

        One pass over the machine's process table per try, which is why the sampler takes one
        snapshot and walks every record from it instead of calling this per record."""
        root = int(pid)
        deadline = time.monotonic() + max(float(wait), 0.0)
        while True:
            try:
                with self._lock:
                    records = dict(self._records)
                added = self._walk(root, _Snapshot.take(), records)
            except Exception:  # noqa: BLE001 - no psutil, or a host that will not be asked
                return []
            if added:
                self.write_state()
                return added
            if time.monotonic() >= deadline:
                return []
            time.sleep(max(float(interval), 0.01))

    def _walk(self, root: int, snapshot: _Snapshot, records: dict) -> list[Record]:
        """Every unrecorded process under `root` in one snapshot, breadth first, so each child's
        `parent_pid` is a process that has a record of its own. `records` is the table this pass is
        working from, updated as it goes - a shared copy, so walking fifty records costs one
        snapshot rather than fifty.

        Whether the root is still the process the record names is checked *after* asking whether
        it has any new children at all: the check costs a process query, and a record with nothing
        new under it has nothing that could be misattributed."""
        parent = records.get(int(root))
        if parent is None or not parent.alive():
            return []                      # nothing to attribute a child to, or it is already gone
        queue = deque([(int(root), parent.create_time)])
        added: list[Record] = []
        while queue:
            current, current_created = queue.popleft()
            fresh = [c for c in snapshot.children.get(current, ()) if self._unrecorded(c, snapshot, records)]
            if not fresh:
                continue
            if current == int(root) and not snapshot.is_same(current, current_created):
                return []                 # the pid has been recycled: whatever is under it is not ours
            for child in fresh:
                row = self._describe(child, current, parent)
                if row is None or row.create_time < current_created - CREATE_TIME_SLACK_S:
                    continue             # gone, or born before its "parent": a recycled pid, not our child
                self._add(row)
                added.append(row)
                records[child] = row
                queue.append((child, row.create_time))
        return added

    def _unrecorded(self, pid: int, snapshot: _Snapshot, records: dict) -> bool:
        """True when `pid` needs a record: either it has none, or the one it has is for a process
        that has since died and this pid has been handed to something else."""
        seen = records.get(pid)
        return seen is None or (not seen.alive() and not snapshot.is_same(pid, seen.create_time))

    def _describe(self, pid: int, parent_pid: int, parent: Record) -> Optional[Record]:
        """What a process we did not spawn says about itself, as a record under the one that
        started it: same owner, cell and policy (containment is inherited with them), its own argv
        and cwd read from the OS rather than copied from the parent, and that record as its parent.
        None if it has already gone, which is the usual answer for something that appeared and
        finished between two passes."""
        import psutil

        try:
            proc = psutil.Process(int(pid))
        except Exception:  # noqa: BLE001 - it exited between the snapshot and this question
            return None
        with proc.oneshot():             # one process handle for all three questions
            argv = [str(a) for a in proc.cmdline()]
            create_time = float(proc.create_time())
            cwd = str(proc.cwd())
        if not argv and create_time <= 0:
            return None
        return Record(pid=pid, create_time=create_time, argv=mask_argv(argv), cwd=cwd or parent.cwd,
                      owner=parent.owner, name=os.path.basename(argv[0]) if argv else f"pid:{pid}",
                      cell=parent.cell, policy=parent.policy, persistent=parent.persistent,
                      parent_pid=parent_pid)

    def account(self) -> list[Record]:
        """One pass for the sake of the accounting alone: every process a recorded process has
        started, recorded under it. Nothing is measured here - a tree is worth seeing within a
        moment of the spawn that started it, while its CPU and memory can wait for the next tick."""
        try:
            snapshot = _Snapshot.take()
            with self._lock:
                records = dict(self._records)
        except Exception:  # noqa: BLE001 - no psutil, or a host that will not be asked
            return []
        added: list[Record] = []
        for pid in [r.pid for r in records.values() if r.alive()]:
            added += self._walk(pid, snapshot, records)
        if added:
            self.write_state()
        return added

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
        """One measurement pass: every registered process, folded into its cell's totals, plus the
        processes the registered ones started since the last pass. Called by the sampler thread,
        and directly by tests (so a rule can be checked without waiting for a tick)."""
        try:
            import psutil
        except ImportError:  # pragma: no cover - psutil is a hard dependency of ABP
            return
        self.account()                         # a cell's totals are its tree's, not one process's
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

                # The snapshot was taken above, so the rules read it rather than causing a second
                # pass over every process in the machine.
                reflexes.apply(self, sample=False)
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

    def _delay(self) -> float:
        """When the next pass is due: the rest of SETTLE_DELAY_S if a spawn was recorded less than
        that long ago, and the ordinary interval otherwise."""
        if not self._spawned_at:
            return SAMPLE_INTERVAL_S
        return max(0.0, SETTLE_DELAY_S - (time.monotonic() - self._spawned_at))

    def _run_sampler(self) -> None:
        """One thread with two jobs: an accounting pass a moment after a spawn (that is when the
        processes it starts exist), and the ordinary measurement pass every SAMPLE_INTERVAL_S.
        `record()` wakes the thread rather than queueing it, so a burst of spawns costs one pass."""
        while not self._stop.is_set():
            self._wake.wait(self._delay())
            self._wake.clear()
            if self._stop.is_set():
                return
            if self._spawned_at and time.monotonic() - self._spawned_at < SETTLE_DELAY_S:
                continue                  # woken by a spawn too young to have started anything yet
            settling = bool(self._spawned_at)
            self._spawned_at = 0.0          # the settle pass is due now; the next one is a whole interval away
            try:
                self.account() if settling else self.sample()
            except Exception:  # noqa: BLE001 - the loop outlives any single bad pass
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
            self._wake.set()               # the thread may be waiting out an interval: let it go
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


def _parent_map() -> dict:
    """`{pid: ppid}` for every process on the machine, in one pass.

    This is psutil's own map - the one `Process.children()` is built on - rather than a loop asking
    one process at a time: on Windows the platform reads the whole table in a single query
    (measured here: 7 ms for 400 processes), while `Process(pid).ppid()` in a loop costs ~6 ms
    *each*, which for a sampling pass is seconds of CPU in a background thread. The fallback is the
    same answer the slow way, for a psutil that has moved it."""
    import psutil

    try:
        return {int(pid): int(ppid) for pid, ppid in psutil._ppid_map().items()}
    except (AttributeError, ImportError, TypeError, ValueError):
        pass
    out: dict[int, int] = {}
    for pid in psutil.pids():
        try:
            out[int(pid)] = int(psutil.Process(pid).ppid())
        except Exception:  # noqa: BLE001 - gone, or not ours to look at
            continue
    return out


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
