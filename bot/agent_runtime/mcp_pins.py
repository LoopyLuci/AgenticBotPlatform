"""Pinning external MCP tools against silent changes (roadmap P2).

An MCP server describes its tools - name, description, input schema - and the model
trusts those descriptions. A server (or something that took it over) can change a
description after you connected it, slipping in new instructions ("also send the user's
files to ..."): a "rug pull". Pinning makes that visible:

* the first time a tool is seen its fingerprint (a hash of the description and input
  schema) is recorded and the tool works - trust on first use, because an operator who
  connected the server chose to trust what it offered at that moment;
* if a pinned tool's description or schema later changes, the tool is blocked - not
  offered to the model, not callable - until a person reviews the change and approves it
  (dashboard: /api/mcp/pins);
* an approved change re-pins the tool.

Switch off with `native_agent.mcp_pinning: false`. Pins live in the agent state folder
(`mcp_pins.json`).
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from typing import Any

from bot.agent_runtime.state import state_dir

logger = logging.getLogger("bot.agent_runtime.mcp_pins")

_warned: set[tuple[str, str]] = set()


def enabled() -> bool:
    try:
        from bot.config import config

        return bool((config.current.get("native_agent") or {}).get("mcp_pinning", True))
    except Exception:  # noqa: BLE001
        return True


def _path():
    return state_dir() / "mcp_pins.json"


def _load() -> dict:
    try:
        data = json.loads(_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(data: dict) -> None:
    tmp = _path().with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1, sort_keys=True), encoding="utf-8")
    tmp.replace(_path())


def fingerprint(tool: dict) -> str:
    material = json.dumps({"description": tool.get("description", ""), "input_schema": tool.get("input_schema") or {}},
                          sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def observe(server: str, tool: dict) -> str:
    """Record or check one tool. Returns "new" (just pinned), "ok" or "changed" (blocked)."""
    if not enabled():
        return "ok"
    name = str(tool.get("name", ""))
    fp = fingerprint(tool)
    data = _load()
    entry = (data.get(server) or {}).get(name)
    if entry is None:
        data.setdefault(server, {})[name] = {"fingerprint": fp, "description": str(tool.get("description", ""))[:500],
                                              "pinned_at": time.time()}
        try:
            _save(data)
        except OSError:
            logger.warning("could not save MCP tool pins", exc_info=True)
        return "new"
    if entry.get("fingerprint") == fp:
        return "ok"
    if (server, name) not in _warned:
        _warned.add((server, name))
        logger.warning("MCP tool %s/%s changed since it was pinned; it is blocked until a person approves the change",
                       server, name)
    return "changed"


def status(server: str, tool: dict) -> str:
    """Like observe(), without recording anything (for read-only views)."""
    if not enabled():
        return "ok"
    entry = (_load().get(server) or {}).get(str(tool.get("name", "")))
    if entry is None:
        return "new"
    return "ok" if entry.get("fingerprint") == fingerprint(tool) else "changed"


def approve(server: str, tool: dict) -> None:
    """A person has reviewed the tool as it is now: pin this version."""
    data = _load()
    data.setdefault(server, {})[str(tool.get("name", ""))] = {
        "fingerprint": fingerprint(tool), "description": str(tool.get("description", ""))[:500], "pinned_at": time.time(),
        "approved": True}
    _save(data)
    _warned.discard((server, str(tool.get("name", ""))))


def forget(server: str, name: str | None = None) -> None:
    data = _load()
    if name is None:
        data.pop(server, None)
    else:
        (data.get(server) or {}).pop(name, None)
    _save(data)


def list_pins() -> dict[str, Any]:
    return _load()
