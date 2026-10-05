"""Starting, stopping, checking and reading the log of an instance process.

Modeled on bot/hosting/procs.py (which does the same job for the edge/tunnel/
Caddy processes ABP starts itself) and deliberately kept to the same rules:

  * CREATE_NO_WINDOW on Windows, so no console window ever appears. NEVER
    DETACHED_PROCESS: it makes the child its own console-group leader with no
    console at all, and a later `taskkill /T` from an unrelated console then
    behaves inconsistently - a real failure mode this project has hit.
  * the process outlives whatever started it. The gate and its instances must
    keep running when the terminal (or the desktop app, or this agent's shell)
    that launched them goes away, and must not die with it, which is what
    CREATE_NEW_PROCESS_GROUP + CREATE_NO_WINDOW gives us.
  * every pid is verified as ours (command line and create time) before it is
    ever signalled, so a recycled pid is never mistaken for an instance.
  * the active instance runs at normal priority; every other instance (a
    standby waiting to be swapped in, a sandbox an agent is using) runs BELOW
    NORMAL priority, so a dev sandbox can never make the machine feel slow for
    the person actually using ABP.

Every instance also gets a Windows job object (bot/agent_runtime/win_job.py),
which is the only reliable way to stop one. Two facts make anything less
reliable, and both of them bit this project for real:

  * a venv's python.exe is a LAUNCHER: it starts the real interpreter as a
    CHILD and waits for it. Signalling the launcher alone leaves the
    interpreter running, holding the instance's port, and a supervisor that
    "restarted" it then started a second interpreter on top of that one.
  * that is also why a dead launcher is not a dead instance, and why walking
    `psutil.children()` at kill time is a race: the child may already be
    orphaned, in which case the tree is empty and nothing gets killed.

A job object closes both holes. It holds the interpreter no matter who its
parent is, it holds every process the interpreter starts afterwards (children
inherit membership), and JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE means the gate's
handle closing - including because the gate itself was killed, however it was
killed - takes the whole instance with it. Everything here degrades to psutil
tree-killing plus `taskkill /T /F` where a job object is not available.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("abp_gate.procs")

NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0
NEW_PROCESS_GROUP = 0x00000200 if sys.platform == "win32" else 0

#: How long after a spawn an instance's job keeps absorbing processes the
#: launcher already started. Assigning the launcher is not enough (see the
#: module docstring); assigning its existing children covers the interpreter
#: the launcher spawns in its first few milliseconds. This is best-effort, not
#: airtight - the documented race in bot/agent_runtime/win_job.py - and what
#: closes it for good is that the interpreter cannot have run any user code yet.
JOB_ABSORB_S = 0.35
JOB_ABSORB_INTERVAL_S = 0.05


class ProcError(RuntimeError):
    pass


def python_for(code_root: Path) -> Path:
    """The interpreter for an instance at `code_root`.

    `<code root>/.venv` first: a worktree made by `abp_cli dev up` has no venv
    of its own, and a worktree that does (because somebody copied one in) is
    using it deliberately. Falling back to this very interpreter is the correct
    last resort - it is a Python that can import ABP, which is all an instance
    needs, and a missing venv is not a reason to refuse to start."""

    rel = ("Scripts", "python.exe") if sys.platform == "win32" else ("bin", "python")
    candidate = code_root / ".venv" / rel[0] / rel[1]
    return candidate if candidate.is_file() else Path(sys.executable)


def _win_job():
    """bot.agent_runtime.win_job, imported lazily and never fatally.

    It is stdlib ctypes only (no ABP runtime), which is why the gate is allowed
    to use it at all: nothing here can be broken by the code being swapped.
    Anywhere it cannot be imported or the platform has no job objects, every
    caller falls back to psutil tree-killing plus taskkill."""
    try:
        from bot.agent_runtime import win_job

        return win_job if win_job.is_supported() else None
    except Exception:  # noqa: BLE001 - no job objects here, then
        return None


class Job:
    """One job object, holding exactly one instance and everything it starts.

    `live` is False on any platform without job objects (and if creation
    failed), and every method is then a no-op, so callers never have to ask
    whether this one is real - `kill()` falls through to the pid-based tree
    kill that has always been here.
    """

    def __init__(self, label: str = ""):
        self.label = label
        self.handle: Optional[int] = None
        self.error = ""
        win_job = _win_job()
        if win_job is None:
            self.error = "job objects are not available here"
            return
        try:
            self.handle = win_job.create()
        except OSError as exc:  # pragma: no cover - only on a locked-down host
            self.error = str(exc)
            logger.warning("no job object for %r (%s); falling back to tree-kill", label, exc)

    @property
    def live(self) -> bool:
        return bool(self.handle)

    def absorb(self, pid: int) -> None:
        """Put `pid` - and anything it has already started - in the job."""
        if not self.handle:
            return
        win_job = _win_job()
        if win_job is None:  # pragma: no cover - cannot happen while live
            return
        deadline = time.monotonic() + JOB_ABSORB_S
        seen: set[int] = set()
        while True:
            for target in [pid] + _tree(pid):
                if target in seen:
                    continue
                seen.add(target)
                try:
                    win_job.assign(self.handle, target)
                except OSError as exc:
                    # Already dead, or the platform refused the nesting. Either
                    # way there is nothing better to do here than log and let
                    # the pid-based kill be the backstop.
                    logger.debug("could not assign pid %s to the job for %r: %s", target, self.label, exc)
            if time.monotonic() >= deadline:
                return
            time.sleep(JOB_ABSORB_INTERVAL_S)

    def kill(self, exit_code: int = 1) -> None:
        """Kill everything this job ever held and drop the handle.

        Terminate-then-close rather than close alone: the explicit terminate
        does not depend on the handle being the last one, which matters when a
        gate is re-adopting instances it has no handle for."""
        handle, self.handle = self.handle, None
        if not handle:
            return
        win_job = _win_job()
        if win_job is None:  # pragma: no cover
            return
        try:
            win_job.terminate(handle, exit_code)
        except Exception:  # noqa: BLE001 - the process is going away regardless
            logger.debug("terminating the job for %r failed", self.label, exc_info=True)


def _tree(pid: int) -> list[int]:
    p = process(pid)
    if p is None:
        return []
    try:
        return [c.pid for c in p.children(recursive=True)]
    except Exception:  # noqa: BLE001 - access denied / already gone
        return []


def tree_pids(pid: Optional[int]) -> list[int]:
    """`pid` plus every process below it, right now.

    The venv launcher is why this is more than one pid: the instance is the
    interpreter it started, and an assertion (or a status line) that only
    looked at the launcher would call a dead instance "stopped" while its
    interpreter went on holding the port."""
    if not pid:
        return []
    return [pid] + _tree(pid)


def wait_gone(pids, timeout: float = 15.0) -> list[int]:
    """Wait until every one of `pids` is gone; returns the ones that survived."""
    wanted = [p for p in pids if p]
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        alive_left = [p for p in wanted if alive(p)]
        if not alive_left:
            return []
        time.sleep(0.1)
    return [p for p in wanted if alive(p)]


def instance_argv(python: Path, *, standby: bool = False) -> list[str]:
    argv = [str(python), "-m", "bot.main"]
    if standby:
        argv.append("--standby")
    return argv


def spawn(
    argv: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    log_path: Path,
    below_normal: bool = True,
    wait_s: float = 1.0,
    job: Optional[Job] = None,
) -> int:
    """Start one instance, windowless, logging to `log_path`. Returns its pid.

    `wait_s` is short and only used to catch a process that dies on the spot
    (a bad code root, an import error at module scope) so the caller can report
    the real traceback from the log instead of a bare exit code.

    `job`, when given, is the instance's job object: the process (and the
    interpreter its launcher starts) joins it before this returns, which is
    what makes the whole instance killable afterwards."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    with open(log_path, "ab") as log:
        log.write(f"\n--- {stamp} starting {' '.join(argv)}\n".encode())
        log.flush()
        kw: dict[str, Any] = {}
        if sys.platform == "win32":
            kw["creationflags"] = NO_WINDOW | NEW_PROCESS_GROUP
        else:
            kw["start_new_session"] = True
        try:
            proc = subprocess.Popen(  # noqa: S603 - argv is ours, no shell
                argv,
                cwd=str(cwd),
                env=env,
                stdout=log,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                **kw,
            )
        except OSError as exc:
            raise ProcError(f"cannot start {' '.join(argv)}: {exc}") from exc
    pid = proc.pid
    if job is not None:
        job.absorb(pid)
    if below_normal:
        set_priority(pid)
    time.sleep(wait_s)
    if not alive(pid):
        raise ProcError(f"the instance exited at once (code {proc.returncode}):\n{tail(log_path, 25)}")
    return pid


def process(pid: Optional[int]) -> Optional[Any]:
    if not pid:
        return None
    try:
        import psutil

        return psutil.Process(pid)
    except Exception:  # noqa: BLE001 - psutil.NoSuchProcess and ImportError both mean "no process"
        return None


def alive(pid: Optional[int]) -> bool:
    p = process(pid)
    if p is None:
        return False
    try:
        import psutil
        return p.is_running() and p.status() != psutil.STATUS_ZOMBIE
    except Exception:  # noqa: BLE001
        return False


def owns(pid: Optional[int], marker: str = "bot.main") -> bool:
    """Is this pid one of OUR ABP instances? The command line is the check -
    pids get recycled, and a gate that killed "its" pid on faith would, one day
    on an unlucky machine, kill somebody's editor."""
    p = process(pid)
    if p is None:
        return False
    try:
        cmd = " ".join(p.cmdline())
    except Exception:  # noqa: BLE001 - access denied / already gone
        return False
    return marker in cmd


def set_priority(pid: Optional[int], below_normal: bool = True) -> str:
    """Below-normal priority for everything that isn't the active instance."""
    p = process(pid)
    if p is None:
        return ""
    try:
        p.nice(psutil_below_normal() if below_normal else psutil_normal())
        return "below-normal" if below_normal else "normal"
    except Exception:  # noqa: BLE001 - priority is a nicety, never a requirement
        return ""


def psutil_below_normal() -> int:
    import psutil

    if hasattr(psutil, "BELOW_NORMAL_PRIORITY_CLASS"):
        return int(psutil.BELOW_NORMAL_PRIORITY_CLASS)
    return 10  # POSIX nice


def psutil_normal() -> int:
    import psutil

    if hasattr(psutil, "NORMAL_PRIORITY_CLASS"):
        return int(psutil.NORMAL_PRIORITY_CLASS)
    return 0


def stop(pid: Optional[int], *, timeout: float = 10.0, marker: str = "bot.main") -> bool:
    """Stop the process TREE politely, then firmly, then by taskkill.

    A venv's python.exe on Windows re-execs the real interpreter as a child, so
    killing only the pid we spawned leaves the actual ABP holding the port.
    `taskkill /T /F` is last because it is the only one of the three that
    reliably walks a tree through a launcher whose children were reparented -
    but it only knows about processes it can still find from `pid`, so it does
    not replace the job object, it backs it up (see `Job`)."""
    p = process(pid)
    if p is None:
        return False
    if marker and not owns(pid, marker):
        return False
    try:
        children = p.children(recursive=True)
    except Exception:  # noqa: BLE001
        children = []
    try:
        for c in children:
            with_suppress(c.terminate)
        p.terminate()
        gone, _alive = psutil_wait(p, timeout)
        if not gone:
            for c in children:
                with_suppress(c.kill)
            with_suppress(p.kill)
            psutil_wait(p, 5.0)
    except Exception:  # noqa: BLE001 - the process is going away regardless
        pass
    if alive(pid) or any(alive(c.pid) for c in children):
        taskkill(pid)
    return True


def taskkill(pid: Optional[int], *, tree: bool = True) -> bool:
    """`taskkill /F`, with `/T` unless the caller wants one process only.

    The belt to `Job`'s braces. `tree=False` is for the case where a pid's
    children are NOT meant to die - killing the gate's own interpreter without
    taking the detached instance with it is how you see whether the two really
    are independent."""
    if not pid or sys.platform != "win32" or not alive(pid):
        return False
    argv = ["taskkill", "/F"] + (["/T"] if tree else []) + ["/PID", str(pid)]
    try:
        return subprocess.run(  # noqa: S603 - fixed argv, no shell
            argv, capture_output=True, timeout=30, creationflags=NO_WINDOW, check=False,
        ).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def taskkill_tree(pid: Optional[int]) -> bool:
    """`taskkill /F /T /PID`: the process and everything it started."""
    return taskkill(pid, tree=True)


def with_suppress(fn, *args) -> None:
    try:
        fn(*args)
    except Exception:  # noqa: BLE001
        pass


def psutil_wait(p, timeout: float):
    import psutil

    return psutil.wait_procs([p], timeout=timeout)


def tail(log_path: Path, lines: int = 80) -> str:
    try:
        return "\n".join(Path(log_path).read_text(encoding="utf-8", errors="replace").splitlines()[-lines:])
    except OSError:
        return ""


def is_free(port: int) -> bool:
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def free_port(preferred: int = 0) -> int:
    from abp_gate import registry

    return registry.free_port(preferred)


def wait_port_closed(port: int, timeout: float = 5.0) -> bool:
    """True once nothing accepts on `port`. Windows can hold a just-closed
    socket for a moment (TIME_WAIT), and a swap that immediately rebinds the
    same port would otherwise lose that race."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if is_free(port):
            return True
        time.sleep(0.1)
    return is_free(port)


def instance_env(
    *,
    code_root: Path,
    data_root: Optional[Path],
    port: int,
    localai_port: int = 0,
    extra: Optional[dict[str, str]] = None,
) -> dict[str, str]:
    """The environment one instance is started with.

    Only what makes an instance an instance; everything else is inherited:
    ABP_HOME (its own state), DASHBOARD_PORT (its own private port),
    ABP_LOCALAI_PORT (private, so the gate can own the public 11436), and the
    two role flags bot/lease.py reads."""
    env = dict(os.environ)
    env.update(
        {
            "PYTHONPATH": str(code_root) + os.pathsep + env.get("PYTHONPATH", ""),
            "PYTHONUNBUFFERED": "1",
            "PYTHONUTF8": "1",
            "DASHBOARD_HOST": "127.0.0.1",
            "DASHBOARD_PORT": str(port),
            "ABP_GATE": "1",
            # The gate owns this process's lifetime and health; ABP's own
            # watchdog restarting it behind the gate's back would just be a
            # second supervisor racing the first one.
            "ABP_SUPERVISED": "",
            "ABP_SUPERVISOR_PID": "",
        }
    )
    if data_root is not None:
        env["ABP_HOME"] = str(data_root)
    else:
        env.pop("ABP_HOME", None)
    if localai_port:
        env["ABP_LOCALAI_PORT"] = str(localai_port)
    env.update(extra or {})
    return env


def kill_stragglers(port: int, marker: str = "bot.main") -> list[int]:
    """Any of OUR ABP processes still listening on `port`, killed tree-first.
    Only ever called for a port the gate owns, and only for pids whose command
    line contains `marker`."""
    killed: list[int] = []
    for pid in pids_on_port(port):
        if owns(pid, marker) and stop(pid, marker=marker):
            killed.append(pid)
    return killed


def pids_on_port(port: int) -> list[int]:
    """Pids LISTENING on `port`. netstat -ano on Windows, `ss -ltnp`/`lsof`
    elsewhere; psutil's own net_connections needs privileges this deliberately
    does not assume."""
    import shutil

    if sys.platform == "win32":
        exe = shutil.which("netstat")
        args = [exe, "-ano", "-p", "tcp"] if exe else []
    else:
        exe = shutil.which("ss") or shutil.which("lsof")
        args = [exe, "-ltnp"] if exe else []
    if not exe:
        return []
    try:
        out = subprocess.run(  # noqa: S603 - fixed argv, no shell
            args, capture_output=True, text=True, timeout=15, encoding="utf-8", errors="replace",
            creationflags=NO_WINDOW if sys.platform == "win32" else 0,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    pids: list[int] = []
    for line in out.stdout.splitlines():
        # netstat columns: Proto LocalAddress ForeignAddress State PID.
        # ss/lsof both end the line with the owning pid, after a process name.
        cols = line.split()
        if len(cols) < 2:
            continue
        local = cols[1] if sys.platform == "win32" else cols[3] if len(cols) > 3 else ""
        if not local.endswith(f":{port}"):
            continue
        for candidate in reversed(cols):
            if candidate.isdigit() and int(candidate) > 4:
                pid = int(candidate)
                if pid not in pids:
                    pids.append(pid)
                break
    return pids