"""An agent's request for DriveTier to quarantine paths on an owner drive (D7, decided 2026-10-27).

The request goes through DriveTier's space_propose, the only operation this module calls. That operation builds a plan
and a pending approval inside DriveTier. Nothing moves: the owner approves it in the DriveTier GUI, and only then is the
plan applied. DriveTier's apply and undo are never called from here, and agents cannot approve (docs/recovery-spec.md C2).
"""
from __future__ import annotations

import asyncio
import os
from typing import Any

from bot.modules import harness
from bot.modules.client import ModuleError

MODULE_ID = "drivetier"
OPERATION = "space_propose"
MAX_PATHS = 50
MAX_REASON = 300
NAME = "owner_drive_request"


def _paths(raw: Any) -> list[str]:
    """Absolute local paths only: no relative paths, no network shares, no '..' segments."""
    if not isinstance(raw, list) or not raw:
        raise ModuleError("give paths: a list of absolute folders or files on a local drive", code="bad_request")
    if len(raw) > MAX_PATHS:
        raise ModuleError(f"at most {MAX_PATHS} paths per request", code="bad_request")
    out = []
    for p in raw:
        s = str(p or "").strip()
        segments = s.replace("/", "\\").split("\\")
        if not s or not os.path.isabs(s) or s.startswith("\\\\") or ".." in segments:
            raise ModuleError(f"not an absolute path on a local drive: {p!r}", code="bad_request")
        out.append(s)
    return out


def request_quarantine(paths: Any, reason: str = "") -> str:
    """Ask DriveTier to plan quarantine of these paths. Returns one line for the agent."""
    args = {"paths": _paths(paths), "action": "quarantine",
            "reason": (reason or "agent request: quarantine owner-drive paths")[:MAX_REASON]}
    res = harness.call(MODULE_ID, OPERATION, args)
    if not isinstance(res, dict):
        raise ModuleError("DriveTier returned no plan", code="bad_response")
    if res.get("error"):
        raise ModuleError(str(res["error"]), code="refused")
    plan = res.get("plan") or {}
    notes = list(plan.get("notes") or [])
    approval = res.get("approval")
    if not approval:
        return "Nothing requested: " + "; ".join([res.get("message") or "no moves to propose", *notes])
    line = (f"Requested {approval['moves']} move(s) to quarantine ({plan.get('total_bytes', 0)} bytes). Approval "
            f"{approval['id']} is {approval['status']}: the owner approves it in the DriveTier GUI. Nothing has moved.")
    return line + (f" Notes: {'; '.join(notes)}" if notes else "")


def register_tool(enabled) -> None:
    from bot.agent_runtime import toolspec

    async def handler(inp, **_):
        try:
            return await asyncio.to_thread(request_quarantine, inp.get("paths"), str(inp.get("reason") or ""))
        except ModuleError as exc:
            return f"Error ({exc.code}): {exc}"

    toolspec.register(
        {"name": NAME,
         "description": "Ask DriveTier to plan moving the given paths on an owner drive into quarantine (restorable; "
                        "kept 30 days, then only the owner can purge). Only folders DriveTier's last scan found can be "
                        "planned: scan a new folder first. This only creates a pending approval in DriveTier. Nothing "
                        "moves until the owner approves it in the DriveTier GUI.",
         "input_schema": {"type": "object", "properties": {
             "paths": {"type": "array", "items": {"type": "string"},
                       "description": "absolute folders or files on an owner drive"},
             "reason": {"type": "string", "description": "why these should be quarantined"}},
             "required": ["paths"]}},
        # The request changes nothing: it only asks. So it needs no ABP approval; DriveTier's owner approval is the gate.
        toolspec.ToolSpec(NAME, "external", read_only=False, needs_approval=False, origin="registered"),
        handler, enabled=enabled)
