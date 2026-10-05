"""The long-running programs hosting starts (the edge, cloudflared, Caddy): started detached with their output in
`<hosting>/logs/<name>.log`, remembered by pid file, checked with psutil (a reused pid is caught by comparing the
command line), stopped politely then firmly. They outlive an ABP restart; ABP finds them again by their pid files.
Every function takes `home`, the folder holding run/ and logs/ (default: hosting's; the file server passes its own).

Each of them also gets a sandbox_ns cell (preset "daemon"), keyed by (home, name) like the pid file: persistent, so
the reaper and ABP's own shutdown leave it running, and its kill is the whole tree - which is what stop() falls
back on when a polite stop did not finish the job. A program an earlier ABP started has no cell here, so the pid
file path stays exactly as it was.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Optional

import psutil

from bot.hosting.store import HostingError, root
from bot.sandbox_ns.cell import Cell, new_cell
from bot.sandbox_ns.spawn import spawn as ns_spawn

OWNER = "hosting.procs"

#: The daemon cell per (home, name), for the programs this process started. Released by stop().
_cells: dict[tuple[str, str], Cell] = {}
_cells_lock = threading.Lock()


def _key(name: str, home: Optional[Path] = None) -> tuple[str, str]:
    return (str(home or root()), name)


def _take_cell(name: str, home: Optional[Path] = None) -> Optional[Cell]:
    with _cells_lock:
        return _cells.pop(_key(name, home), None)


def _meta(name: str, home: Optional[Path] = None) -> Path:
    d = (home or root()) / "run"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{name}.json"


def log_path(name: str, home: Optional[Path] = None) -> Path:
    d = (home or root()) / "logs"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{name}.log"


def status(name: str, home: Optional[Path] = None) -> dict:
    m = _meta(name, home)
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
            "log": str(log_path(name, home))}


def start(name: str, argv: list[str], *, env: Optional[dict] = None, cwd: Optional[str] = None, secret_args: int = 0,
          home: Optional[Path] = None) -> dict:
    """Start argv detached (stopping an earlier instance of `name` first). The last `secret_args` arguments are not
    recorded (a tunnel token)."""
    stop(name, home=home)
    log = open(log_path(name, home), "ab")
    log.write(f"\n--- {time.strftime('%Y-%m-%d %H:%M:%S')} starting {name}\n".encode())
    log.flush()
    kw: dict = {}
    if sys.platform == "win32":
        kw["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
    else:
        kw["start_new_session"] = True
    cell = new_cell("daemon", name=name, owner=OWNER)
    try:
        p = ns_spawn(argv, cell=cell, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, cwd=cwd,
                     env={**os.environ, **(env or {})}, name=name, owner=OWNER, **kw)
    except OSError as e:
        cell.close()
        raise HostingError(f"cannot start {name} ({argv[0]}): {e}") from e
    finally:
        log.close()
    with _cells_lock:
        _cells[_key(name, home)] = cell
    shown = argv[: len(argv) - secret_args] + (["<hidden>"] * secret_args)
    info = {"pid": p.pid, "created": psutil.Process(p.pid).create_time(), "argv": shown, "started": time.time()}
    _meta(name, home).write_text(json.dumps(info), encoding="utf-8")
    time.sleep(0.8)
    if p.poll() is not None:
        # It died at once: there is nothing to keep a cell for, and its log is what says why.
        dead = _take_cell(name, home)
        if dead is not None:
            dead.kill(f"{name} exited at once (code {p.returncode})")
        tail = log_path(name, home).read_text(encoding="utf-8", errors="replace")[-1500:]
        raise HostingError(f"{name} exited at once (code {p.returncode}):\n{tail}")
    return status(name, home)


def stop(name: str, timeout: float = 8.0, home: Optional[Path] = None) -> bool:
    s = status(name, home)
    _meta(name, home).unlink(missing_ok=True)
    cell = _take_cell(name, home)
    if not s["running"]:
        if cell is not None:
            cell.close()          # nothing to stop, just the job handle to release
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
    if cell is not None:
        # Anything the polite stop did not finish - a grandchild that ignored the terminate -
        # goes with the cell, tree and all.
        cell.kill(f"stopping {name}")
    return True


def tail(name: str, lines: int = 80, home: Optional[Path] = None) -> str:
    try:
        return "\n".join(log_path(name, home).read_text(encoding="utf-8", errors="replace").splitlines()[-lines:])
    except OSError:
        return ""
