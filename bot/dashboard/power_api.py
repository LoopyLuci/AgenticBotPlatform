"""Power API: keeping this machine awake while in use, and waking other machines.

    GET  /api/power/status                        mode, holds, why it is awake right now, wake targets
    PUT  /api/power/settings                      {keep_awake, keep_display_on, idle_minutes, auto_wake}
    POST /api/power/hold                          {reason, minutes, key?}   keep awake for a while (0 = until released)
    POST /api/power/release                       {key?}                    drop one hold, or all
    GET  /api/power/info                          this machine's network cards (MAC, broadcast, wake enabled?)
    POST /api/power/wake                          {target} | {mac, broadcast} [, via: a linked server to send it from]
    POST /api/power/learn/{peer}                  ask a linked server for its cards and remember them for waking

A linked server may call these when this machine's owner allows the "power" area (peers.remote_control): that is how
one ABP keeps another awake while it is using it, or asks one on the same network to wake a third.
"""
from __future__ import annotations

import asyncio
from typing import Any, Callable

from fastapi import Body, Depends, FastAPI, HTTPException


def register(app: FastAPI, read_auth: Callable, write_auth: Callable) -> None:
    from bot import db, power

    read = [Depends(read_auth)]
    write = [Depends(write_auth)]

    @app.get("/api/power/status", dependencies=read)
    async def power_status():
        return power.keeper.state()

    @app.put("/api/power/settings", dependencies=write)
    async def power_settings(body: dict = Body(...)):
        try:
            out = await asyncio.to_thread(power.save_settings, **{k: v for k, v in body.items() if k != "wake"})
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        db.log_audit(actor="dashboard", action="power_settings", detail=str(body)[:300])
        return out

    @app.post("/api/power/hold", dependencies=write)
    async def power_hold(body: dict = Body(default={})):
        key = str(body.get("key") or body.get("reason") or "manual")[:80]
        return power.keeper.hold(key, str(body.get("reason") or "kept awake by request")[:200],
                                 float(body.get("minutes", 60)), by=str(body.get("by") or "")[:80])

    @app.post("/api/power/release", dependencies=write)
    async def power_release(body: dict = Body(default={})):
        return power.keeper.release(str(body.get("key") or ""))

    @app.get("/api/power/info", dependencies=read)
    async def power_info():
        return await asyncio.to_thread(power.info)

    @app.post("/api/power/wake", dependencies=write)
    async def power_wake(body: dict = Body(...)):
        via = body.get("via")
        if via:
            from bot import peers
            try:
                row = peers.find_peer(via)
                target = power.settings()["wake"].get(str(body.get("target") or ""), {})
                payload = {"mac": body.get("mac") or target.get("mac"), "broadcast": body.get("broadcast") or target.get("broadcast")}
                if not payload["mac"]:
                    raise HTTPException(status_code=400, detail="no MAC for that target")
                return {"relayed_by": row["name"], **(await peers.proxy(row, "POST", "/api/power/wake", payload))}
            except peers.PeerError as e:
                raise HTTPException(status_code=502, detail=str(e)) from e
        try:
            if body.get("target"):
                out = await asyncio.to_thread(power.wake_target, str(body["target"]))
            elif body.get("mac"):
                out = await asyncio.to_thread(power.wake, str(body["mac"]), str(body.get("broadcast") or "255.255.255.255"))
            else:
                raise HTTPException(status_code=400, detail="give target (a learned machine) or mac")
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        db.log_audit(actor="dashboard", action="power_wake", detail=str(out)[:300])
        return out

    @app.post("/api/power/learn/{peer}", dependencies=write)
    async def power_learn(peer: str) -> Any:
        from bot import peers
        try:
            return await power.learn_peer(peer)
        except peers.PeerError as e:
            raise HTTPException(status_code=502, detail=str(e)) from e
