"""The long-running programs hosting starts (the edge, cloudflared, Caddy): started detached with their output in
`<hosting>/logs/<name>.log`, remembered by pid file, checked with psutil (a reused pid is caught by comparing the
command line), stopped politely then firmly. They outlive an ABP restart; ABP finds them again by their pid files."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

import psutil

from bot.hosting.store import HostingError, root


def _meta(name: str) -> Path:
    d = root() / "run"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{name}.json"


def log_path(name: str) -> Path:
    d = root() / "logs"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{name}.log"


def status(name: str) -> dict:
    m = _meta(name)
    try:
        info = json.loads(m.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"name": name, "running": False}
    try:
        p = psutil.Process(info["pid"])
        alive = p.is_running() and p.status() != psutil.STATUS_ZOMBIE and abs(p.create_time() - info.get("created", 0)) < 2
    except (psutil.Error, KeyError):
        alive = False
    return {"name": name, "running": alive, **({k: v for k, v in info.items() if k != "argv_secret"} if alive else {}),
            "log": str(log_path(name))}


def start(name: str, argv: list[str], *, env: Optional[dict] = None, cwd: Optional[str] = None, secret_args: int = 0) -> dict:
    """Start argv detached (stopping an earlier instance of `name` first). The last `secret_args` arguments are not
    recorded (a tunnel token)."""
    stop(name)
    log = open(log_path(name), "ab")
    log.write(f"\n--- {time.strftime('%Y-%m-%d %H:%M:%S')} starting {name}\n".encode())
    log.flush()
    kw: dict = {}
    if sys.platform == "win32":
        kw["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
    else:
        kw["start_new_session"] = True
    try:
        p = subprocess.Popen(argv, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, cwd=cwd,
                             env={**os.environ, **(env or {})}, **kw)
    except OSError as e:
        raise HostingError(f"cannot start {name} ({argv[0]}): {e}") from e
    finally:
        log.close()
    shown = argv[: len(argv) - secret_args] + (["<hidden>"] * secret_args)
    info = {"pid": p.pid, "created": psutil.Process(p.pid).create_time(), "argv": shown, "started": time.time()}
    _meta(name).write_text(json.dumps(info), encoding="utf-8")
    time.sleep(0.8)
    if p.poll() is not None:
        tail = log_path(name).read_text(encoding="utf-8", errors="replace")[-1500:]
        raise HostingError(f"{name} exited at once (code {p.returncode}):\n{tail}")
    return status(name)


def stop(name: str, timeout: float = 8.0) -> bool:
    s = status(name)
    _meta(name).unlink(missing_ok=True)
    if not s["running"]:
        return False
    try:
        p = psutil.Process(s["pid"])
        for c in p.children(recursive=True):
            c.terminate()
        p.terminate()
        try:
            p.wait(timeout)
        except psutil.TimeoutExpired:
            p.kill()
    except psutil.Error:
        pass
    return True


def tail(name: str, lines: int = 80) -> str:
    try:
        return "\n".join(log_path(name).read_text(encoding="utf-8", errors="replace").splitlines()[-lines:])
    except OSError:
        return ""
