"""`spawn(argv)` / `async_spawn(argv)`: how new code starts a process.

Everything ABP starts should go through here rather than calling `subprocess.Popen` directly,
because this one call gets five things right that are otherwise five separate mistakes:

* **windowless** (guard.py) - no console window on the desktop, ever, unless the code wrapped
  the call in `guard.visible()` because a person is meant to see it;
* **contained** (cell.py) - inside a job object on Windows, so `Cell.kill()` really is the
  whole tree, or a session/limits cell on POSIX;
* **accounted for** (registry.py) - recorded with its pid, create time, masked argv, cwd,
  owner and policy, in the ring buffer and in `data/sandbox_ns/live.json`;
* **limited** - the preset's memory, CPU rate, processor set and priority;
* **cleaned up** - a `timeout_s` in the policy kills the cell when it expires, and the record
  is marked as soon as the process is gone.

Pass a `cell=` for processes that belong together (a build and the tests it runs, a bridge and
its children) and that cell's policy is what applies; otherwise the named `preset=` is used.
Both at once is not an error: the cell wins, because the cell is what was asked to contain
them.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import os
import shlex
import subprocess
import threading
from typing import Any, Optional, Sequence, Union

from bot.sandbox_ns import guard
from bot.sandbox_ns.cell import Cell
from bot.sandbox_ns.policy import Policy

Argv = Union[str, Sequence]

#: What a caller may pass as `stdin`/`stdout`/`stderr`: the stdlib's own values, or the names
#: 'pipe' / 'devnull' / 'inherit' (which is what a config file can actually say).
_IO_ALIASES = {"pipe": subprocess.PIPE, "devnull": subprocess.DEVNULL, "inherit": None}


def _stdio(value: Any, name: str) -> Any:
    if isinstance(value, str):
        if value not in _IO_ALIASES:
            raise ValueError(f"{name} must be a file object, subprocess.PIPE/DEVNULL, or one of "
                             f"{', '.join(sorted(_IO_ALIASES))}, not {value!r}")
        return _IO_ALIASES[value]
    return value


def _argv(argv: Argv) -> list:
    if isinstance(argv, str):
        return [argv]
    return [str(a) for a in argv]


def _policy_for(cell: Optional[Cell], preset: str, policy: Optional[Policy]) -> Policy:
    from bot.sandbox_ns import policy as policy_mod

    if cell is not None:
        return cell.policy            # the cell is what was asked to contain these
    if policy is not None:
        return policy_mod.with_defaults(policy)
    return policy_mod.policy_for(preset)


def _with_timeout(policy: Policy, override: Optional[float]) -> Policy:
    """The policy as applied: an explicit `timeout_s=` at the call site beats the preset's."""
    if override is None or override == policy.timeout_s:
        return policy
    return dataclasses.replace(policy, timeout_s=float(override))


def spawn(argv: Argv, *, cell: Optional[Cell] = None, preset: str = "tool", cwd=None, env=None,
          stdin=None, stdout=None, stderr=None, visible: bool = False, name: str = "", owner: str = "",
          policy: Optional[Policy] = None, shell: bool = False, timeout_s: Optional[float] = None,
          **popen_kw) -> subprocess.Popen:
    """Start a process and put it in a sandbox. Returns the live Popen; the registry holds the
    record (`registry.record_for(pid)`), and the cell - if there is one - is what a later kill
    has to go through. `cwd`, `env` and the stdio arguments are Popen's own; `env=None` means
    the policy's environment (scrubbed of secret-looking names for 'tool', inherited
    otherwise).

    The pid here is the one Popen created, which is not always the process that ends up doing the
    work - on Windows a venv's `python.exe` is a launcher for the base interpreter - so the
    registry records what that process starts as well, under this one (registry.py's module
    docstring)."""
    guard.install()
    args = _argv(argv)
    if not args:
        raise ValueError("spawn() needs an argv (or a command, with shell=True)")
    pol = _policy_for(cell, preset, policy)
    if env is None:
        env = pol.environment()
    with guard.visible() if visible else contextlib.nullcontext():
        proc = subprocess.Popen(argv, cwd=str(cwd) if cwd else None, env=env,
                                stdin=_stdio(stdin, "stdin"), stdout=_stdio(stdout, "stdout"),
                                stderr=_stdio(stderr, "stderr"), shell=shell, **popen_kw)
    label = name or os.path.basename(args[0])
    _register(proc.pid, [shlex.join(args)] if shell else args, cwd, owner or "spawn", label, cell, pol, proc)
    _watch_exit(proc, proc.pid)
    _timeout_watchdog(cell, _with_timeout(pol, timeout_s), proc.pid, label)
    return proc


async def async_spawn(argv: Argv, *, cell: Optional[Cell] = None, preset: str = "tool", cwd=None, env=None,
                      stdin=None, stdout=None, stderr=None, visible: bool = False, name: str = "",
                      owner: str = "", policy: Optional[Policy] = None, shell: bool = False,
                      timeout_s: Optional[float] = None, **popen_kw) -> asyncio.subprocess.Process:
    """The asyncio equivalent. On Windows this goes through `subprocess.Popen` inside asyncio's
    own transport, so the guard covers it (tests/test_sandbox_ns.py checks that for real);
    elsewhere asyncio's fork/exec path cannot be given a job object, so the cell is a session
    and the limits are applied to the pid right after it starts.

    Its exit is recorded by the next sampler pass rather than by a watcher thread - awaiting
    `proc.wait()` from a plain thread is not a thing asyncio supports - so an idle process that
    has already exited can show as live for at most one sample interval."""
    guard.install()
    args = _argv(argv)
    if not args:
        raise ValueError("async_spawn() needs an argv (or a command, with shell=True)")
    pol = _policy_for(cell, preset, policy)
    if env is None:
        env = pol.environment()
    if os.name != "nt" and cell is not None:
        popen_kw.setdefault("start_new_session", True)
    with guard.visible() if visible else contextlib.nullcontext():
        if shell:
            proc = await asyncio.create_subprocess_shell(argv, cwd=str(cwd) if cwd else None, env=env,
                                                         stdin=_stdio(stdin, "stdin"), stdout=_stdio(stdout, "stdout"),
                                                         stderr=_stdio(stderr, "stderr"), **popen_kw)
        else:
            proc = await asyncio.create_subprocess_exec(*argv, cwd=str(cwd) if cwd else None, env=env,
                                                        stdin=_stdio(stdin, "stdin"), stdout=_stdio(stdout, "stdout"),
                                                        stderr=_stdio(stderr, "stderr"), **popen_kw)
    label = name or os.path.basename(args[0])
    _register(proc.pid, [shlex.join(args)] if shell else args, cwd, owner or "async_spawn", label, cell, pol, proc)
    _timeout_watchdog(cell, _with_timeout(pol, timeout_s), proc.pid, label)
    return proc


def run(argv: Argv, *, timeout: float = 60, cell: Optional[Cell] = None,
        check: bool = False, **kw) -> subprocess.CompletedProcess:
    """`subprocess.run()` through spawn(), for the many callers that only want a result and a
    timeout: containment, limits, an exit record and a timeout that takes the whole tree with
    it, all for one call. `cwd`/`env`/stdio are Popen's own; the result is Popen's own
    CompletedProcess, so an existing `subprocess.run()` call site can move over unchanged."""
    kw.setdefault("stdin", subprocess.DEVNULL)
    kw.setdefault("stdout", subprocess.PIPE)
    kw.setdefault("stderr", subprocess.PIPE)
    kw.setdefault("text", True)
    kw.setdefault("encoding", "utf-8")
    kw.setdefault("errors", "replace")
    proc = spawn(argv, cell=cell, **kw)
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        if cell is not None:
            cell.kill(reason=f"`{_cmdline(proc.args)}` passed its {timeout:g}s timeout")
        else:
            with contextlib.suppress(OSError, subprocess.SubprocessError):
                proc.kill()
                proc.wait()
        raise
    done = subprocess.CompletedProcess(proc.args, proc.returncode, out, err)
    if check and done.returncode:
        raise subprocess.CalledProcessError(done.returncode, done.args, done.stdout, done.stderr)
    return done


def _cmdline(args) -> str:
    return args if isinstance(args, str) else " ".join(str(a) for a in args)


# ---- what happens to the process once it is running ------------------------------------------------------------

def _register(pid: int, argv: list, cwd, owner: str, name: str, cell: Optional[Cell], policy: Policy, proc) -> None:
    """Put the process in its cell and write it down. A cell that could not contain the process
    is fatal for a non-persistent policy - an uncontained process is exactly the leak this
    package exists to stop - so the process is killed rather than left running free.

    What the process goes on to start is recorded by the registry's sampler, a moment later and
    again on every pass: none of it exists yet in the instant after CreateProcess returns."""
    from bot.sandbox_ns import registry as registry_mod
    from bot.sandbox_ns.registry import registry

    if cell is not None:
        try:
            cell.admit(pid)
        except OSError as exc:
            if not policy.persistent:
                with contextlib.suppress(OSError, ProcessLookupError):
                    proc.kill()
                raise OSError(f"{name} could not be confined to its cell: {exc}") from exc
            registry.event("limit_hit", pid=pid, cell=cell.id, detail=f"not confined ({exc}); a daemon outlives this")
    elif not policy.persistent:
        _soften(pid, policy)
    registry.record(pid=pid, argv=registry_mod.mask_argv(argv), cwd=str(cwd or ""), owner=owner, name=name,
                    cell=cell, policy=policy)
    if cell is not None:
        with contextlib.suppress(AttributeError):
            proc.abp_cell = cell        # so shell.kill_tree()/sandbox.kill() can find the cell


def _soften(pid: int, policy: Policy) -> None:
    """A process with no cell gets its priority and processor set directly (best effort). The
    job object is the real enforcement; this is what keeps a windowless, below-normal
    llama-server from competing with the person at the keyboard while nothing is in a cell."""
    if policy.priority == "normal" and not policy.affinity:
        return
    try:
        import psutil

        p = psutil.Process(pid)
        if policy.priority != "normal":
            p.nice(getattr(psutil, f"{policy.priority.upper()}_PRIORITY_CLASS"))
        if policy.affinity:
            p.cpu_affinity(list(policy.affinity))
    except Exception:  # noqa: BLE001 - a process that exited at once, or no psutil on this box
        pass


def _timeout_watchdog(cell: Optional[Cell], policy: Policy, pid: int, name: str) -> Optional[threading.Timer]:
    """A policy timeout kills the whole cell, not just the pid (a build's compiler is the thing
    that has to go). With no cell there is no tree to stop, so only the pid is killed - and the
    event says which it was."""
    if not policy.timeout_s:
        return None

    def fire() -> None:
        from bot.sandbox_ns.cell import kill_tree
        from bot.sandbox_ns.registry import registry

        detail = f"{name} passed its {policy.timeout_s:g}s limit"
        registry.event("limit_hit", pid=pid, cell=getattr(cell, "id", ""), detail=detail)
        if cell is not None:
            cell.kill(reason=detail)
        else:
            kill_tree(pid)

    timer = threading.Timer(policy.timeout_s, fire)
    timer.daemon = True
    timer.start()
    return timer


def _watch_exit(proc: subprocess.Popen, pid: int) -> None:
    """Mark the record the moment a synchronous process is gone, so the state file and the
    reaper never see a process that has already left."""
    def wait() -> None:
        from bot.sandbox_ns.registry import registry

        try:
            code = proc.wait()
        except Exception:  # noqa: BLE001 - Popen.wait() on a process nobody can wait for
            return
        registry.finish(pid, code)

    threading.Thread(target=wait, name=f"sandbox-ns-wait-{pid}", daemon=True).start()
