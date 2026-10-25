"""Sentinel API: ad-blocking and firewall rules (bot/netguard.py; the engine is the Sentinel module).

    GET    /api/netguard/status                      engine, settings, DNS filter state and counters, blocklists
    PUT    /api/netguard/settings                    {adblock?: {...}, system_dns?: {...}, firewall?: {...}} (audited)
    GET    /api/netguard/queries?limit=&blocked=     the filter's recent queries, newest first
    POST   /api/netguard/check                       {"name"} would this name be blocked, and by which rule
    POST   /api/netguard/lists/refresh               {"id"?} download the switched-on lists (or one) (audited)
    GET    /api/netguard/firewall                    configured rules, Sentinel's rules on the host, the exact plan
    POST   /api/netguard/firewall/rules              a rule: add, or replace the one with that name (audited)
    PUT    /api/netguard/firewall/rules/{name}       {"enabled"} switch one rule (audited)
    DELETE /api/netguard/firewall/rules/{name}       (audited)
    POST   /api/netguard/firewall/apply              run the plan; only shows it while firewall.dry_run is on (audited)
    POST   /api/netguard/firewall/rollback           undo the last live apply (audited)
    GET    /api/netguard/system-dns                  adapters, their DNS servers, the plan to point or restore them
    POST   /api/netguard/system-dns/apply            run that plan; only shows it while system_dns.dry_run is on (audited)
    GET    /api/netguard/journal?limit=              what Sentinel did, newest first

Reading uses the dashboard's normal auth; every change needs the desktop dashboard token.
"""
from __future__ import annotations

import asyncio
from typing import Callable, Optional

from fastapi import Body, Depends, FastAPI, HTTPException, Query


def register(app: FastAPI, read_auth: Callable, write_auth: Callable) -> None:
    from bot import netguard as ng

    read = [Depends(read_auth)]
    write = [Depends(write_auth)]

    async def call(fn, *args, **kwargs):
        try:
            return await asyncio.to_thread(fn, *args, **kwargs)
        except ng.Unavailable as e:
            raise HTTPException(status_code=409, detail={"error": str(e), "code": "unavailable"}) from e
        except KeyError as e:
            raise HTTPException(status_code=404, detail=f"no rule called {e.args[0]!r}") from e
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e

    @app.get("/api/netguard/status", dependencies=read)
    async def netguard_status():
        return await call(ng.status)

    @app.put("/api/netguard/settings", dependencies=write)
    async def netguard_settings(body: dict = Body(...)):
        """Switch any Sentinel setting (each has its own control on the page)."""
        return await call(ng.save, body)

    @app.get("/api/netguard/queries", dependencies=read)
    async def netguard_queries(limit: int = Query(200, ge=1, le=1000), blocked: Optional[bool] = None):
        return {"queries": ng.service.queries(limit, blocked), "dns": ng.service.view()}

    @app.post("/api/netguard/check", dependencies=read)
    async def netguard_check(body: dict = Body(...)):
        name = str(body.get("name") or "").strip()
        if not name or len(name) > 253:
            raise HTTPException(status_code=400, detail="send the name to check")
        return await call(ng.service.check, name)

    @app.post("/api/netguard/lists/refresh", dependencies=write)
    async def netguard_lists_refresh(body: Optional[dict] = Body(None)):
        return await call(ng.refresh_lists, (body or {}).get("id") or None)

    @app.get("/api/netguard/firewall", dependencies=read)
    async def netguard_firewall():
        return await call(ng.firewall_view)

    @app.post("/api/netguard/firewall/rules", dependencies=write)
    async def netguard_rule_upsert(body: dict = Body(...)):
        return await call(ng.upsert_rule, body)

    @app.put("/api/netguard/firewall/rules/{name}", dependencies=write)
    async def netguard_rule_enabled(name: str, body: dict = Body(...)):
        if not isinstance(body.get("enabled"), bool):
            raise HTTPException(status_code=400, detail="enabled must be true or false")
        return await call(ng.set_rule_enabled, name, body["enabled"])

    @app.delete("/api/netguard/firewall/rules/{name}", dependencies=write)
    async def netguard_rule_delete(name: str):
        return await call(ng.delete_rule, name)

    @app.post("/api/netguard/firewall/apply", dependencies=write)
    async def netguard_firewall_apply():
        return await call(ng.firewall_apply)

    @app.post("/api/netguard/firewall/rollback", dependencies=write)
    async def netguard_firewall_rollback():
        return await call(ng.firewall_rollback)

    @app.get("/api/netguard/system-dns", dependencies=read)
    async def netguard_system_dns():
        return await call(ng.system_dns_view)

    @app.post("/api/netguard/system-dns/apply", dependencies=write)
    async def netguard_system_dns_apply():
        return await call(ng.system_dns_apply)

    @app.get("/api/netguard/journal", dependencies=read)
    async def netguard_journal(limit: int = Query(100, ge=1, le=1000)):
        return {"entries": await asyncio.to_thread(ng.journal_tail, limit)}
