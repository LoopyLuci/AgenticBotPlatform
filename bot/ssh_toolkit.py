"""Python wrapper around the SSH Toolkit (https://github.com/LoopyLuci/SSH_Toolkit) -
a separately maintained PowerShell tool, vendored here as a git submodule at
`vendor/ssh_toolkit`, for creating/managing/visualizing named SSH connections between
machines. This module never reimplements any of that logic: every function here shells
out to the submodule's own `bin/ssh-toolkit.ps1 -Action ... -Json` and parses the
result, so ABP and the standalone toolkit can never drift out of behavioral sync with
each other - a bug fixed upstream is fixed here the moment the submodule is updated
(see `check_update`/`apply_update` below), with no ABP code change needed.

Exposed to bots as a set of MCP tools would be a natural next step; today it backs
`GET/POST /api/ssh-toolkit/*` (bot/dashboard/server.py), `abp_cli ssh ...`, and
`bot/tui/screens/ssh_toolkit.py`.

**Fails closed, cleanly.** `is_available()` is false (and every other function raises
`SshToolkitError`) when either the submodule isn't checked out (a fresh clone without
`git submodule update --init`) or no PowerShell (`pwsh` or, on Windows, `powershell`)
can be found - never a confusing traceback three layers down.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import shutil
import time
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("bot.ssh_toolkit")

_NO_WINDOW = 0x08000000  # CREATE_NO_WINDOW - see bot/desktop.py's/bot/firewall.py's same convention


class SshToolkitError(Exception):
    """A toolkit call that failed in a way the caller should be told about - never a
    crash three layers down. Includes "not installed"/"no PowerShell found"."""


def _code_root() -> Path:
    from bot.envfile import CODE_ROOT

    return CODE_ROOT


def _toolkit_dir() -> Path:
    return _code_root() / "vendor" / "ssh_toolkit"


def _script_path() -> Path:
    return _toolkit_dir() / "bin" / "ssh-toolkit.ps1"


def _powershell_binary() -> Optional[str]:
    # pwsh (PowerShell 7+, cross-platform) first, then Windows' own powershell.exe -
    # same preference order the rest of ABP has no existing convention for (this is the
    # first PowerShell-shelling-out code in the codebase), chosen because pwsh is what
    # the toolkit's own README documents as the primary supported runtime.
    return shutil.which("pwsh") or shutil.which("powershell")


def is_available() -> tuple[bool, str]:
    """(available, reason). reason explains why not, when it isn't - shown as-is in the
    GUI/TUI/CLI rather than a generic "unavailable"."""
    if not _script_path().exists():
        return False, ("the ssh_toolkit submodule isn't checked out - run "
                       "'git submodule update --init vendor/ssh_toolkit'")
    if _powershell_binary() is None:
        return False, "no PowerShell found (pwsh, or powershell.exe on Windows)"
    return True, ""


async def _run(args: list[str], *, json_output: bool = True, timeout: float = 30.0,
               allow_nonzero_exit: bool = False) -> Any:
    """allow_nonzero_exit=True is for actions where a non-zero exit is a normal,
    meaningful result (e.g. -Action Test exits 1 for "unreachable", not "this call
    failed") - the JSON on stdout is still parsed and returned instead of raising."""
    ok, reason = is_available()
    if not ok:
        raise SshToolkitError(f"SSH Toolkit is not available: {reason}")
    ps = _powershell_binary()
    argv = [ps, "-NoProfile", "-NonInteractive", "-File", str(_script_path()), *args]
    if json_output:
        argv.append("-Json")
    child_env = dict(os.environ)
    # Test isolation, same convention as abp_agenteval's ABP_AGENT_TRACE_DB/
    # ABP_AGENT_STATE_DIR: with this set, the child PowerShell process's own $HOME
    # (and therefore ~/.ssh-toolkit and ~/.ssh/config) points at a throwaway
    # directory instead of the real one - a test must never write to a real machine's
    # SSH config. $HOME covers pwsh (Linux/macOS and Windows); USERPROFILE covers
    # Windows PowerShell 5.1, which derives $HOME from it.
    test_home = os.environ.get("ABP_SSH_TOOLKIT_HOME")
    if test_home:
        child_env["HOME"] = test_home
        child_env["USERPROFILE"] = test_home
        # Install-SshLinkTrustedKey's administrator-account path writes under
        # %ProgramData% (a real, machine-wide location Windows sshd itself
        # reads from - it can't live under $HOME), which $HOME/$USERPROFILE
        # above don't touch at all. Without this, a test running on an
        # administrator account either fails needing real elevation or, if it
        # had elevation, would actually mutate the real machine's
        # C:\ProgramData\ssh\administrators_authorized_keys - neither of
        # which a test may ever do.
        child_env["ProgramData"] = test_home
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            env=child_env, creationflags=_NO_WINDOW if os.name == "nt" else 0,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError as exc:
        with contextlib.suppress(ProcessLookupError, OSError):
            proc.kill()
        raise SshToolkitError(f"timed out after {timeout:.0f}s running: {' '.join(args)}") from exc
    except OSError as exc:
        raise SshToolkitError(f"could not start PowerShell: {exc}") from exc
    out_text = stdout.decode(errors="replace").strip()
    err_text = stderr.decode(errors="replace").strip()
    if proc.returncode != 0 and not allow_nonzero_exit:
        raise SshToolkitError(err_text or out_text or f"exited with code {proc.returncode}")
    if not json_output:
        return out_text
    if not out_text:
        return None
    try:
        return json.loads(out_text)
    except json.JSONDecodeError as exc:
        raise SshToolkitError(f"unexpected (non-JSON) output: {out_text[:500]!r}") from exc


# ---------------------------------------------------------------- connections
async def list_connections() -> list[dict]:
    result = await _run(["-Action", "List"])
    return result if isinstance(result, list) else ([] if result is None else [result])


async def get_connection(name: str) -> dict:
    return await _run(["-Action", "Show", "-Name", name])


async def add_connection(name: str, host_name: str, *, port: int = 22, user: Optional[str] = None,
                         identity_file: Optional[str] = None, generate_key: bool = False,
                         proxy_jump: Optional[str] = None, tags: Optional[str] = None,
                         multiplex: bool = False, force: bool = False) -> None:
    args = ["-Action", "Add", "-Name", name, "-HostName", host_name, "-Port", str(port)]
    if user:
        args += ["-User", user]
    if identity_file:
        args += ["-IdentityFile", identity_file]
    if generate_key:
        args.append("-GenerateKey")
    if proxy_jump:
        args += ["-ProxyJump", proxy_jump]
    if tags:
        args += ["-Tags", tags]
    if multiplex:
        args.append("-Multiplex")
    if force:
        args.append("-Force")
    await _run(args, json_output=False)


async def remove_connection(name: str) -> None:
    await _run(["-Action", "Remove", "-Name", name, "-Force"], json_output=False)


async def test_connection(name: str, *, timeout: float = 15.0) -> bool:
    try:
        result = await _run(["-Action", "Test", "-Name", name], timeout=timeout, allow_nonzero_exit=True)
    except SshToolkitError:
        raise
    if isinstance(result, dict):
        return bool(result.get("Reachable"))
    return False


async def run_command(name: str, command: str, *, timeout: float = 30.0) -> str:
    """Runs one remote command over a registered connection and returns its output -
    not an interactive session (there's no terminal to be interactive with here)."""
    return await _run(["-Action", "Connect", "-Name", name, "-Command", command],
                      json_output=False, timeout=timeout)


async def generate_keypair(name: str) -> dict:
    """Generates (or reuses) an ed25519 keypair for `name` without registering
    a connection for it yet - see SSH Toolkit's own New-SshLinkKeypair for why
    this is separate from add_connection(..., generate_key=True): a caller
    (bot/peers.py's peer-pairing handshake) needs its own public key to hand
    to the other side before it knows enough to register a full connection
    (the username to log in as, which the other side hasn't said yet).
    Returns {"identity_file": str, "public_key": str, "created": bool}."""
    result = await _run(["-Action", "GenerateKeypair", "-Name", name])
    return {
        "identity_file": result["IdentityFile"],
        "public_key": result["PublicKey"],
        "created": bool(result["Created"]),
    }


async def install_trusted_key(public_key: str) -> dict:
    """Trusts an already-received public key locally - no SSH session, no
    password prompt, safe to call unattended - for a key that arrived through
    some other already-authenticated channel (the peer-pairing handshake's
    own HTTPS call, gated by a one-time pairing token) rather than a real SSH
    session the way install_public_key-style tooling normally works.
    Returns {"key_file": str, "already_present": bool}."""
    result = await _run(["-Action", "InstallTrustedKey", "-PublicKey", public_key])
    return {"key_file": result["KeyFile"], "already_present": bool(result["AlreadyPresent"])}


def _ssh_binary() -> Optional[str]:
    return shutil.which("ssh")


async def stream_command(name: str, command: str, *, timeout: Optional[float] = None):
    """Runs one remote command over a registered connection (the same `~/.ssh/config`
    Host alias Add-SshLinkConnection wrote - real ssh, no PowerShell wrapper in this
    path, since streaming needs to see each line the moment it arrives, not a JSON blob
    after the whole thing finishes) and yields structured events as they happen:
    {"type": "start", ...} once, then any number of {"type": "stdout"/"stderr", "text":
    line}, then exactly one {"type": "exit", "code": returncode} - never raw video/
    terminal bytes, so a caller (the session monitor, a recorder, a live GUI feed) gets
    something it can render and log meaningfully rather than a screen to look at.

    This is an async generator: iterate it with `async for event in stream_command(...)`.
    """
    ssh_bin = _ssh_binary()
    if not ssh_bin:
        raise SshToolkitError("no ssh client found on PATH")
    argv = [ssh_bin, name, command]
    start_ts = time.time()
    yield {"type": "start", "ts": start_ts, "connection": name, "command": command}

    proc = await asyncio.create_subprocess_exec(
        *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        creationflags=_NO_WINDOW if os.name == "nt" else 0,
    )

    queue: asyncio.Queue = asyncio.Queue()
    _DONE = object()

    async def _pump(stream: asyncio.StreamReader, event_type: str) -> None:
        while True:
            line = await stream.readline()
            if not line:
                break
            await queue.put({"type": event_type, "ts": time.time(),
                             "text": line.decode(errors="replace").rstrip("\r\n")})
        await queue.put(_DONE)

    pumps = [
        asyncio.create_task(_pump(proc.stdout, "stdout")),
        asyncio.create_task(_pump(proc.stderr, "stderr")),
    ]

    async def _drain():
        done_count = 0
        while done_count < len(pumps):
            item = await queue.get()
            if item is _DONE:
                done_count += 1
                continue
            yield item

    try:
        if timeout:
            deadline = start_ts + timeout
            async for event in _drain():
                yield event
                if time.time() > deadline:
                    with contextlib.suppress(ProcessLookupError, OSError):
                        proc.kill()
                    yield {"type": "exit", "ts": time.time(), "code": None, "error": "timed out"}
                    return
        else:
            async for event in _drain():
                yield event
        code = await proc.wait()
        yield {"type": "exit", "ts": time.time(), "code": code}
    finally:
        for p in pumps:
            p.cancel()


_METRICS_PROBE_WINDOWS = (
    "wmic cpu get loadpercentage /value & "
    "wmic OS get FreePhysicalMemory,TotalVisibleMemorySize /value"
)


def _parse_metrics_probe(output: str) -> dict:
    values: dict[str, str] = {}
    for line in output.splitlines():
        if "=" in line:
            k, _, v = line.partition("=")
            k, v = k.strip(), v.strip()
            if k and v:
                values[k] = v
    metrics: dict[str, Any] = {}
    if "LoadPercentage" in values:
        with contextlib.suppress(ValueError):
            metrics["cpu_percent"] = float(values["LoadPercentage"])
    if "FreePhysicalMemory" in values and "TotalVisibleMemorySize" in values:
        with contextlib.suppress(ValueError, ZeroDivisionError):
            free_kb, total_kb = float(values["FreePhysicalMemory"]), float(values["TotalVisibleMemorySize"])
            metrics["mem_used_percent"] = round((1 - free_kb / total_kb) * 100, 1)
            metrics["mem_total_mb"] = round(total_kb / 1024, 1)
    return metrics


async def probe_metrics(name: str, *, timeout: float = 10.0) -> dict:
    """One lightweight CPU/memory snapshot of the connection's remote machine - a
    quick, separate command (fast to run repeatedly over the toolkit's own SSH
    multiplexing/ControlMaster support), not a persistent remote agent. Windows-only
    probe today, matching the machines this feature was built and demoed against;
    returns {} rather than raising when the probe command itself fails or the target
    isn't Windows, since a monitor session's live command output matters far more than
    one missed metrics tick."""
    ssh_bin = _ssh_binary()
    if not ssh_bin:
        return {}
    try:
        proc = await asyncio.create_subprocess_exec(
            ssh_bin, name, _METRICS_PROBE_WINDOWS,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            creationflags=_NO_WINDOW if os.name == "nt" else 0,
        )
        stdout, _stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except (asyncio.TimeoutError, OSError):
        return {}
    if proc.returncode != 0:
        return {}
    return _parse_metrics_probe(stdout.decode(errors="replace"))


async def status_all() -> list[dict]:
    result = await _run(["-Action", "TestAll"])
    return result if isinstance(result, list) else ([] if result is None else [result])


async def graph() -> list[dict]:
    """The proxy-jump tree with live status - {Connection, Depth, Reachable} per node,
    same shape the toolkit's own Get-SshLinkGraph returns, for building a visualization."""
    result = await _run(["-Action", "Visualize"])
    return result if isinstance(result, list) else ([] if result is None else [result])


# --------------------------------------------------------------------- update
async def check_update() -> dict:
    return await _run(["-Action", "CheckUpdate"])


async def apply_update() -> dict:
    """Updates the vendored submodule's OWN files in place (git pull inside
    vendor/ssh_toolkit, since that's a git submodule checkout) - never touches this
    machine's ~/.ssh-toolkit registry or ~/.ssh/config."""
    return await _run(["-Action", "Update"])


AUTO_UPDATE_MODES = ("never", "notify", "auto")
DEFAULT_AUTO_UPDATE_MODE = "never"


def get_auto_update_mode() -> str:
    """The global (machine-wide, not per-instance) setting controlling how a
    submodule/sidecar install of this toolkit picks up new releases -
    "never" (manual only, the default), "notify" (a background check logs
    availability but never applies it), or "auto" (the background check
    applies it immediately). Stored the same way as swarm_budget - a plain
    dict under config.current, hot-reloaded, no schema enforcement."""
    from bot.config import config

    cfg = config.current.get("ssh_toolkit") or {}
    mode = cfg.get("auto_update", DEFAULT_AUTO_UPDATE_MODE)
    return mode if mode in AUTO_UPDATE_MODES else DEFAULT_AUTO_UPDATE_MODE


def set_auto_update_mode(mode: str, *, actor: str = "dashboard") -> None:
    if mode not in AUTO_UPDATE_MODES:
        raise SshToolkitError(f"invalid auto_update mode {mode!r} - must be one of {AUTO_UPDATE_MODES}")
    from bot.config import config

    config.set_value(["ssh_toolkit", "auto_update"], mode, actor=actor)


async def run_auto_update_check() -> Optional[dict]:
    """One cycle of the background auto-update check - called periodically from
    bot/dashboard/server.py's lifespan task. Returns None when mode is "never" or the
    toolkit itself isn't available (nothing to do), otherwise the check_update()/
    apply_update() result actually acted on."""
    mode = get_auto_update_mode()
    if mode == "never":
        return None
    available, _reason = is_available()
    if not available:
        return None
    try:
        check = await check_update()
    except SshToolkitError as exc:
        logger.warning("ssh_toolkit auto-update check failed: %s", exc)
        return None
    if not check.get("UpdateAvailable"):
        return None
    if mode == "notify":
        logger.info(
            "SSH Toolkit update available: %s -> %s (auto_update=notify, not applying)",
            check.get("InstalledVersion"), check.get("LatestVersion"),
        )
        return check
    try:
        result = await apply_update()
    except SshToolkitError as exc:
        logger.warning("ssh_toolkit auto-update apply failed: %s", exc)
        return None
    logger.info("SSH Toolkit auto-updated to %s", result.get("Version"))
    return result
