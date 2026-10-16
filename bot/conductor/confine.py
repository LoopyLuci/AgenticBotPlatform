"""The confined runner's checks: before a conductor worker is given `run_shell`, prove the sandbox it would run in really confines.

`shell="sandbox"` (the owner's approval of 2026-10-07: native agent, file + test tools, `run_shell` only inside an AppContainer with no
network) must never fall back to an unconfined shell. `verify()` runs three probes through the very tool path the agent uses
(`run_shell` -> bot/agent_runtime/sandbox.py, same settings as `abp_agenteval.factory.agent_settings(shell="sandbox")`):

* the user's home folder cannot be listed,
* a file outside the worktree cannot be read,
* a loopback server cannot be reached (so neither can anything else on the network),

and a control that the worktree itself is writable. Any problem, or a platform without an AppContainer, raises `SandboxUnavailable`.
"""
from __future__ import annotations

import asyncio
import os
import shutil
import socket
import sys
import tempfile
import threading
from pathlib import Path
from typing import Optional


class SandboxUnavailable(RuntimeError):
    """The confined shell cannot be provided (or did not confine); no shell is given and the worker does not run unconfined."""

    release_task = False


def sandbox_paths() -> list[str]:
    """Folders the sandboxed shell may read (read-only): the Python install and venv the agent's pytest runs from."""
    system = [os.path.normcase(os.environ.get(v, "")) for v in ("ProgramFiles", "ProgramFiles(x86)", "SystemRoot") if os.environ.get(v)]
    out: list[str] = []
    for p in (sys.prefix, sys.base_prefix):
        if not p or p in out or not Path(p).is_dir():
            continue
        if any(os.path.normcase(p).startswith(s) for s in system):      # already readable by every app container; a grant needs admin
            continue
        out.append(str(Path(p)))
    return out


def supported() -> bool:
    if os.name != "nt":
        return False
    try:
        from bot.agent_runtime import appcontainer

        return bool(appcontainer.is_supported())
    except Exception:  # noqa: BLE001
        return False


class _Listener:
    """A loopback TCP server that records whether anything connected."""

    def __init__(self) -> None:
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(5)
        self.sock.settimeout(0.2)
        self.port = self.sock.getsockname()[1]
        self.connections = 0
        self._stop = False
        self._t = threading.Thread(target=self._run, daemon=True)
        self._t.start()

    def _run(self) -> None:
        while not self._stop:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                continue
            self.connections += 1
            try:
                conn.close()
            except OSError:
                pass

    def close(self) -> None:
        self._stop = True
        try:
            self.sock.close()
        except OSError:
            pass
        self._t.join(timeout=2)


def _run_shell(workspace: Path, command: str, timeout: int = 20) -> str:
    from bot.agent_runtime import toolspec, tools
    from bot.agent_runtime.errors import ToolError

    token = toolspec.session_var.set("conductor-confine")
    try:
        return asyncio.run(tools.execute_tool("run_shell", {"command": command, "timeout": timeout}, workspace=workspace))
    except ToolError as exc:
        return f"[tool error] {exc}"
    finally:
        toolspec.session_var.reset(token)


def probe(workspace: Path, *, home: Optional[Path] = None) -> list[str]:
    """Run the probes in the *current* sandbox configuration; the list of problems (empty = confined). The caller has put the
    sandbox settings in place (see `verify`)."""
    problems: list[str] = []
    workspace = Path(workspace).resolve()
    home = Path(home or Path.home())

    ok = _run_shell(workspace, "echo confine-write> .confine_probe")
    probe_file = workspace / ".confine_probe"
    try:
        if not probe_file.exists():
            problems.append(f"the worktree is not writable from the sandbox: {ok[-200:]!r}")
    finally:
        probe_file.unlink(missing_ok=True)

    names = []
    try:
        names = [n for n in os.listdir(home) if len(n) > 3][:25]
    except OSError:
        pass
    listing = _run_shell(workspace, f'dir /b "{home}"')
    seen = [n for n in names if any(line.strip().lower() == n.lower() for line in listing.splitlines())]
    if seen:
        problems.append(f"the sandbox can list the user's home folder (saw {seen[:3]})")

    outside = Path(tempfile.mkdtemp(prefix="abp-confine-"))
    try:
        secret = outside / "secret.txt"
        secret.write_text("outside-the-worktree-secret", encoding="utf-8")
        got = _run_shell(workspace, f'type "{secret}"')
        if "outside-the-worktree-secret" in got:
            problems.append("the sandbox can read a file outside the worktree")
    finally:
        shutil.rmtree(outside, ignore_errors=True)

    curl = shutil.which("curl.exe")
    if curl:
        srv = _Listener()
        try:
            _run_shell(workspace, f"curl.exe -sS -m 5 http://127.0.0.1:{srv.port}/")
            if srv.connections:
                problems.append("the sandbox reached a loopback server (the network is not blocked)")
        finally:
            srv.close()
    return problems


def verify(workspace: Path, *, extra_paths: Optional[list[str]] = None) -> None:
    """Raise SandboxUnavailable unless the AppContainer shell is available and confines (probes above), using the settings a
    conductor worker's agent will get."""
    if not supported():
        raise SandboxUnavailable("the confined shell needs a Windows AppContainer; this machine cannot give one, so the worker "
                                 "runs without a shell (--shell none) rather than unconfined")
    from abp_agenteval import factory

    settings = factory.agent_settings(model="probe", shell="sandbox", sandbox_paths=extra_paths or sandbox_paths())
    with factory.agent_environment(settings, python=sys.executable):
        problems = probe(Path(workspace))
    if problems:
        raise SandboxUnavailable("the sandbox did not confine: " + "; ".join(problems))
