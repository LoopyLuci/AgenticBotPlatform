"""Talking to TransferDaemon: finding its control hub, calling its operations, driving its window and terminal UI.

TransferDaemon (https://github.com/LoopyLuci/TransferDaemon) is a separate program: a daemon (``transferd``) with a
gRPC API, a window (``transferd-ui``), a terminal UI (``transferd-tui``), a CLI (``transferd-cli``) and relays. The
daemon runs a local control hub and writes ``<LocalAppData>/transferdaemon/control.json`` (its URL, token and pid);
ABP reads it, so there is nothing to configure. See docs/agents/transferdaemon.md.

A hub on another machine can be used with ``transferdaemon.url`` and ``transferdaemon.token`` in config (through a
tunnel); for a linked ABP server, use the tools' ``machine`` argument instead.
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import httpx


class DaemonError(Exception):
    def __init__(self, message: str, code: str = "error", status: int = 0) -> None:
        super().__init__(message)
        self.code, self.status = code, status


@dataclass
class Hub:
    url: str
    token: str
    pid: int
    version: str = ""
    grpc: str = ""


def _cfg() -> dict:
    try:
        from bot.config import config
        return dict((config.current or {}).get("transferdaemon") or {})
    except Exception:  # noqa: BLE001
        return {}


def data_dir() -> Path:
    """Where the daemon keeps its data (the same rule as the daemon: $TRANSFERD_DATA_DIR, else the platform's local
    data folder), plus ``transferdaemon``."""
    env = os.environ.get("TRANSFERD_DATA_DIR") or _cfg().get("data_dir")
    if env:
        base = Path(env)
    elif sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    return base / "transferdaemon"


def find() -> Optional[Hub]:
    """The running daemon's hub, or None. The pid in control.json must match the one answering (a stale file from a
    daemon that died is ignored)."""
    cfg = _cfg()
    if cfg.get("url") and cfg.get("token"):
        d = {"url": cfg["url"], "token": cfg["token"], "pid": None}
    else:
        try:
            d = json.loads((data_dir() / "control.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
    try:
        health = httpx.get(str(d["url"]).rstrip("/") + "/v1/health", timeout=2.0).json()
    except (httpx.HTTPError, ValueError, KeyError):
        return None
    if d.get("pid") is not None and health.get("pid") != d.get("pid"):
        return None
    return Hub(str(d["url"]).rstrip("/"), str(d["token"]), int(health.get("pid") or 0), str(health.get("version") or ""),
               str(d.get("grpc") or ""))


_found: dict[str, Any] = {"at": 0.0, "hub": None, "busy": False}


def find_cached() -> Optional[Hub]:
    """find(), answered from a cache and refreshed in the background (for checks that run every turn)."""
    if time.monotonic() - _found["at"] > 20 and not _found["busy"]:
        _found["busy"] = True

        def refresh() -> None:
            try:
                _found["hub"] = find()
            finally:
                _found.update(at=time.monotonic(), busy=False)
        threading.Thread(target=refresh, name="td-find", daemon=True).start()
    return _found["hub"]


def require(*, start: bool = True) -> Hub:
    hub = find()
    if hub is None and start:
        from bot.transferdaemon import harness
        harness.start_daemon()
        hub = find()
    if hub is None:
        raise DaemonError("TransferDaemon is not running (start it from the TransferDaemon page)", code="unavailable")
    return hub


def request(method: str, path: str, *, body: Any = None, timeout: float = 900.0, hub: Optional[Hub] = None) -> Any:
    h = hub or require()
    try:
        r = httpx.request(method, h.url + path, json=body, timeout=timeout, headers={"Authorization": f"Bearer {h.token}"})
    except httpx.HTTPError as e:
        raise DaemonError(f"TransferDaemon's control hub at {h.url} is not reachable: {e}", code="unavailable") from None
    try:
        data = r.json() if r.content else None
    except ValueError:
        data = {"text": r.text}
    if r.status_code >= 400:
        err = data.get("error") if isinstance(data, dict) else None
        if isinstance(err, dict):
            raise DaemonError(str(err.get("message") or r.status_code), code=str(err.get("code") or "http"), status=r.status_code)
        raise DaemonError(f"HTTP {r.status_code}", code="http", status=r.status_code)
    return data


_ops: dict[str, Any] = {"at": 0.0, "url": "", "ops": []}


def operations(refresh: bool = False) -> list[dict]:
    """Every operation (id, group, summary, mutating, destructive, streaming, input/output schemas), cached a minute."""
    h = require()
    if refresh or _ops["url"] != h.url or time.time() - _ops["at"] > 60:
        _ops.update(at=time.time(), url=h.url, ops=request("GET", "/v1/operations", hub=h))
    return list(_ops["ops"])


def operation(op_id: str) -> dict:
    for o in operations():
        if o["id"] == op_id:
            return o
    raise DaemonError(f"TransferDaemon has no operation {op_id!r}", code="not_found", status=404)


def call(op_id: str, args: Optional[dict] = None, *, timeout: float = 900.0) -> Any:
    data = request("POST", f"/v1/call/{op_id}", body=dict(args or {}), timeout=timeout)
    return data.get("result") if isinstance(data, dict) and "result" in data else data


def audit(limit: int = 100) -> list:
    return request("GET", f"/v1/audit?limit={int(limit)}") or []
