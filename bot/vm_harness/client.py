"""Talking to VM-Harness: finding its hub, starting it, calling its operations.

VM-Harness is a separate program (https://github.com/LoopyLuci/VM-Harness) with its own window, its own venv and its
own release cycle. It runs one local service, the hub, which writes ``~/.vmharness/control.json`` (its URL and token)
when it starts; ABP reads that file, so there is no port or token to configure. See docs/agents/vm-harness.md.

A hub on another machine is reached with ``vm_harness.url`` + ``vm_harness.token`` in config/backends.yaml (for example
through an SSH tunnel), or through a paired ABP peer.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import httpx


class HarnessError(Exception):
    def __init__(self, message: str, code: str = "error", status: int = 0) -> None:
        super().__init__(message)
        self.code, self.status = code, status


@dataclass
class Hub:
    url: str
    token: str
    pid: int = 0
    version: str = ""
    gui: bool = False
    remote: bool = False


def vmh_home() -> Path:
    return Path(os.environ.get("VMH_HOME") or Path.home() / ".vmharness")


def _cfg() -> dict:
    try:
        from bot.config import config
        return dict((config.current or {}).get("vm_harness") or {})
    except Exception:  # noqa: BLE001
        return {}


def _alive(url: str, timeout: float = 2.0) -> Optional[dict]:
    try:
        r = httpx.get(url.rstrip("/") + "/v1/health", timeout=timeout)
        data = r.json()
        return data if data.get("ok") and data.get("service") == "vm-harness" else None
    except (httpx.HTTPError, ValueError):
        return None


def find() -> Optional[Hub]:
    """The hub to talk to: the configured remote one, else the local one from control.json. None if none answers."""
    cfg = _cfg()
    if cfg.get("url") and cfg.get("token"):
        health = _alive(str(cfg["url"]))
        return Hub(str(cfg["url"]).rstrip("/"), str(cfg["token"]), health.get("pid", 0), health.get("version", ""),
                   bool(health.get("gui")), remote=True) if health else None
    try:
        d = json.loads((vmh_home() / "control.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    health = _alive(d.get("url", ""))
    if not health or health.get("pid") != d.get("pid"):
        return None
    return Hub(d["url"].rstrip("/"), d["token"], d.get("pid", 0), health.get("version", ""), bool(health.get("gui")))


def require(*, start: bool = True) -> Hub:
    hub = find()
    if hub is None and start and not _cfg().get("url"):
        from bot.vm_harness import harness
        harness.start_hub()
        hub = find()
    if hub is None:
        raise HarnessError("VM-Harness is not running (start it from the VM-Harness page, or vmh_setup)", code="unavailable")
    return hub


def request(method: str, path: str, *, body: Any = None, timeout: float = 600.0, hub: Optional[Hub] = None) -> dict:
    hub = hub or require()
    try:
        r = httpx.request(method, hub.url + path, json=body, timeout=timeout,
                          headers={"Authorization": f"Bearer {hub.token}", "X-VMH-Client": "abp"})
    except httpx.HTTPError as e:
        raise HarnessError(f"VM-Harness at {hub.url} is not reachable: {e}", code="unavailable") from e
    try:
        data = r.json()
    except ValueError:
        raise HarnessError(f"VM-Harness answered HTTP {r.status_code} without JSON", status=r.status_code) from None
    if r.status_code >= 400 or data.get("ok") is False:
        raise HarnessError(data.get("error") or f"HTTP {r.status_code}", code=data.get("code", "error"), status=r.status_code)
    return data


def call(op: str, args: Optional[dict] = None, *, timeout: float = 600.0) -> Any:
    """Run one VM-Harness operation (see operations()) and return its result."""
    return request("POST", f"/v1/call/{op}", body=dict(args or {}), timeout=timeout)["result"]


_ops_cache: dict[str, Any] = {"at": 0.0, "url": "", "ops": []}


def operations(refresh: bool = False) -> list[dict]:
    """The hub's catalog (cached for a minute): id, group, summary, params (JSON Schema), mutating, destructive."""
    hub = require()
    if refresh or _ops_cache["url"] != hub.url or time.time() - _ops_cache["at"] > 60:
        _ops_cache.update(at=time.time(), url=hub.url, ops=request("GET", "/v1/operations", hub=hub)["operations"])
    return list(_ops_cache["ops"])


def operation(op_id: str) -> dict:
    for o in operations():
        if o["id"] == op_id:
            return o
    raise HarnessError(f"VM-Harness has no operation {op_id!r}", code="not_found", status=404)
