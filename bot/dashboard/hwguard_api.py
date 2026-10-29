"""Hardware guard API (bot/hwguard, docs/hwguard.md): live readings, the governor's verdict, the WHEA history, settings.

    GET  /api/hwguard/state               the latest reading and verdict, the recent history, the slot holder, the settings
    POST /api/hwguard/sample              take a reading now
    PUT  /api/hwguard/settings            {enabled, monitor, whea_watch, alerts, exclusive, govern: {kind: bool}, caps: {kind: n},
                                           thresholds...}  (audited)
    POST /api/hwguard/ack                 {"note"} acknowledge the WHEA events seen so far, so heavy work may resume (audited)
    POST /api/hwguard/slot/release        free the heavy-work slot held by a process that is stuck (audited)

Reading uses the dashboard's normal auth; changing anything needs the dashboard token.
"""
from __future__ import annotations

import asyncio
from typing import Callable, Optional

from fastapi import Body, Depends, FastAPI, HTTPException


def register(app: FastAPI, read_auth: Callable, write_auth: Callable) -> None:
    from bot import db
    from bot.hwguard import governor, monitor, slot

    read = [Depends(read_auth)]
    write = [Depends(write_auth)]

    @app.get("/api/hwguard/state", dependencies=read)
    async def hwguard_state():
        st = monitor.state()
        if st["latest"] is None:
            await asyncio.to_thread(monitor.sample)
            st = monitor.state()
        return st

    @app.post("/api/hwguard/sample", dependencies=write)
    async def hwguard_sample():
        await asyncio.to_thread(monitor.sample)
        return monitor.state()

    @app.put("/api/hwguard/settings", dependencies=write)
    async def hwguard_settings(body: dict = Body(...)):
        try:
            clean = governor.validate(body)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        from bot.config import config

        changes = {}
        for k, v in clean.items():
            if isinstance(v, dict):
                changes.update({("hwguard", k, kk): vv for kk, vv in v.items()})
            else:
                changes[("hwguard", k)] = v
        await asyncio.to_thread(config.set_values, changes, "hwguard")
        db.log_audit(actor="dashboard", action="hwguard_settings", detail=str(clean)[:300])
        monitor.poke()
        return governor.settings()

    @app.post("/api/hwguard/ack", dependencies=write)
    async def hwguard_ack(body: Optional[dict] = Body(None)):
        rec = governor.acknowledge("dashboard", str((body or {}).get("note") or ""))
        db.log_audit(actor="dashboard", action="hwguard_whea_ack", detail=rec["note"][:300])
        await asyncio.to_thread(monitor.sample)
        return monitor.state()

    @app.post("/api/hwguard/slot/release", dependencies=write)
    async def hwguard_slot_release():
        cur = slot.holder()
        freed = slot.release(force=True)
        db.log_audit(actor="dashboard", action="hwguard_slot_release", detail=str(cur)[:300])
        return {"released": freed, "was": cur}
