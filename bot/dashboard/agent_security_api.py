"""Agent security API: permission rules and modes, MCP tool pins, untrusted-content marks.

    GET  /api/agent/permissions                 the host's mode, rules, lock and bypass setting
    POST /api/agent/permissions/validate        check a list of rules without saving
    GET  /api/instances/{id}/permissions        one bot instance's settings and what is in force
    PUT  /api/instances/{id}/permissions        change them (409 permissions_locked if the host locked them)
    GET  /api/mcp/pins                          every connected MCP tool with its pin status
    POST /api/mcp/pins/approve                  approve a changed tool
    GET  /api/agent/taint?session=KEY           has this session read untrusted content
    POST /api/agent/taint/clear                 a person clears the mark
    GET  /api/skills/packs                      installed skill packs
    POST /api/skills/fetch                      fetch a pack from a git URL into quarantine (scan report returned)
    GET  /api/skills/quarantine                 packs waiting for a person, with their scan reports
    POST /api/skills/quarantine/approve|reject  a person's decision
    GET  /api/skills/drafts                     skills the agent drafted from a task
    POST /api/skills/drafts/approve|reject      a person's decision

Reads follow the dashboard's normal auth (the desktop token or a paired device's key);
anything that changes something needs the dashboard token itself, so a paired phone cannot
loosen the agent's permissions.
"""
from __future__ import annotations

from typing import Any, Callable, Optional

from fastapi import Depends, FastAPI, HTTPException, Query
from pydantic import BaseModel


class _RulesBody(BaseModel):
    rules: list[dict[str, Any]] = []


class _SettingsBody(BaseModel):
    mode: Optional[str] = None
    rules: Optional[list[dict[str, Any]]] = None


class _PinBody(BaseModel):
    server: str
    tool: str


class _SessionBody(BaseModel):
    session: str


class _NameBody(BaseModel):
    name: str


class _FetchBody(BaseModel):
    url: str
    ref: Optional[str] = None
    subdir: str = ""


def _rule_dicts(rules) -> list[dict]:
    return [{"decision": r.decision, "tool": r.tool, "match": r.match, "note": r.note} for r in rules]


def register(app: FastAPI, read_auth: Callable, write_auth: Callable) -> None:
    from bot.agent_runtime import mcp_client, permissions, taint

    read = [Depends(read_auth)]
    write = [Depends(write_auth)]

    def _host() -> dict:
        cfg = permissions._config()
        mode, rules, allow_bypass = permissions.effective(None)
        return {"mode": mode, "rules": _rule_dicts(rules), "locked": permissions.is_locked(),
                "allow_bypass": allow_bypass, "modes": list(permissions.MODES),
                "problems": permissions.validate_rules(cfg.get("rules"))}

    @app.get("/api/agent/permissions", dependencies=read)
    async def get_permissions():
        return _host()

    @app.post("/api/agent/permissions/validate", dependencies=read)
    async def validate_permissions(body: _RulesBody):
        problems = permissions.validate_rules(body.rules)
        return {"ok": not problems, "problems": problems}

    @app.get("/api/instances/{instance_id}/permissions", dependencies=read)
    async def get_instance_permissions(instance_id: int):
        from bot import bot_instances

        if bot_instances.get_instance(instance_id) is None:
            raise HTTPException(status_code=404, detail="no such bot instance")
        mode, rules, allow_bypass = permissions.effective(instance_id)
        return {"instance": permissions.instance_settings(instance_id),
                "effective": {"mode": mode, "rules": _rule_dicts(rules), "allow_bypass": allow_bypass},
                "locked": permissions.is_locked(), "modes": list(permissions.MODES)}

    @app.put("/api/instances/{instance_id}/permissions", dependencies=write)
    async def put_instance_permissions(instance_id: int, body: _SettingsBody):
        try:
            saved = permissions.set_instance_settings(instance_id, mode=body.mode, rules=body.rules, actor="dashboard")
        except PermissionError:
            raise HTTPException(status_code=409, detail="permissions_locked")
        except KeyError:
            raise HTTPException(status_code=404, detail="no such bot instance")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return {"instance": saved}

    @app.get("/api/mcp/pins", dependencies=read)
    async def get_pins():
        return {"tools": mcp_client.pin_report()}

    @app.post("/api/mcp/pins/approve", dependencies=write)
    async def approve_pin(body: _PinBody):
        if not mcp_client.approve_pin(body.server, body.tool):
            raise HTTPException(status_code=404, detail="no such connected tool")
        return {"approved": True}

    @app.get("/api/agent/taint", dependencies=read)
    async def get_taint(session: str = Query(..., min_length=1)):
        return {"session": session, "tainted": taint.is_tainted(session), "sources": taint.sources(session)}

    @app.post("/api/agent/taint/clear", dependencies=write)
    async def clear_taint(body: _SessionBody):
        was = taint.is_tainted(body.session)
        taint.clear(body.session)
        return {"session": body.session, "cleared": was}

    # ---- skill packs: quarantine and drafts --------------------------------------------
    import asyncio

    from bot import skill_install, skill_packs
    from bot.agent_runtime import skill_learning
    from bot.agent_runtime.errors import ToolError

    def _guard(fn, *a):
        try:
            return fn(*a)
        except ToolError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    @app.get("/api/skills/packs", dependencies=read)
    async def get_packs():
        return {"packs": [{"name": s.name, "description": s.description[:300], "source": s.source, "files": len(s.files),
                           "problems": list(s.problems)} for s in skill_packs.discover(None).values()]}

    @app.post("/api/skills/fetch", dependencies=write)
    async def fetch_pack(body: _FetchBody):
        try:
            return await asyncio.to_thread(skill_install.install_from_git, body.url, body.ref, body.subdir)
        except ToolError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    @app.get("/api/skills/quarantine", dependencies=read)
    async def get_quarantine():
        return {"packs": skill_install.list_quarantine()}

    @app.post("/api/skills/quarantine/approve", dependencies=write)
    async def approve_pack(body: _NameBody):
        return _guard(skill_install.approve, body.name)

    @app.post("/api/skills/quarantine/reject", dependencies=write)
    async def reject_pack(body: _NameBody):
        _guard(skill_install.reject, body.name)
        return {"rejected": True}

    @app.get("/api/skills/drafts", dependencies=read)
    async def get_drafts():
        return {"drafts": skill_learning.list_drafts()}

    @app.post("/api/skills/drafts/approve", dependencies=write)
    async def approve_draft(body: _NameBody):
        return _guard(skill_learning.approve_draft, body.name)

    @app.post("/api/skills/drafts/reject", dependencies=write)
    async def reject_draft(body: _NameBody):
        _guard(skill_learning.reject_draft, body.name)
        return {"rejected": True}
