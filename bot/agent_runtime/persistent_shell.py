"""A shell that keeps `cd` and exported variables between run_shell calls (roadmap P1).

Opt-in: `run_shell` takes `persistent: true`, or `native_agent.shell.persistent: true` makes it the
default. Without either, every call is a fresh shell exactly as before.

    native_agent:
      shell:
        persistent: false       # default for run_shell calls that don't say
        idle_minutes: 30        # close a session's shell after this long unused

**How.** No PTY. One long-lived shell per (session, working directory): PowerShell on Windows (pwsh
if present, else powershell.exe), bash elsewhere. It runs a small driver loop that reads one request
per line on stdin, `<base64 of the command> <marker>`, runs the command in its own global scope, then
prints `\\n<marker>|<exit code>|<cwd>\\n`. The host reads stdout and stderr (one pipe) until that line.
The marker is 128 random bits chosen per call and is never shown to the model, so a command whose
output merely looks like a marker line cannot end the call early. The command text is sent base64
encoded, so quoting, newlines and here-strings never touch the driver's own syntax.

**Same sandbox.** The shell is started through `sandbox`: the same scrubbed environment, the same cell
(job object / process group) and the same kill. The permission rules still see the command text because
the tool is still `run_shell`. A backend that can't host a long-lived process (docker, ssh, wsl) or
`network: none` (an AppContainer / network namespace per command) falls back to the stateless call with
a one-line note; nothing is weakened.

**Timeout.** The per-call timeout stays. On timeout the whole tree is killed and the session dropped;
the next call starts a fresh shell and says so.

**Workspace.** If a command leaves the working directory with `cd`, the call reports it, the next call
is refused once and the shell is put back in the working directory.

**Limits.** A command that reads stdin gets no input and will wait until the timeout. The marker lives in
the shell's own memory, so a command that deliberately inspects the driver's variables could forge it;
that can only confuse that session's own output. Not covered: POSIX (written, untested here).
"""

from __future__ import annotations

import asyncio
import atexit
import base64
import os
import secrets
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from bot.agent_runtime import sandbox, toolspec
from bot.agent_runtime.errors import ToolError

MAX_CAPTURE_BYTES = 400_000
DEFAULT_IDLE_MINUTES = 30
_TAIL = 8192                      # how much of the newest output is searched for the end marker

_POWERSHELL_DRIVER = r"""
$ErrorActionPreference = 'Continue'
$ProgressPreference = 'SilentlyContinue'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$__in = [Console]::In
while ($true) {
  $__line = $__in.ReadLine()
  if ($null -eq $__line) { break }
  $__p = $__line.Split(' ')
  $__mk = $__p[1]
  $__cmd = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($__p[0]))
  $global:LASTEXITCODE = 0
  $__ok = $true
  $__out = $null
  try { $__out = . ([scriptblock]::Create($__cmd)) *>&1; $__ok = $? } catch { $__out = @($_); $__ok = $false }
  # a cmdlet's non-terminating error does not always clear $?; a native program's stderr must not count as failure
  if ($__ok -and @($__out | Where-Object { $_ -is [System.Management.Automation.ErrorRecord] -and $_.FullyQualifiedErrorId -notlike 'NativeCommandError*' }).Count -gt 0) { $__ok = $false }
  if ($null -ne $__out) {
    $__txt = ($__out | ForEach-Object { if ($_ -is [System.Management.Automation.ErrorRecord]) { $_.ToString() } else { $_ } } | Out-String -Width 4096)
    [Console]::Out.Write($__txt.TrimEnd("`r", "`n"))
  }
  $__rc = 0
  if (-not $__ok) { $__rc = 1 }
  if ($null -ne $LASTEXITCODE -and $LASTEXITCODE -ne 0) { $__rc = $LASTEXITCODE }
  [Console]::Out.Write("`n" + $__mk + "|" + $__rc + "|" + (Get-Location).ProviderPath + "`n")
  [Console]::Out.Flush()
}
"""

_BASH_DRIVER = r"""
while IFS= read -r __line; do
  __mk=${__line#* }
  __cmd=$(printf %s "${__line%% *}" | base64 -d)
  eval "$__cmd" </dev/null 2>&1
  __rc=$?
  printf '\n%s|%s|%s\n' "$__mk" "$__rc" "$PWD"
done
"""


@dataclass
class Session:
    key: tuple
    proc: asyncio.subprocess.Process
    loop: asyncio.AbstractEventLoop
    workspace: Path
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    last_used: float = field(default_factory=time.monotonic)
    escaped: Optional[str] = None          # cwd the last command ended in, if outside the workspace


_sessions: dict[tuple, Session] = {}
_notes: dict[tuple, str] = {}              # why the next call starts a fresh shell
_watch: Optional[asyncio.Task] = None


# ---- configuration -----------------------------------------------------------------
def _config() -> dict:
    try:
        from bot.config import config

        return ((config.current.get("native_agent") or {}).get("shell")) or {}
    except Exception:  # noqa: BLE001
        return {}


def default_persistent() -> bool:
    return bool(_config().get("persistent", False))


def idle_seconds() -> float:
    try:
        minutes = float(_config().get("idle_minutes", DEFAULT_IDLE_MINUTES))
    except (TypeError, ValueError):
        minutes = DEFAULT_IDLE_MINUTES
    return max(minutes, 0.0) * 60


# ---- which shell, and can the sandbox host it --------------------------------------
def _driver_argv() -> Optional[list[str]]:
    if os.name == "nt":
        exe = shutil.which("pwsh") or shutil.which("powershell")
        if not exe:
            return None
        encoded = base64.b64encode(_POWERSHELL_DRIVER.encode("utf-16-le")).decode("ascii")
        return [exe, "-NoLogo", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded]
    bash = shutil.which("bash")
    if not bash:
        return None
    return [bash, "--noprofile", "--norc", "-c", _BASH_DRIVER]


def unsupported_reason() -> Optional[str]:
    """None if a persistent shell can run under the configured sandbox, else a one-line reason."""
    which = sandbox.backend()
    if which not in ("local", "windows_job"):
        return f"the {which} sandbox can't keep a shell open between calls"
    if sandbox.network() != "allow":
        return "network: none / allowlist runs every command in its own confined process, so a shell can't be kept open"
    if _driver_argv() is None:
        return "no PowerShell (Windows) or bash was found to keep open"
    return None


# ---- sessions ----------------------------------------------------------------------
def _key(workspace: Path) -> tuple:
    return (toolspec.current_session(), str(Path(workspace).resolve()))


def _kill(sess: Session) -> None:
    _sessions.pop(sess.key, None)
    try:
        sandbox.kill(sess.proc)
    except Exception:  # noqa: BLE001
        pass
    try:
        if sess.proc.stdin:
            sess.proc.stdin.close()
    except Exception:  # noqa: BLE001
        pass


def close_session(session: Optional[str] = None) -> int:
    """Close every shell of a session (default: the current one). Returns how many were open."""
    name = session or toolspec.current_session()
    victims = [s for s in _sessions.values() if s.key[0] == name]
    for sess in victims:
        _kill(sess)
    return len(victims)


def close_all() -> int:
    victims = list(_sessions.values())
    for sess in victims:
        _kill(sess)
    return len(victims)


atexit.register(close_all)


def reap_idle(now: Optional[float] = None) -> int:
    limit = idle_seconds()
    now = time.monotonic() if now is None else now
    stale = [s for s in _sessions.values() if not s.lock.locked() and now - s.last_used > limit]
    for sess in stale:
        _notes[sess.key] = "the previous shell was closed after sitting idle"
        _kill(sess)
    return len(stale)


async def _idle_watch() -> None:
    while True:
        await asyncio.sleep(max(1.0, min(60.0, idle_seconds() / 2 or 1.0)))
        reap_idle()
        if not _sessions:
            return


def _ensure_watch() -> None:
    global _watch
    loop = asyncio.get_running_loop()
    if _watch is None or _watch.done() or _watch.get_loop() is not loop:
        _watch = loop.create_task(_idle_watch())


async def _open(workspace: Path, key: tuple) -> Session:
    argv = _driver_argv()
    proc = await sandbox.start_persistent(argv, workspace)
    sess = Session(key=key, proc=proc, loop=asyncio.get_running_loop(), workspace=Path(workspace).resolve())
    _sessions[key] = sess
    _ensure_watch()
    return sess


async def _get(workspace: Path) -> tuple[Session, str]:
    """The live shell for this session and folder, and a note if it is a fresh replacement."""
    key = _key(workspace)
    sess = _sessions.get(key)
    if sess is not None and (sess.loop is not asyncio.get_running_loop() or sess.proc.returncode is not None):
        why = "the previous shell had ended" if sess.proc.returncode is not None else "the previous shell belonged to a finished run"
        _kill(sess)
        _notes.setdefault(key, why)
        sess = None
    if sess is None:
        sess = await _open(workspace, key)
        return sess, _notes.pop(key, "")
    return sess, ""


# ---- running a command -------------------------------------------------------------
def _inside(workspace: Path, cwd: str) -> bool:
    try:
        Path(cwd).resolve().relative_to(Path(workspace).resolve())
        return True
    except (ValueError, OSError):
        return False


def _psq(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"


def _cd_command(path: Path) -> str:
    if os.name == "nt":
        return f"Set-Location -LiteralPath {_psq(str(path))}"
    import shlex

    return f"cd {shlex.quote(str(path))}"


async def _exchange(sess: Session, command: str, limit: float) -> tuple[str, int, str, bool]:
    """Send one command, read until its marker. Returns (output, exit code, cwd, output was capped).
    Raises TimeoutError, EOFError (the shell ended) or CancelledError; the caller drops the session."""
    marker = "ABPEND" + secrets.token_hex(16)
    request = base64.b64encode(command.encode("utf-8")).decode("ascii") + " " + marker + "\n"
    needle = ("\n" + marker + "|").encode()
    head = bytearray()
    tail = b""
    capped = False
    loop = asyncio.get_running_loop()
    deadline = loop.time() + limit
    try:
        sess.proc.stdin.write(request.encode())
        await sess.proc.stdin.drain()
    except (BrokenPipeError, ConnectionResetError, OSError) as exc:
        raise EOFError("the shell is not accepting input") from exc
    while True:
        remaining = deadline - loop.time()
        if remaining <= 0:
            raise asyncio.TimeoutError()
        data = await asyncio.wait_for(sess.proc.stdout.read(65536), timeout=remaining)
        if not data:
            await sess.proc.wait()
            raise EOFError(bytes(head).decode(errors="replace"))
        if len(head) < MAX_CAPTURE_BYTES:
            head.extend(data)
        elif not capped:
            capped = True
        tail = (tail + data)[-_TAIL:]
        pos = tail.find(needle)
        if pos < 0:
            continue
        rest = tail[pos + len(needle):]
        if b"\n" not in rest:
            continue
        line = rest.split(b"\n", 1)[0].decode(errors="replace").rstrip("\r")
        code_text, _, cwd = line.partition("|")
        try:
            code = int(code_text)
        except ValueError:
            code = -1
        raw = bytes(head)
        idx = raw.find(needle)
        if idx >= 0 and not capped:
            raw = raw[:idx]
        else:
            capped = True
            raw = raw[:MAX_CAPTURE_BYTES]
        return raw.decode(errors="replace"), code, cwd, capped


async def run(command: str, *, workspace: Path, cwd: Optional[Path], limit: int) -> str:
    """Run `command` in the session's persistent shell. `cwd` (already validated, inside the
    workspace) is entered first when given."""
    sess, fresh_note = await _get(workspace)
    async with sess.lock:
        sess.last_used = time.monotonic()
        prefix = f"[new shell started: {fresh_note}; cd and variables from earlier calls are gone]\n" if fresh_note else ""
        try:
            if sess.escaped:
                gone = sess.escaped
                sess.escaped = None
                await _exchange(sess, _cd_command(sess.workspace), 10)
                raise ToolError(f"the previous command left the working directory (the shell was in {gone}). "
                                f"The shell has been put back in {sess.workspace}; run the command again.")
            if cwd is not None and Path(cwd).resolve() != sess.workspace:
                await _exchange(sess, _cd_command(Path(cwd)), 10)
            text, code, where, capped = await _exchange(sess, command, limit)
        except asyncio.TimeoutError as exc:
            _kill(sess)
            _notes[sess.key] = f"the previous shell was stopped after {limit}s"
            raise ToolError(f"timed out after {limit}s and was stopped, with the shell and its state (cd, variables). "
                            "The next call starts a fresh shell. Use background=true for long-running commands.") from exc
        except EOFError as exc:
            _kill(sess)
            _notes[sess.key] = "the previous shell exited"
            tail = str(exc)[-3000:]
            raise ToolError("the shell exited (the command ended it, for example with exit). The next call starts a "
                            "fresh shell." + (f" Output so far:\n{tail}" if tail.strip() else "")) from exc
        except asyncio.CancelledError:
            _kill(sess)
            _notes[sess.key] = "the previous call was cancelled"
            raise
        sess.last_used = time.monotonic()
        extra = ""
        if capped:
            extra = f"\n... (output capped at {MAX_CAPTURE_BYTES} bytes)"
        if where and not _inside(sess.workspace, where):
            sess.escaped = where
            extra += (f"\n[warning: the shell is now in {where}, outside the working directory. "
                      "The next call will be refused and the shell put back.]")
        return f"{prefix}{text}{extra}\n[exit code {code}, cwd {where}]"
