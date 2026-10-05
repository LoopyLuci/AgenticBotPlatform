"""One sandbox: a policy, and the OS thing that enforces it.

A `Cell` is what "every process ABP starts is contained, accounted for, limited and cleaned
up" means concretely. On Windows that is a Win32 Job Object (bot/agent_runtime/win_job.py,
extended with the CPU rate, affinity and priority limits): every process assigned to it, and
every process those start, is in the job for the rest of its life, and closing the job's
handle kills all of them. That is a guarantee, not a best-effort tree walk - unlike the
`taskkill /T /F` several callers used before, which loses a race against a process that forks
quickly or detaches deliberately.

**The race is real and documented here too** (win_job.py has the long version): a process has
to be assigned to the job *after* it is spawned, so anything it starts in those few
milliseconds is not guaranteed to be in the job. It is milliseconds wide - the caller spawns,
reads the pid, assigns - and unlike a container's namespaces it is not airtight.

On POSIX the same cell is a new session (so a signal to the process group reaches the whole
tree) plus `setrlimit`/`nice`/`sched_setaffinity` where the OS lets an unprivileged process
apply them, and a cgroup v2 slice when `/sys/fs/cgroup` is writable. Where a limit could not
be applied, `status()` says so by name instead of pretending: a cell that says "memory_mb: 0"
because nothing was capped is a lie, and this is exactly the kind of lie that is hard to
notice on a machine that is merely slow.
"""

from __future__ import annotations

import contextlib
import os
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Iterator, Optional

from bot.sandbox_ns import policy as policy_mod
from bot.sandbox_ns.policy import Policy


class Cell:
    """A group of processes under one policy. Use it as a context manager to be sure it is
    closed; a cell left open keeps its job handle (and, for a non-persistent one, everything
    in it) alive."""

    def __init__(self, policy: Policy, *, name: str = "", owner: str = "", register: bool = True):
        self.id = f"{policy.name}-{uuid.uuid4().hex[:8]}"
        self.policy = policy_mod.with_defaults(policy)
        self.name = name or self.id
        self.owner = owner
        self.created = time.time()
        self.notes: list[str] = []               # limits asked for but not applied, and why
        self._lock = threading.RLock()
        self._closed = False
        self._job: Optional[int] = None          # a Win32 Job Object handle
        self._pids: list[int] = []
        self._kills = 0
        self._cgroup: Optional[str] = None
        self._posix_affinity: Optional[tuple] = None
        self._prepare(register)

    # ---- the OS side ------------------------------------------------------------------------
    def _prepare(self, register: bool = True) -> None:
        if os.name == "nt":
            self._prepare_windows()
        else:
            self._prepare_posix()
        if register:
            # Sampled per cell id, which outlives the cell: a daemon whose owner let go of the
            # handle is no longer limited by a job, but it is still a recorded process, and the
            # numbers it used are worth keeping for the diagnostics view.
            from bot.sandbox_ns.registry import registry

            registry.add_cell(self)

    def _prepare_windows(self) -> None:
        from bot.agent_runtime import win_job

        p = self.policy
        affinity = p.affinity
        if affinity and max(affinity) > win_job.MAX_AFFINITY_CPU:
            self.notes.append(f"affinity skipped: a job's mask names at most 64 processors and this one wants "
                              f"cpu {max(affinity)}")
            affinity = None
        # A persistent cell's job is never kill-on-close, whatever the policy says: this handle is
        # going to be closed (at GC, or when ABP exits) while the daemon keeps running, and the
        # default kill-on-close would take it down exactly when ABP was not watching.
        try:
            self._job = win_job.create(memory_mb=p.memory_mb, job_memory_mb=p.job_memory_mb,
                                       active_process_limit=p.max_processes,
                                       cpu_rate_percent=p.cpu_rate_percent, affinity=affinity,
                                       priority=p.priority, kill_on_close=p.kill_on_close and not p.persistent)
        except OSError as exc:
            self.notes.append(f"no job object ({exc}); the tree is contained by the caller's own kill, not by Windows")

    def _prepare_posix(self) -> None:
        p = self.policy
        if p.affinity and hasattr(os, "sched_setaffinity"):
            self._posix_affinity = tuple(p.affinity)
        else:
            self._posix_affinity = None
        self._cgroup = _make_cgroup(self.id, self.notes)

    # ---- putting a process in ---------------------------------------------------------------
    def admit(self, proc_or_pid) -> None:
        """Put an already-started process in this cell, immediately. Raises OSError if the OS
        refused, and says so in `notes` - the caller decides whether that is fatal (spawn()
        treats a non-persistent cell's failure to admit as fatal, because "recorded but not
        contained" is not containment)."""
        pid = proc_or_pid if isinstance(proc_or_pid, int) else getattr(proc_or_pid, "pid", None)
        if pid is None:
            return
        with self._lock:
            self._pids.append(int(pid))
        if os.name == "nt":
            from bot.agent_runtime import win_job

            if self._job is None:
                # Windows refused the job object at creation (see notes). Admitting the process
                # anyway would mean a cell that contains nothing while claiming it does, so this
                # refuses: the caller either dies with the error or, for a persistent policy,
                # carries on knowing the tree is not contained.
                raise OSError("this cell has no job object, so nothing can be confined to it")
            try:
                win_job.assign(self._job, int(pid))
            except OSError as exc:
                self.notes.append(f"pid {pid} could not be assigned to the job ({exc})")
                raise
            return
        self._admit_posix(int(pid))

    def _admit_posix(self, pid: int) -> None:
        """setrlimit / nice / affinity / cgroup for one pid. Each of these is best effort: a
        container without CAP_SYS_RESOURCE, a system that caps nice at 0, a read-only cgroup
        tree - all normal, all recorded as a note instead of an exception."""
        import resource

        p = self.policy
        if p.memory_mb > 0:
            limit = p.memory_mb * 1024 * 1024
            for what in (resource.RLIMIT_AS, resource.RLIMIT_DATA):
                try:
                    _soft, hard = resource.getrlimit(what)
                    resource.setrlimit(what, (limit, hard))
                except (ValueError, OSError) as exc:
                    self.notes.append(f"rlimit {what} not applied ({exc})")
                    break
        if p.max_processes > 0 and hasattr(resource, "RLIMIT_NPROC"):
            try:
                _soft, hard = resource.getrlimit(resource.RLIMIT_NPROC)
                resource.setrlimit(resource.RLIMIT_NPROC, (p.max_processes, hard))
            except (ValueError, OSError) as exc:
                self.notes.append(f"RLIMIT_NPROC not applied ({exc}) - it counts every process of this user, not this tree")
        if p.priority != "normal":
            try:
                os.setpriority(os.PRIO_PROCESS, pid, policy_mod.nice_value(p.priority))
            except (OSError, PermissionError) as exc:
                self.notes.append(f"priority {p.priority} not applied ({exc})")
        if self._posix_affinity and hasattr(os, "sched_setaffinity"):
            try:
                os.sched_setaffinity(pid, set(self._posix_affinity))
            except OSError as exc:
                self.notes.append(f"affinity not applied ({exc})")
        if self._cgroup:
            _join_cgroup(self._cgroup, pid, self.notes)

    # ---- closing -----------------------------------------------------------------------------
    def kill(self, reason: str = "") -> int:
        """Stop everything in this cell now. The whole tree, not just the pids we know about:
        on Windows that is the job object (every descendant is in it by construction); on
        POSIX it is the process group, plus a psutil sweep for anything that left it."""
        with self._lock:
            if self._closed:
                return 0
            self._closed = True
            self._kills += 1
            pids = list(self._pids)
            job = self._job
            self._job = None
        detail = reason or f"killed cell {self.name}"
        from bot.sandbox_ns.registry import registry

        registry.event("kill", cell=self.id, detail=detail)
        if os.name == "nt" and job is not None:
            from bot.agent_runtime import win_job

            win_job.terminate(job)              # the job, not the pids: the tree is in it
            registry.remove_cell(self)
            registry.write_state()
            return len(pids)
        for pid in pids:
            kill_tree(pid)
        if self._cgroup:
            _drop_cgroup(self._cgroup)
        registry.remove_cell(self)
        registry.write_state()
        return len(pids)

    def close(self) -> None:
        """Release the cell. A non-persistent cell kills what is in it (that is what it is
        for); a persistent one - a daemon meant to outlive ABP - only lets go, and stays
        recorded and windowless."""
        if self.policy.persistent:
            with self._lock:
                job, self._job = self._job, None
                self._closed = True
            if os.name == "nt" and job is not None:
                from bot.agent_runtime import win_job

                win_job.close(job)
            if os.name != "nt" and self._cgroup:
                _drop_cgroup(self._cgroup)
            from bot.sandbox_ns.registry import registry

            registry.remove_cell(self)
            registry.write_state()
        else:
            self.kill(reason=f"closed cell {self.name}")

    def __enter__(self) -> "Cell":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def __del__(self) -> None:
        """A cell nobody holds any more releases its job handle. On Windows that handle is
        kill-on-close, so this is also the tree's last chance to be stopped - which is the right
        answer for a cell that was never closed, and harmless for one whose processes are
        already gone. Every failure here is swallowed: this can run at interpreter shutdown,
        with half the module globals already gone."""
        try:
            self.close()
        except Exception:  # noqa: BLE001
            pass

    # ---- what reflexes.py and the routes need ----------------------------------------------------
    @property
    def closed(self) -> bool:
        return self._closed

    def pids(self) -> list[int]:
        with self._lock:
            return list(self._pids)

    def job_handle(self) -> Optional[int]:
        """The live Job Object handle, or None on POSIX (and after a close). reflexes.py needs
        it to change one cell's priority for every process in it at once."""
        return self._job

    # ---- what people and routes want to know --------------------------------------------------
    def status(self) -> dict:
        out: dict[str, Any] = {"name": self.name, "owner": self.owner, "created": round(self.created, 3),
                               "closed": self._closed, "pids": list(self._pids), "kills": self._kills,
                               "notes": list(self.notes), "os": "windows" if os.name == "nt" else "posix",
                               "limits_applied": {}}
        p = self.policy
        if os.name == "nt":
            if self._job is not None:
                from bot.agent_runtime import win_job

                out["job"] = win_job.query(self._job)
                out["limits_applied"] = {k: out["job"].get(k) for k in
                                         ("memory_mb", "job_memory_mb", "cpu_rate_percent", "affinity", "priority",
                                          "active_process_limit")}
            else:
                out["job"] = {"error": "no job handle" if not p.kill_on_close else "closed"}
        else:
            out["cgroup"] = self._cgroup or "none (cgroups v2 not writable here)"
            out["limits_applied"] = {"affinity": list(self._posix_affinity) if self._posix_affinity else None}
        return out


def kill_tree(pid: int) -> None:
    """Best-effort tree kill for one pid, on a platform that has no job object: children first
    (so nothing is re-parented out of reach mid-sweep), then the process. Used by a cell's own
    POSIX close, by the timeout watchdog and by the reaper - one implementation of "stop this and
    everything it started", not three."""
    try:
        import psutil

        parent = psutil.Process(pid)
        for child in parent.children(recursive=True):
            with contextlib.suppress(psutil.Error, OSError):
                child.kill()
        parent.kill()
        return
    except Exception:  # noqa: BLE001 - psutil missing, or the pid is already gone
        pass
    with contextlib.suppress(OSError, subprocess.SubprocessError, ValueError):
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, timeout=15,
                           creationflags=0x08000000)
        else:
            import signal

            with contextlib.suppress(OSError):
                if os.getpgid(pid) != os.getpgid(0):
                    os.killpg(os.getpgid(pid), signal.SIGKILL)


_CGROUP_ROOT = Path("/sys/fs/cgroup")


def _make_cgroup(cell_id: str, notes: list) -> Optional[str]:
    """A cgroup v2 slice for this cell, when the kernel has one and this user may write to it.
    Otherwise None plus a note - the cell is still limited by setrlimit and nice, and its
    status says the stronger limit is not in force rather than pretending it is.

    Controllers only appear in a cgroup if the parent enabled them for it, so this walks down
    two levels (root -> abp-sandbox-ns -> the cell). Each write can fail on a host that manages
    its own cgroup tree (Docker, a systemd slice); that is normal and recorded, not fatal."""
    try:
        if not (_CGROUP_ROOT / "cgroup.controllers").exists():
            notes.append("no cgroup v2 at /sys/fs/cgroup; limits are setrlimit/nice only")
            return None
        root_available = {c.strip() for c in (_CGROUP_ROOT / "cgroup.controllers").read_text().split()}
        wanted = [c for c in ("memory", "cpu", "pids") if c in root_available]
        with contextlib.suppress(OSError):
            (_CGROUP_ROOT / "cgroup.subtree_control").write_text(" ".join(f"+{c}" for c in wanted), encoding="utf-8")
        parent = _CGROUP_ROOT / "abp-sandbox-ns"
        parent.mkdir(parents=True, exist_ok=True)
        granted = {c.strip() for c in (parent / "cgroup.controllers").read_text().split()}
        with contextlib.suppress(OSError):
            (parent / "cgroup.subtree_control").write_text(
                " ".join(f"+{c}" for c in wanted if c in granted), encoding="utf-8")
        path = parent / cell_id
        path.mkdir(parents=True, exist_ok=True)
        (path / "cgroup.procs").write_text(str(os.getpid()), encoding="utf-8")
        missing = [c for c in wanted if c not in granted]
        if missing:
            notes.append(f"cgroup v2 without {', '.join(missing)} (the host did not grant those controllers)")
        return str(path)
    except OSError as exc:
        notes.append(f"cgroups v2 not usable here ({exc}); limits are setrlimit/nice only")
        return None


def _join_cgroup(path: str, pid: int, notes: list) -> None:
    try:
        with open(os.path.join(path, "cgroup.procs"), "w", encoding="utf-8") as f:
            f.write(str(pid))
    except OSError as exc:
        notes.append(f"pid {pid} not moved into its cgroup ({exc})")


def _drop_cgroup(path: str) -> None:
    import shutil

    with contextlib.suppress(OSError):
        shutil.rmtree(path, ignore_errors=True)


def new_cell(preset: str = "tool", *, name: str = "", owner: str = "",
             policy: Optional[Policy] = None) -> Cell:
    """A cell for a named preset (or an explicit policy). Every caller that starts more than
    one process that belongs together should use one, so `Cell.kill()` really is one call."""
    return Cell(policy or policy_mod.policy_for(preset), name=name, owner=owner)


__all__ = ["Cell", "new_cell"]


@contextlib.contextmanager
def cell_for(preset: str = "tool", **kwargs) -> Iterator[Cell]:
    """`with cell_for("daemon", name="hermes-bridge") as cell:` - closed however the block
    ends, including an exception."""
    c = new_cell(preset, **kwargs)
    try:
        yield c
    finally:
        c.close()
