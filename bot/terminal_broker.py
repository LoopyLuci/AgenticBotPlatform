"""Interactive terminals for containers and VMs, over WebSocket.

This is deliberately NOT a general remote shell. A session can only be one of:
  container : `docker exec -it <container> <sh|bash|ash|zsh|auto>` in a real PTY
  vm-serial : the serial console of an ABP-defined QEMU VM (a localhost TCP socket QEMU serves)
  vm-monitor: an interactive QEMU monitor prompt (runs through QMP's human-monitor-command)
  libvirt   : `virsh console <domain>` in a real PTY
Every argv is built here from validated names - callers never supply a command line - and each
session is audited, capped in number, and reaped when idle or disconnected.

A PTY is ConPTY via pywinpty on Windows and the stdlib `pty` elsewhere.
"""
from __future__ import annotations

import os
import shutil
import socket
import threading
import time
import uuid
from typing import Optional

from bot import db, docker_mgr as dk, vm_mgr as vm

MAX_SESSIONS = 8
IDLE_SECONDS = 30 * 60
SHELLS = {"auto", "sh", "bash", "ash", "zsh"}
_AUTO = "command -v bash >/dev/null 2>&1 && exec bash || exec sh"


class TerminalError(Exception):
    pass


def container_argv(container: str, shell: str = "auto", user: Optional[str] = None) -> list[str]:
    exe = shutil.which("docker")
    if not exe:
        raise TerminalError("Docker is not installed on this machine")
    if shell not in SHELLS:
        raise TerminalError(f"shell must be one of {sorted(SHELLS)}")
    argv = [exe, "exec", "-it", "-e", "TERM=xterm-256color"]
    if user:
        argv += ["-u", dk._name(user, "user")]
    argv.append(dk._name(container, "container"))
    argv += ["sh", "-c", _AUTO] if shell == "auto" else [shell]
    return argv


def libvirt_argv(domain: str) -> list[str]:
    exe = shutil.which("virsh")
    if not exe:
        raise TerminalError("libvirt (virsh) is not installed on this machine")
    return [exe, "console", vm._name(domain)]


class Session:
    kind = ""
    target = ""

    def __init__(self) -> None:
        self.id = uuid.uuid4().hex[:12]
        self.created = time.time()
        self.last_active = self.created
        self.closed = False

    def read(self, timeout: float = 0.5) -> Optional[str]:
        raise NotImplementedError

    def write(self, data: str) -> None:
        raise NotImplementedError

    def resize(self, cols: int, rows: int) -> None:
        pass

    def alive(self) -> bool:
        return not self.closed

    def close(self) -> None:
        self.closed = True


class PtySession(Session):
    def __init__(self, kind: str, target: str, argv: list[str], cols: int, rows: int) -> None:
        super().__init__()
        self.kind, self.target = kind, target
        cols, rows = max(20, min(cols, 500)), max(5, min(rows, 200))
        if os.name == "nt":
            from winpty import PtyProcess  # pywinpty

            self._proc = PtyProcess.spawn(argv, dimensions=(rows, cols))
            self._win = True
        else:
            import pty
            import subprocess

            master, slave = pty.openpty()
            self._proc = subprocess.Popen(argv, stdin=slave, stdout=slave, stderr=slave, close_fds=True,
                                          start_new_session=True)
            os.close(slave)
            self._fd = master
            self._win = False
            self.resize(cols, rows)

    def read(self, timeout: float = 0.5) -> Optional[str]:
        if self._win:
            try:
                data = self._proc.read(65536)
            except EOFError:
                self.closed = True
                return None
            if data:
                return data
            return "" if self._proc.isalive() else None
        import select

        ready, _, _ = select.select([self._fd], [], [], timeout)
        if not ready:
            return "" if self._proc.poll() is None else None
        try:
            chunk = os.read(self._fd, 65536)
        except OSError:
            self.closed = True
            return None
        return chunk.decode("utf-8", "replace") if chunk else None

    def write(self, data: str) -> None:
        self.last_active = time.time()
        if self._win:
            self._proc.write(data)
        else:
            os.write(self._fd, data.encode())

    def resize(self, cols: int, rows: int) -> None:
        cols, rows = max(20, min(cols, 500)), max(5, min(rows, 200))
        if self._win:
            self._proc.setwinsize(rows, cols)
        else:
            import fcntl
            import struct
            import termios

            fcntl.ioctl(self._fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))

    def alive(self) -> bool:
        if self.closed:
            return False
        return self._proc.isalive() if self._win else self._proc.poll() is None

    def close(self) -> None:
        if self.closed and not self.alive():
            return
        self.closed = True
        try:
            if self._win:
                self._proc.terminate(force=True)
            else:
                self._proc.kill()
                os.close(self._fd)
        except Exception:
            pass


class SocketSession(Session):
    """A raw byte stream to a localhost TCP port (QEMU's serial console)."""

    def __init__(self, kind: str, target: str, port: int) -> None:
        super().__init__()
        self.kind, self.target = kind, target
        try:
            self._sock = socket.create_connection(("127.0.0.1", port), timeout=5)
        except OSError as exc:
            raise TerminalError(f"could not open the VM's serial console: {exc}") from exc
        self._sock.settimeout(0.5)

    def read(self, timeout: float = 0.5) -> Optional[str]:
        try:
            chunk = self._sock.recv(65536)
        except socket.timeout:
            return ""
        except OSError:
            self.closed = True
            return None
        if not chunk:
            self.closed = True
            return None
        return chunk.decode("utf-8", "replace")

    def write(self, data: str) -> None:
        self.last_active = time.time()
        try:
            self._sock.sendall(data.encode())
        except OSError:
            self.closed = True

    def close(self) -> None:
        self.closed = True
        try:
            self._sock.close()
        except OSError:
            pass


class MonitorSession(Session):
    """A line-edited prompt for the QEMU human monitor. Each Enter runs the line through QMP."""

    def __init__(self, target: str) -> None:
        super().__init__()
        self.kind, self.target = "vm-monitor", target
        if not vm.qemu_status(target)["running"]:
            raise TerminalError("the VM is not running")
        self._buf = ""
        self._out: list[str] = [f"QEMU monitor for {target} - type a command (try: info status, info block, help)\r\n(qemu) "]
        self._lock = threading.Lock()

    def _emit(self, text: str) -> None:
        with self._lock:
            self._out.append(text)

    def read(self, timeout: float = 0.5) -> Optional[str]:
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self._lock:
                if self._out:
                    data, self._out = "".join(self._out), []
                    return data
            if self.closed:
                return None
            time.sleep(0.05)
        return ""

    def write(self, data: str) -> None:
        self.last_active = time.time()
        for ch in data:
            if ch in ("\r", "\n"):
                line, self._buf = self._buf.strip(), ""
                self._emit("\r\n")
                if line in ("quit", "exit"):
                    self.closed = True
                    return
                if line:
                    try:
                        out = vm.qemu_monitor(self.target, line)["output"]
                    except vm.VMError as exc:
                        out = f"error: {exc}"
                    self._emit(out.replace("\r\n", "\n").replace("\n", "\r\n") + ("\r\n" if out and not out.endswith("\n") else ""))
                self._emit("(qemu) ")
            elif ch in ("\x7f", "\b"):
                if self._buf:
                    self._buf = self._buf[:-1]
                    self._emit("\b \b")
            elif ch == "\x03":
                self._buf = ""
                self._emit("^C\r\n(qemu) ")
            elif ch.isprintable():
                self._buf += ch
                self._emit(ch)


_sessions: dict[str, Session] = {}
_lock = threading.Lock()


def _reap() -> None:
    now = time.time()
    for sid, s in list(_sessions.items()):
        if not s.alive() or now - s.last_active > IDLE_SECONDS:
            s.close()
            _sessions.pop(sid, None)


def open_session(kind: str, target: str, *, cols: int = 80, rows: int = 24, shell: str = "auto",
                 user: Optional[str] = None) -> Session:
    with _lock:
        _reap()
        if len(_sessions) >= MAX_SESSIONS:
            raise TerminalError(f"too many open terminals (max {MAX_SESSIONS}); close one first")
        if kind == "container":
            s: Session = PtySession(kind, target, container_argv(target, shell, user), cols, rows)
        elif kind == "libvirt":
            s = PtySession(kind, target, libvirt_argv(target), cols, rows)
        elif kind == "vm-serial":
            spec = vm._load(target)
            port = spec.get("serial_port")
            if not vm.qemu_status(target)["running"] or not port:
                raise TerminalError("the VM is not running with a serial console (start it first)")
            s = SocketSession(kind, target, int(port))
        elif kind == "vm-monitor":
            s = MonitorSession(target)
        else:
            raise TerminalError("kind must be container, vm-serial, vm-monitor or libvirt")
        _sessions[s.id] = s
    db.log_audit(actor="dashboard", action="terminal_open", detail=f"{kind}:{target}")
    return s


def close_session(s: Session) -> None:
    s.close()
    with _lock:
        _sessions.pop(s.id, None)


def list_sessions() -> list[dict]:
    with _lock:
        _reap()
        return [{"id": s.id, "kind": s.kind, "target": s.target, "created": s.created,
                 "idle_s": int(time.time() - s.last_active)} for s in _sessions.values()]
