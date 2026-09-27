"""Sentinel API (ADR-0011): ABP's self-preservation state, and the manual levers behind its automation.

    GET  /api/sentinel/status                  everything at a glance: boot/safe mode, watchdog, last runs, backups,
                                               CVE findings, open issues, recent alerts
    GET  /api/sentinel/journal?limit=&level=   what the Sentinel saw and did
    POST /api/sentinel/run/{duty}              run a duty now: integrity | backup | cve | security | bugs
    GET  /api/sentinel/backups                 verified backup sets, newest first
    POST /api/sentinel/backups/{name}/verify   re-check a set against its manifest
    POST /api/sentinel/backups/{name}/restore  {"parts": ["db", "config", "secrets"]}
    GET  /api/sentinel/cve                     the last vulnerability scan
    POST /api/sentinel/cve/fix                 upgrade vulnerable Python packages (smoke-tested, rolled back on failure)
    GET  /api/sentinel/issues?status=          fingerprinted errors
    POST /api/sentinel/issues/status           {"signature": ..., "status": "open|resolved|ignored"}

Reading uses the dashboard's normal auth, so a paired phone can watch. Anything that changes state (running a
duty, restoring, upgrading packages, triaging) needs the desktop dashboard token."""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Callable, Optional

from fastapi import Body, Depends, FastAPI, HTTPException, Query


def register(app: FastAPI, read_auth: Callable, write_auth: Callable) -> None:
    from bot import db
    from bot.sentinel import backup, bug_hunter, cve, journal, sentinel, settings

    read = [Depends(read_auth)]
    write = [Depends(write_auth)]

    @app.get("/api/sentinel/status", dependencies=read)
    def sentinel_status():
        return sentinel.status()

    @app.get("/api/sentinel/journal", dependencies=read)
    def sentinel_journal(limit: int = Query(100, ge=1, le=500), level: str = Query("info", pattern="^(info|warning|critical)$"),
                         kind: Optional[str] = None):
        return {"entries": journal.recent(limit, kind=kind, min_level=level)}

    duties = {
        "integrity": lambda cfg: sentinel.integrity_duty(cfg),
        "backup": lambda cfg: sentinel.backup_duty(cfg),
        "cve": lambda cfg: sentinel.cve_duty(cfg),
        "security": lambda cfg: sentinel.security_duty(cfg),
        "bugs": lambda cfg: bug_hunter.review(cfg["bug_hunter"].get("kanban_instance_id")),
    }

    @app.post("/api/sentinel/run/{duty}", dependencies=write)
    async def sentinel_run(duty: str):
        fn = duties.get(duty)
        if fn is None:
            raise HTTPException(status_code=404, detail=f"unknown duty {duty!r}; one of {sorted(duties)}")
        result = await sentinel._run(duty, fn, settings())
        db.log_audit(actor="dashboard", action="sentinel_run", detail=duty)
        return {"duty": duty, "result": sentinel.last_result.get(duty), "value": result}

    @app.get("/api/sentinel/backups", dependencies=read)
    def sentinel_backups():
        return {"backups": backup.list_backups()}

    def _set_path(name: str) -> Path:
        if "/" in name or "\\" in name or name.startswith("."):
            raise HTTPException(status_code=400, detail="bad backup name")
        path = backup.BACKUPS_ROOT / name
        if not (path / backup.MANIFEST).is_file():
            raise HTTPException(status_code=404, detail="no such backup")
        return path

    @app.post("/api/sentinel/backups/{name}/verify", dependencies=write)
    async def sentinel_verify(name: str):
        problems = await asyncio.to_thread(backup.verify_backup, _set_path(name))
        return {"name": name, "sound": not problems, "problems": problems}

    @app.post("/api/sentinel/backups/{name}/restore", dependencies=write)
    async def sentinel_restore(name: str, payload: dict = Body(default={})):
        _set_path(name)
        parts = tuple(payload.get("parts") or ("db", "config", "secrets"))
        if not set(parts) <= {"db", "config", "secrets"}:
            raise HTTPException(status_code=400, detail="parts must be a subset of db, config, secrets")
        try:
            result = await asyncio.to_thread(backup.restore_backup, name, parts=parts)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        db.log_audit(actor="dashboard", action="sentinel_restore", detail=f"{name} parts={','.join(parts)}")
        journal.record("restore", f"restored {','.join(parts)} from backup {name} (dashboard)", level="warning")
        return result

    @app.get("/api/sentinel/cve", dependencies=read)
    def sentinel_cve():
        return cve.last_result() or {"scanned_at": None, "packages": 0, "findings": []}

    @app.post("/api/sentinel/cve/fix", dependencies=write)
    async def sentinel_cve_fix():
        last = cve.last_result()
        if not last:
            raise HTTPException(status_code=409, detail="no scan yet — run the cve duty first")
        outcomes = await asyncio.to_thread(cve.fix_python, last["findings"])
        db.log_audit(actor="dashboard", action="sentinel_cve_fix", detail=f"{len(outcomes)} package(s)")
        return {"outcomes": outcomes}

    @app.get("/api/sentinel/issues", dependencies=read)
    def sentinel_issues(status: Optional[str] = Query(None, pattern="^(open|resolved|ignored|regressed)$"),
                        limit: int = Query(100, ge=1, le=1000)):
        return {"issues": bug_hunter.issues(limit, status=status)}

    @app.post("/api/sentinel/issues/status", dependencies=write)
    def sentinel_issue_status(payload: dict = Body(...)):
        if not bug_hunter.set_status(str(payload.get("signature", "")), str(payload.get("status", ""))):
            raise HTTPException(status_code=404, detail="unknown signature or status")
        return {"ok": True}
