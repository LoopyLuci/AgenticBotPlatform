"""Talking to Hermes Manager: finding its bridge, starting one, calling its operations and driving its window.

Hermes Manager (https://github.com/LoopyLuci/Hermes-Manager) is a separate program: an Electron window over a local
API, the bridge, in front of a Hermes install. A running bridge writes ``~/.hermes-manager/control.json`` (its URL,
token and pid); ABP reads it, so there is nothing to configure. See docs/agents/hermes-manager.md.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import httpx


class ManagerError(Exception):
    def __init__(self, message: str, code: str = "error", status: int = 0) -> None:
        super().__init__(message)
        self.code, self.status = code, status


@dataclass
class Bridge:
    url: str
    token: str
    pid: int
    owner: str = ""
    gui: bool = False


def hm_home() -> Path:
    return Path(os.environ.get("HM_HOME") or Path.home() / ".hermes-manager")


def find() -> Optional[Bridge]:
    try:
        d = json.loads((hm_home() / "control.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    try:
        ping = httpx.get(d["url"].rstrip("/") + "/api/v1/ping", timeout=2.0).json()
    except (httpx.HTTPError, ValueError, KeyError):
        return None
    if ping.get("pid") != d.get("pid"):
        return None
    return Bridge(d["url"].rstrip("/"), d["token"], d["pid"], d.get("owner", ""), bool(d.get("gui")))


def require(*, start: bool = True) -> Bridge:
    b = find()
    if b is None and start:
        from bot.hermes_manager import harness
        harness.start_bridge()
        b = find()
    if b is None:
        raise ManagerError("Hermes Manager's bridge is not running (start it from the Hermes Manager page)", code="unavailable")
    return b


def request(method: str, path: str, *, body: Any = None, timeout: float = 900.0, bridge: Optional[Bridge] = None) -> Any:
    b = bridge or require()
    try:
        r = httpx.request(method, b.url + path, json=body, timeout=timeout, headers={"Authorization": f"Bearer {b.token}"})
    except httpx.HTTPError as e:
        raise ManagerError(f"Hermes Manager's bridge at {b.url} is not reachable: {e}", code="unavailable") from None
    try:
        data = r.json() if r.content else None
    except ValueError:
        data = {"text": r.text}
    if r.status_code >= 400:
        detail = data.get("detail") if isinstance(data, dict) else None
        raise ManagerError(str(detail or f"HTTP {r.status_code}"), code="http", status=r.status_code)
    return data


_ops: dict[str, Any] = {"at": 0.0, "url": "", "api": [], "gui": []}


def operations(refresh: bool = False) -> list[dict]:
    """Bridge operations (id, group, summary, params, mutating) and window operations (gui.*), cached for a minute."""
    b = require()
    if refresh or _ops["url"] != b.url or time.time() - _ops["at"] > 60:
        api = request("GET", "/api/v1/operations", bridge=b)
        gui = request("GET", "/api/v1/gui/operations", bridge=b)
        _ops.update(at=time.time(), url=b.url, api=api, gui=[
            {"id": g["id"], "group": "gui", "summary": g["summary"], "mutating": bool(g.get("mutating")),
             "params": {"type": "object", "properties": g.get("params") or {}, **({"required": g["required"]} if g.get("required") else {})}}
            for g in gui])
    return list(_ops["api"]) + list(_ops["gui"])


def operation(op_id: str) -> dict:
    for o in operations():
        if o["id"] == op_id:
            return o
    raise ManagerError(f"Hermes Manager has no operation {op_id!r}", code="not_found", status=404)


def call(op_id: str, args: Optional[dict] = None, *, timeout: float = 900.0) -> Any:
    args = dict(args or {})
    if op_id == "gui.launch":
        return request("POST", "/api/v1/gui/launch", body={"wait_s": args.get("wait_s", 60)}, timeout=200)
    if op_id.startswith("gui."):
        return request("POST", f"/api/v1/gui/{op_id[4:]}", body=args, timeout=min(timeout, 180))
    return request("POST", f"/api/v1/call/{op_id}", body=args, timeout=timeout)
