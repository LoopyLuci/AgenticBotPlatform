"""Ephemeral per-job WSL2 micro-VM (sandbox backend `wsl_ephemeral`).

Unlike the `wsl` backend (a shared persistent distro that auto-mounts every Windows drive),
each command here gets its own throw-away distro imported from a cached base rootfs:

1. `wsl --import` a uniquely named distro under data/wsl_vm/<uuid>.
2. Write /etc/wsl.conf (automount off, interop off, no PATH leak, no generated resolv.conf)
   and `wsl --terminate` so it takes effect: no /mnt host drives, no Windows interop.
3. Copy the workspace INTO /work (a tar stream; the guest never sees a host path), run the
   command under `unshare -rn` (no network), copy /work back OUT.
4. In a finally: terminate, unregister, delete the temp dir. Nothing survives an error or timeout.

It runs as a helper process (`python -m bot.agent_runtime.wsl_vm run ...`) so sandbox.start()
can hand back an ordinary Process whose stdout is the guest's output. Copy-back replaces and
adds files; it never deletes host files, and refuses links, devices and paths outside the workspace.
Fails closed: no WSL or no base image means ToolError, never a host run.
"""
from __future__ import annotations

import io
import os
import shlex
import shutil
import subprocess
import sys
import tarfile
import threading
import uuid
from pathlib import Path, PurePosixPath
from typing import Optional

if __name__ == "__main__":      # run as a script by sandbox.start(): -I drops the path, so add the project root
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from bot.agent_runtime.errors import ToolError  # noqa: E402

WSL_CONF = ("[automount]\nenabled=false\nmountFsTab=false\n\n"
            "[interop]\nenabled=false\nappendWindowsPath=false\n\n"
            "[network]\ngenerateResolvConf=false\n")
GUEST_WORK = "/work"
NAME_PREFIX = "abp-vm-"
_NOWIN = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _root() -> Path:
    from bot.envfile import PROJECT_ROOT
    return Path(PROJECT_ROOT) / "data"


def vm_root() -> Path:
    return _root() / "wsl_vm"


def base_tar_path() -> Path:
    return _root() / "wsl_base" / "base.tar"


def wsl_exe() -> Optional[str]:
    return shutil.which("wsl.exe") if os.name == "nt" else None


def _wsl(*args: str, timeout: float = 120, input: Optional[bytes] = None) -> subprocess.CompletedProcess:
    exe = wsl_exe()
    if not exe:
        raise ToolError("sandbox.backend is wsl_ephemeral but wsl.exe was not found; the command was not run")
    return subprocess.run([exe, *args], capture_output=True, input=input, timeout=timeout, creationflags=_NOWIN)


def _decode(b: bytes) -> str:
    # wsl.exe management commands print UTF-16LE
    if b"\x00" in b:
        return b.decode("utf-16-le", "ignore").replace("﻿", "")
    return b.decode("utf-8", "ignore")


def list_distros() -> list[str]:
    r = _wsl("-l", "-q", timeout=30)
    if r.returncode != 0:
        raise ToolError("WSL is not available (wsl.exe -l -q failed); the command was not run")
    return [ln.strip() for ln in _decode(r.stdout).splitlines() if ln.strip()]


def available() -> bool:
    try:
        return bool(wsl_exe()) and _wsl("-l", "-q", timeout=30).returncode == 0
    except (OSError, subprocess.SubprocessError, ToolError):
        return False


def _distro_size(name: str) -> int:
    """Size of a distro's disk from the registry; unknown sorts last."""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Lxss") as k:
            for i in range(winreg.QueryInfoKey(k)[0]):
                with winreg.OpenKey(k, winreg.EnumKey(k, i)) as sub:
                    if winreg.QueryValueEx(sub, "DistributionName")[0] == name:
                        base = Path(winreg.QueryValueEx(sub, "BasePath")[0].replace("\\\\?\\", ""))
                        return (base / "ext4.vhdx").stat().st_size
    except (OSError, ImportError):
        pass
    return 1 << 62


def pick_base_distro(names: list[str]) -> str:
    cands = [n for n in names if not n.lower().startswith("docker-desktop")]
    if not cands:
        raise ToolError("no installed WSL distro to build the base image from; the command was not run")
    return min(cands, key=_distro_size)


_base_lock = threading.Lock()


def ensure_base() -> Path:
    """The cached base rootfs tarball, exported once from the smallest installed distro."""
    tar = base_tar_path()
    with _base_lock:
        if tar.is_file() and tar.stat().st_size > 0:
            return tar
        distro = pick_base_distro(list_distros())
        tar.parent.mkdir(parents=True, exist_ok=True)
        part = tar.with_suffix(f".{uuid.uuid4().hex[:8]}.part")
        try:
            r = _wsl("--export", distro, str(part), timeout=1800)
            if r.returncode != 0 or not part.is_file():
                raise ToolError(f"could not export {distro!r} as the base image: {_decode(r.stderr).strip()}")
            os.replace(part, tar)
        finally:
            part.unlink(missing_ok=True)
    return tar


def new_job() -> tuple[str, Path]:
    u = uuid.uuid4().hex[:12]
    return f"{NAME_PREFIX}{u}", vm_root() / u


def _guest(name: str, script: str, *, timeout: float = 120, input: Optional[bytes] = None):
    return _wsl("-d", name, "-u", "root", "--exec", "sh", "-c", script, timeout=timeout, input=input)


def guest_script(command: str, rel_cwd: str, memory_mb: int = 0) -> str:
    """The shell run in the guest: /work cwd, no network (unshare -rn fails closed)."""
    wd = GUEST_WORK + (f"/{rel_cwd}" if rel_cwd else "")
    pre = f"ulimit -v {int(memory_mb) * 1024}; " if memory_mb else ""
    return f"cd {shlex.quote(wd)} && {pre}exec unshare -rn sh -c {shlex.quote(command)}"


def provision(name: str, tmp: Path, base: Path) -> None:
    tmp.mkdir(parents=True, exist_ok=True)
    r = _wsl("--import", name, str(tmp), str(base), "--version", "2", timeout=600)
    if r.returncode != 0:      # WSL occasionally answers E_UNEXPECTED under load; one clean retry
        _wsl("--unregister", name, timeout=120)
        r = _wsl("--import", name, str(tmp), str(base), "--version", "2", timeout=600)
    if r.returncode != 0:
        raise ToolError(f"could not create the VM: {_decode(r.stdout + r.stderr).strip()}")
    r = _guest(name, "cat > /etc/wsl.conf && mkdir -p /work", input=WSL_CONF.encode())
    if r.returncode != 0:
        raise ToolError(f"could not configure the VM: {_decode(r.stderr).strip()}")
    _wsl("--terminate", name, timeout=60)        # wsl.conf is read at the next start


def pack_workspace(workspace: Path) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        for p in sorted(workspace.rglob("*")):
            if p.is_symlink() or not (p.is_file() or p.is_dir()):
                continue
            tf.add(p, arcname=p.relative_to(workspace).as_posix(), recursive=False)
    return buf.getvalue()


def unpack_to(data: bytes, workspace: Path) -> int:
    """Extract only plain files and directories whose paths stay inside the workspace."""
    root = workspace.resolve()
    n = 0
    with tarfile.open(fileobj=io.BytesIO(data), mode="r") as tf:
        for m in tf:
            pp = PurePosixPath(m.name)
            if pp.is_absolute() or ".." in pp.parts or ":" in m.name or "\\" in m.name:
                continue
            if not (m.isfile() or m.isdir()):
                continue
            dest = (root / Path(*pp.parts)).resolve() if pp.parts else root
            if dest != root and root not in dest.parents:
                continue
            if m.isdir():
                dest.mkdir(parents=True, exist_ok=True)
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            f = tf.extractfile(m)
            if f is None:
                continue
            with open(dest, "wb") as out:
                shutil.copyfileobj(f, out)
            n += 1
    return n


def teardown(name: str, tmp: Path) -> None:
    """Idempotent: terminate, unregister, delete the temp dir. Never raises."""
    for args in (("--terminate", name), ("--unregister", name)):
        try:
            _wsl(*args, timeout=120)
        except Exception:  # noqa: BLE001
            pass
    shutil.rmtree(tmp, ignore_errors=True)


def run(command: str, workspace: Path, cwd: Path, name: str, tmp: Path, timeout: float = 0, memory_mb: int = 0) -> int:
    """Provision, run, copy back, tear down. Guest output goes to our stdout. Returns the exit code."""
    workspace = Path(workspace).resolve()
    try:
        rel = Path(cwd).resolve().relative_to(workspace).as_posix()
    except ValueError:
        rel = ""
    rel = "" if rel == "." else rel
    code = 1
    try:
        base = ensure_base()
        provision(name, tmp, base)
        r = _guest(name, f"tar -x -C {GUEST_WORK}", input=pack_workspace(workspace))
        if r.returncode != 0:
            raise ToolError(f"could not copy the workspace into the VM: {_decode(r.stderr).strip()}")
        proc = subprocess.Popen([wsl_exe(), "-d", name, "-u", "root", "--exec", "sh", "-c",
                                 guest_script(command, rel, memory_mb)],
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, creationflags=_NOWIN)
        timer = None
        timed_out: list[int] = []
        if timeout:
            def _expire():
                timed_out.append(1)
                proc.kill()
            timer = threading.Timer(timeout, _expire)
            timer.start()
        try:
            for chunk in iter(lambda: proc.stdout.read1(65536), b""):
                sys.stdout.buffer.write(chunk)
                sys.stdout.buffer.flush()
            code = proc.wait()
        finally:
            if timer:
                timer.cancel()
        if timed_out:
            sys.stdout.buffer.write(f"\n[wsl_ephemeral: timed out after {timeout}s]\n".encode())
            code = 124
        else:
            out = _guest(name, f"tar -c -C {GUEST_WORK} .", timeout=600)
            if out.returncode == 0:
                unpack_to(out.stdout, workspace)
    except ToolError as exc:
        sys.stdout.buffer.write(f"{exc}\n".encode())
        code = 1
    finally:
        teardown(name, tmp)
    sys.stdout.buffer.flush()
    return code


def helper_argv(command: str, cwd: Path, workspace: Path, name: str, tmp: Path, timeout: float, memory_mb: int) -> list[str]:
    return [sys.executable, "-I", str(Path(__file__).resolve()), "run", name, str(tmp), str(workspace), str(cwd),
            str(timeout), str(memory_mb), command]


def main(argv: list[str]) -> int:
    if len(argv) != 8 or argv[0] != "run":
        print("usage: wsl_vm run NAME TMP WORKSPACE CWD TIMEOUT MEMORY_MB COMMAND", file=sys.stderr)
        return 2
    _, name, tmp, ws, cwd, timeout, mem, command = argv
    return run(command, Path(ws), Path(cwd), name, Path(tmp), float(timeout), int(mem))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
