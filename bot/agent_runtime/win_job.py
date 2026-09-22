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
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes

_is_windows = hasattr(ctypes, "windll")

JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
JOB_OBJECT_LIMIT_ACTIVE_PROCESS = 0x00000008
JOB_OBJECT_LIMIT_PROCESS_MEMORY = 0x00000100
_JobObjectExtendedLimitInformation = 9
_PROCESS_SET_QUOTA = 0x0100
_PROCESS_TERMINATE = 0x0001

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
    _kernel32.CloseHandle.restype = wintypes.BOOL
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]


def is_supported() -> bool:
    return _is_windows


def create(*, memory_mb: int = 0, active_process_limit: int = 0) -> int:
    """Create a job object with kill-on-close (and optional limits) already set.
    Returns an opaque handle; raises OSError if the Win32 calls fail."""
    if not _is_windows:
        raise OSError("windows job objects are only available on Windows")
    handle = _kernel32.CreateJobObjectW(None, None)
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
    flags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if active_process_limit > 0:
        flags |= JOB_OBJECT_LIMIT_ACTIVE_PROCESS
        info.BasicLimitInformation.ActiveProcessLimit = active_process_limit
    if memory_mb > 0:
        flags |= JOB_OBJECT_LIMIT_PROCESS_MEMORY
        info.ProcessMemoryLimit = memory_mb * 1024 * 1024
    info.BasicLimitInformation.LimitFlags = flags
    ok = _kernel32.SetInformationJobObject(handle, _JobObjectExtendedLimitInformation, ctypes.byref(info),
                                            ctypes.sizeof(info))
    if not ok:
        err = ctypes.get_last_error()
        _kernel32.CloseHandle(handle)
        raise ctypes.WinError(err)
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


def terminate(job_handle: int, exit_code: int = 1) -> None:
    """Kill every process the job ever contained, then close the handle. Safe to call
    more than once, and safe to call with a handle whose processes are already gone."""
    if not _is_windows or not job_handle:
        return
    try:
        _kernel32.TerminateJobObject(job_handle, exit_code)
    except OSError:
        pass
    try:
        _kernel32.CloseHandle(job_handle)
    except OSError:
        pass
