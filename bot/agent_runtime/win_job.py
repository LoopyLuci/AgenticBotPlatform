"""Minimal Win32 Job Object wrapper for roadmap P2's `windows_job` sandbox backend.

Real tree-confinement (and, optionally, a hard memory / process-count limit) for a
command spawned on Windows, using stdlib `ctypes` only - no `pywin32` dependency.
A job object created with `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE` guarantees every process
ever assigned to it, and every child *that process* spawns afterward, dies the instant
the job handle is closed via `terminate()`. This is stronger than the plain
`taskkill /T /F` the `local` backend still relies on, which walks a live process tree
by parent-pid and can lose a race against a process that detaches or forks quickly.

**Not a full sandbox.** Unlike the `docker` backend, a job object does not isolate the
filesystem or the network - see `sandbox.py`'s module docstring for exactly what each
backend does and does not confine.

**A real, documented race.** `assign()` must be called immediately after the process is
spawned. Any grandchildren the process creates *before* `assign()` returns are not
guaranteed to be members of the job - in practice this window is milliseconds (the
caller spawns, reads back the pid, and assigns before the shell has had time to do
anything), but it is not airtight the way a container's network/pid namespace is.

Extended (for bot/sandbox_ns/: the Sandbox Nervous System) with the three limits a
machine-wide nervous system needs and Windows can actually enforce per job: a hard CPU
rate cap (`cpu_rate_percent`), a processor affinity mask (`affinity`), and a priority
class (`priority`). `kill_on_close=False` creates a job for something that is meant to
outlive the process holding the handle (a daemon): the limits still apply, but closing
the handle no longer kills anything. `query()` reads the settings back, which is how
the tests check them against the real Win32 API instead of against this module's own
memory of what it asked for.

**Honest limits of these three.** Job affinity is a single 64-bit mask, so on a machine
with more than 64 processors it can only name the first 64 - the caller (cell.py) checks
that and says in its status when it left affinity alone instead of pretending it applied
it. The CPU rate cap is a share of *all* processors (10 000 cycles per 10 000), not of the
processors left after affinity, and it is enforced in ~10 ms scheduling periods, so it is
a ceiling, not a reservation.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
from typing import Iterable, Optional

_is_windows = hasattr(ctypes, "windll")

JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
JOB_OBJECT_LIMIT_ACTIVE_PROCESS = 0x00000008
JOB_OBJECT_LIMIT_AFFINITY = 0x00000010
JOB_OBJECT_LIMIT_PRIORITY_CLASS = 0x00000020
JOB_OBJECT_LIMIT_PROCESS_MEMORY = 0x00000100
JOB_OBJECT_LIMIT_JOB_MEMORY = 0x00000200
_JobObjectBasicLimitInformation = 2
_JobObjectExtendedLimitInformation = 9
_JobObjectCpuRateControlInformation = 15
_CPU_RATE_CONTROL_ENABLE = 0x1
_CPU_RATE_CONTROL_HARD_CAP = 0x4
_PROCESS_SET_QUOTA = 0x0100
_PROCESS_TERMINATE = 0x0001

#: ABP's own names for the Win32 priority classes (`BasicLimitInformation.PriorityClass`).
PRIORITY_CLASSES = {
    "idle": 0x00000040,
    "below_normal": 0x00004000,
    "normal": 0x00000020,
    "above_normal": 0x00008000,
    "high": 0x00000080,
    "realtime": 0x00000100,
}
#: The mask form of a job affinity is 64 bits wide, so this is the last processor it can name.
MAX_AFFINITY_CPU = 63

if _is_windows:
    _kernel32 = ctypes.windll.kernel32

    class _IO_COUNTERS(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_uint64), ("WriteOperationCount", ctypes.c_uint64),
            ("OtherOperationCount", ctypes.c_uint64), ("ReadTransferCount", ctypes.c_uint64),
            ("WriteTransferCount", ctypes.c_uint64), ("OtherTransferCount", ctypes.c_uint64),
        ]

    class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION), ("IoInfo", _IO_COUNTERS),
            ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    class _JOBOBJECT_CPU_RATE_CONTROL_INFORMATION(ctypes.Structure):
        _fields_ = [("ControlFlags", wintypes.DWORD), ("CpuRate", wintypes.DWORD)]

    # Explicit argtypes/restype throughout: HANDLE is pointer-sized, and ctypes'
    # default restype (c_int, 32 bits) silently truncates it on 64-bit Windows -
    # that bug is easy to write and hard to notice until a handle happens to land
    # above 4GB of address space, so it's avoided outright here rather than risked.
    _kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    _kernel32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
    _kernel32.SetInformationJobObject.restype = wintypes.BOOL
    _kernel32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]
    _kernel32.OpenProcess.restype = wintypes.HANDLE
    _kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    _kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    _kernel32.TerminateJobObject.restype = wintypes.BOOL
    _kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    _kernel32.QueryInformationJobObject.restype = wintypes.BOOL
    _kernel32.QueryInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD,
                                                    ctypes.POINTER(wintypes.DWORD)]
    _kernel32.CloseHandle.restype = wintypes.BOOL
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]


def is_supported() -> bool:
    return _is_windows


def affinity_mask(cpus: Optional[Iterable[int]]) -> int:
    """A CPU set as the single-bit-per-logical-processor mask a job's affinity is."""
    return sum(1 << int(c) for c in (cpus or ()))


def cpus_in_mask(mask: int) -> list[int]:
    return [c for c in range(MAX_AFFINITY_CPU + 1) if mask >> c & 1]


def _apply_limits(handle: int, *, memory_mb: int = 0, active_process_limit: int = 0, job_memory_mb: int = 0,
                  cpu_rate_percent: float = 0, affinity: Optional[Iterable[int]] = None, priority: str = "",
                  kill_on_close: bool = True) -> None:
    """Set every limit in one SetInformationJobObject call (the Win32 way is all-or-nothing
    per structure, so re-reading and rewriting is what `set_priority` does)."""
    info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
    flags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE if kill_on_close else 0
    if active_process_limit > 0:
        flags |= JOB_OBJECT_LIMIT_ACTIVE_PROCESS
        info.BasicLimitInformation.ActiveProcessLimit = active_process_limit
    if memory_mb > 0:
        flags |= JOB_OBJECT_LIMIT_PROCESS_MEMORY
        info.ProcessMemoryLimit = memory_mb * 1024 * 1024
    if job_memory_mb > 0:
        flags |= JOB_OBJECT_LIMIT_JOB_MEMORY
        info.JobMemoryLimit = job_memory_mb * 1024 * 1024
    cpus = tuple(int(c) for c in (affinity or ()))
    if cpus:
        flags |= JOB_OBJECT_LIMIT_AFFINITY
        info.BasicLimitInformation.Affinity = affinity_mask(cpus)
    if priority:
        if priority not in PRIORITY_CLASSES:
            raise ValueError(f"priority must be one of {', '.join(PRIORITY_CLASSES)}, not {priority!r}")
        flags |= JOB_OBJECT_LIMIT_PRIORITY_CLASS
        info.BasicLimitInformation.PriorityClass = PRIORITY_CLASSES[priority]
    info.BasicLimitInformation.LimitFlags = flags
    if not _kernel32.SetInformationJobObject(handle, _JobObjectExtendedLimitInformation, ctypes.byref(info),
                                             ctypes.sizeof(info)):
        raise ctypes.WinError(ctypes.get_last_error())
    if cpu_rate_percent > 0:
        rate = _JOBOBJECT_CPU_RATE_CONTROL_INFORMATION()
        rate.ControlFlags = _CPU_RATE_CONTROL_ENABLE | _CPU_RATE_CONTROL_HARD_CAP
        rate.CpuRate = max(1, min(10000, int(cpu_rate_percent * 100)))     # cycles per 10,000
        if not _kernel32.SetInformationJobObject(handle, _JobObjectCpuRateControlInformation, ctypes.byref(rate),
                                                 ctypes.sizeof(rate)):
            raise ctypes.WinError(ctypes.get_last_error())


def create(*, memory_mb: int = 0, active_process_limit: int = 0, job_memory_mb: int = 0,
           cpu_rate_percent: float = 0, affinity: Optional[Iterable[int]] = None, priority: str = "",
           kill_on_close: bool = True) -> int:
    """Create a job object with its limits already set.
    memory_mb caps each process; job_memory_mb caps all of the job's processes together; cpu_rate_percent is a hard
    cap on the share of the whole machine's CPU time the job may use (0 = no cap); affinity is the set of logical
    processors the job's processes may run on; priority is one of PRIORITY_CLASSES; kill_on_close=False makes the
    job outlive this process's handle without taking its processes down with it (a daemon).
    Returns an opaque handle; raises OSError if the Win32 calls fail."""
    if not _is_windows:
        raise OSError("windows job objects are only available on Windows")
    handle = _kernel32.CreateJobObjectW(None, None)
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        _apply_limits(handle, memory_mb=memory_mb, active_process_limit=active_process_limit,
                      job_memory_mb=job_memory_mb, cpu_rate_percent=cpu_rate_percent, affinity=affinity,
                      priority=priority, kill_on_close=kill_on_close)
    except Exception:
        _kernel32.CloseHandle(handle)
        raise
    return handle


def assign(job_handle: int, pid: int) -> None:
    """Put an already-running process into the job (see the race note above)."""
    if not _is_windows:
        raise OSError("windows job objects are only available on Windows")
    proc_handle = _kernel32.OpenProcess(_PROCESS_SET_QUOTA | _PROCESS_TERMINATE, False, pid)
    if not proc_handle:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        if not _kernel32.AssignProcessToJobObject(job_handle, proc_handle):
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        _kernel32.CloseHandle(proc_handle)


def set_priority(job_handle: int, priority: str) -> None:
    """Change only the priority class of a live job (reflexes.py demotes builds and
    workers when the machine gets hot). Every other limit is read back and re-sent,
    because the Win32 limit structure is written all at once."""
    if not _is_windows or not job_handle:
        return
    current = query(job_handle)
    _apply_limits(job_handle, memory_mb=int(current.get("memory_mb") or 0),
                  active_process_limit=int(current.get("active_process_limit") or 0),
                  job_memory_mb=int(current.get("job_memory_mb") or 0),
                  cpu_rate_percent=float(current.get("cpu_rate_percent") or 0.0),
                  affinity=current.get("affinity"), priority=priority,
                  kill_on_close=bool(current.get("kill_on_close")))


def query(job_handle: int) -> dict:
    """What this job's limits actually are, read back from Windows with QueryInformationJobObject.
    `affinity` is None when the job has no affinity limit, memory is in MiB, and 0 means "no
    limit set". Never raises: a job that cannot be queried reports an `error` instead.

    Three structures are read rather than one, deliberately: the affinity, priority and process
    count come from JOBOBJECT_BASIC_LIMIT_INFORMATION and the CPU rate from
    JOBOBJECT_CPU_RATE_CONTROL_INFORMATION, so the values checked here are not just an echo of
    the extended structure `create()` wrote.
    (JOBOBJECT_BASIC_ACCOUNTING_INFORMATION - the live process counters - is refused by
    QueryInformationJobObject on this Windows build, measured; ABP's own records and the registry
    carry those counts instead.)"""
    if not _is_windows or not job_handle:
        return {"error": "not a windows job"}
    extended = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
    if not _kernel32.QueryInformationJobObject(job_handle, _JobObjectExtendedLimitInformation,
                                               ctypes.byref(extended), ctypes.sizeof(extended), None):
        return {"error": ctypes.WinError(ctypes.get_last_error()).strerror}
    basic = extended.BasicLimitInformation
    flags = basic.LimitFlags
    out: dict = {
        "kill_on_close": bool(flags & JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE),
        "active_process_limit": int(basic.ActiveProcessLimit) if flags & JOB_OBJECT_LIMIT_ACTIVE_PROCESS else 0,
        "memory_mb": int(extended.ProcessMemoryLimit // (1024 * 1024)) if flags & JOB_OBJECT_LIMIT_PROCESS_MEMORY else 0,
        "job_memory_mb": int(extended.JobMemoryLimit // (1024 * 1024)) if flags & JOB_OBJECT_LIMIT_JOB_MEMORY else 0,
        "affinity": None,
        "priority": "",
        "cpu_rate_percent": 0.0,
    }
    read = _JOBOBJECT_BASIC_LIMIT_INFORMATION()
    if _kernel32.QueryInformationJobObject(job_handle, _JobObjectBasicLimitInformation, ctypes.byref(read),
                                           ctypes.sizeof(read), None):
        if read.LimitFlags & JOB_OBJECT_LIMIT_AFFINITY:
            out["affinity"] = cpus_in_mask(int(read.Affinity))
        if read.LimitFlags & JOB_OBJECT_LIMIT_PRIORITY_CLASS:
            out["priority"] = next((n for n, v in PRIORITY_CLASSES.items() if v == read.PriorityClass), "")
    rate = _JOBOBJECT_CPU_RATE_CONTROL_INFORMATION()
    if _kernel32.QueryInformationJobObject(job_handle, _JobObjectCpuRateControlInformation, ctypes.byref(rate),
                                           ctypes.sizeof(rate), None) and rate.CpuRate:
        out["cpu_rate_percent"] = round(rate.CpuRate / 100.0, 2)
    return out


def close(job_handle: int) -> None:
    """Close the handle only. Because the job is created with kill-on-close (the default),
    this kills everything in it - which is the point for a non-persistent cell. A job made
    with kill_on_close=False survives this."""
    if not _is_windows or not job_handle:
        return
    try:
        _kernel32.CloseHandle(job_handle)
    except OSError:
        pass


def terminate(job_handle: int, exit_code: int = 1) -> None:
    """Kill every process the job ever contained, then close the handle. Safe to call
    more than once, and safe to call with a handle whose processes are already gone."""
    if not _is_windows or not job_handle:
        return
    try:
        _kernel32.TerminateJobObject(job_handle, exit_code)
    except OSError:
        pass
    close(job_handle)
