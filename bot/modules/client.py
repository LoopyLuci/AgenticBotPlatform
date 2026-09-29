"""Talking to a module's control hub, the way its manifest says.

A running hub writes its control file (``hub.control_file``): JSON with at least ``url`` and ``token``, usually
``pid`` and ``version`` too. ABP reads it, checks the hub answers (and, when a pid is given, that it is that
process), and calls it with ``Authorization: Bearer <token>``. The token stays on this machine: it is never shown,
logged or sent anywhere but the hub.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import httpx

from bot.modules import registry
from bot.modules.manifest import Manifest


class ModuleError(Exception):
    def __init__(self, message: str, code: str = "error", status: int = 0) -> None:
        super().__init__(message)
        self.code, self.status = code, status


@dataclass
class Hub:
    url: str
    token: str
    pid: int = 0
    version: str = ""


def control_file(m: Manifest) -> Optional[Path]:
    return Path(registry.expand(m, m.hub.control_file)) if m.hub else None


def find(m: Manifest, timeout: float = 2.0) -> Optional[Hub]:
    """The module's running hub, or None (no control file, nobody answering, or a stale file from a hub that died)."""
    path = control_file(m)
    if path is None:
        return None
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
        url, token = str(d["url"]).rstrip("/"), str(d["token"])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    try:
        r = httpx.get(url + m.hub.api_base + m.hub.health, timeout=timeout)
        health = r.json() if r.content else {}
    except (httpx.HTTPError, ValueError):
        return None
    if r.status_code >= 400:
        return None
    health = health if isinstance(health, dict) else {}
    want_pid = d.get("pid")
    if want_pid and health.get("pid") and int(health["pid"]) != int(want_pid):
        return None
    return Hub(url, token, int(health.get("pid") or want_pid or 0), str(health.get("version") or d.get("version") or ""))


def require(m: Manifest, *, start: bool = False) -> Hub:
    hub = find(m)
    if hub is None and start:
        from bot.modules import harness
        harness.start_hub(m.id)
        hub = find(m)
    if hub is None:
        raise ModuleError(f"{m.name}'s hub is not running (start it from the Modules page)", code="unavailable")
    return hub


def request(m: Manifest, method: str, path: str, *, body: Any = None, timeout: float = 900.0,
            hub: Optional[Hub] = None) -> Any:
    h = hub or require(m)
    try:
        r = httpx.request(method, h.url + m.hub.api_base + path, json=body, timeout=timeout,
                          headers={"Authorization": f"Bearer {h.token}", "X-ABP-Client": "abp"})
    except httpx.HTTPError as e:
        raise ModuleError(f"{m.name}'s hub at {h.url} is not reachable: {e}", code="unavailable") from None
    try:
        data = r.json() if r.content else None
    except ValueError:
        data = {"text": r.text[:4000]}
    if r.status_code >= 400:
        err = data.get("error") if isinstance(data, dict) else None
        if isinstance(err, dict):
            raise ModuleError(str(err.get("message") or r.status_code), code=str(err.get("code") or "http"),
                              status=r.status_code)
        raise ModuleError(str(err or (data.get("detail") if isinstance(data, dict) else None) or f"HTTP {r.status_code}"),
                          code="http", status=r.status_code)
    return data


def normalize_op(o: dict) -> dict:
    """Operations come in slightly different shapes from different hubs; this is the one ABP uses."""
    mutating = o.get("mutating")
    if mutating is None:
        mutating = not bool(o.get("read_only", o.get("readOnly", False)))
    return {"id": str(o.get("id") or o.get("name") or ""), "group": str(o.get("group") or str(o.get("id", "")).split(".")[0]),
            "summary": str(o.get("summary") or o.get("description") or ""), "mutating": bool(mutating),
            "destructive": bool(o.get("destructive", False)), "streaming": bool(o.get("streaming", False)),
            "input_schema": o.get("input_schema") or o.get("inputSchema") or o.get("params") or {"type": "object"}}


_ops: dict[str, dict] = {}


def operations(m: Manifest, refresh: bool = False) -> list[dict]:
    """Every operation the hub offers (normalized), cached a minute per hub."""
    h = require(m)
    c = _ops.get(m.id)
    if refresh or not c or c["url"] != h.url or time.time() - c["at"] > 60:
        raw = request(m, "GET", m.hub.operations, hub=h)
        if isinstance(raw, dict):
            raw = raw.get("operations") or raw.get("ops") or []
        ops = [normalize_op(o) for o in raw or [] if isinstance(o, dict)]
        _ops[m.id] = c = {"at": time.time(), "url": h.url, "ops": [o for o in ops if o["id"]]}
    return list(c["ops"])


def operation(m: Manifest, op_id: str) -> dict:
    for o in operations(m):
        if o["id"] == op_id:
            return o
    raise ModuleError(f"{m.name} has no operation {op_id!r}", code="not_found", status=404)


def call(m: Manifest, op_id: str, args: Optional[dict] = None, *, timeout: float = 900.0) -> Any:
    data = request(m, "POST", m.hub.call.replace("{op}", op_id), body=dict(args or {}), timeout=timeout)
    return data.get("result") if isinstance(data, dict) and "result" in data else data


def stop(m: Manifest, hub: Optional[Hub] = None) -> None:
    request(m, "POST", m.hub.stop, body={}, timeout=15, hub=hub)
