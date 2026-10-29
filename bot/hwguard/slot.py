"""The machine-wide heavy-work slot: one render, full test run, model load or build at a time.

    acquire(kind, label, pid=None, wait_s=0, token=None) -> {"granted", "holder", "reason"}
    release(pid=None, force=False, token=None)          -> bool
    holder()                                -> the current holder or None (a holder whose process is gone is cleared)
    with heavy(kind, label, wait_s): ...    in-process use (ABP's own model loads, renders); raises Refused

The slot is a file in the shared folder (governor.shared_dir), created exclusively, so separate processes (the pipeline,
the render harness, ABP itself) agree without a server. It is re-entrant for the same token (by default the pid, so a
pipeline's test run and build share it; `heavy()` uses a token of its own, so two jobs inside ABP still do not stack). While the governor says
pause, nothing is granted. With `exclusive` off, or the governor off, every request is granted and nothing is recorded.
"""
from __future__ import annotations

import contextlib
import json
import os
import time
import uuid
from typing import Iterator, Optional

from bot.hwguard import governor


def _path():
    return governor.shared_dir() / "slot.json"


def _alive(pid: int) -> bool:
    try:
        import psutil

        return psutil.pid_exists(int(pid))
    except Exception:  # noqa: BLE001
        return True


def holder() -> Optional[dict]:
    p = _path()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not _alive(int(data.get("pid") or 0)):
        try:
            p.unlink()
        except OSError:
            pass
        return None
    return data


class Refused(RuntimeError):
    """The heavy-work slot was not granted (busy or paused); the message says why."""


def _try(kind: str, label: str, pid: int, token: str) -> tuple[bool, Optional[dict]]:
    cur = holder()
    if cur is not None:
        return str(cur.get("token") or cur.get("pid")) == token, cur
    rec = {"kind": kind, "label": label[:200], "pid": pid, "token": token, "since": time.time()}
    try:
        fd = os.open(str(_path()), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return False, holder()
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(rec, fh)
    return True, rec


def acquire(kind: str, label: str = "", pid: Optional[int] = None, wait_s: float = 0.0, poll_s: float = 5.0,
            cfg: Optional[dict] = None, token: Optional[str] = None) -> dict:
    cfg = cfg or governor.settings()
    pid = int(pid or os.getpid())
    token = token or str(pid)
    deadline = time.time() + max(0.0, wait_s)
    if not cfg.get("enabled", True) or not cfg["govern"].get(kind, True):
        return {"granted": True, "holder": None, "reason": "not governed"}
    while True:
        v = governor.verdict()
        if v.get("heavy_allowed", True):
            if not cfg.get("exclusive", True):
                return {"granted": True, "holder": None, "reason": "slot not exclusive"}
            ok, cur = _try(kind, label, pid, token)
            if ok:
                return {"granted": True, "holder": cur, "reason": "slot held"}
            reason = f"busy: {cur.get('kind')} ({cur.get('label') or 'pid ' + str(cur.get('pid'))})" if cur else "busy"
        else:
            cur, reason = holder(), "paused: " + "; ".join(v.get("reasons") or [])
        if time.time() >= deadline:
            return {"granted": False, "holder": cur, "reason": reason}
        time.sleep(min(poll_s, max(0.1, deadline - time.time())))


def release(pid: Optional[int] = None, force: bool = False, token: Optional[str] = None) -> bool:
    cur = holder()
    if cur is None:
        return False
    if not force and str(cur.get("token") or cur.get("pid")) != (token or str(pid or os.getpid())):
        return False
    try:
        _path().unlink()
        return True
    except OSError:
        return False


@contextlib.contextmanager
def heavy(kind: str, label: str = "", wait_s: float = 0.0) -> Iterator[dict]:
    """Hold the slot for the duration of a block inside this process."""
    token = f"{os.getpid()}:{uuid.uuid4().hex[:12]}"
    res = acquire(kind, label, wait_s=wait_s, token=token)
    if not res["granted"]:
        raise Refused(f"hardware guard: {kind} not started - {res['reason']}")
    try:
        yield res
    finally:
        if res.get("holder"):
            release(token=token)
