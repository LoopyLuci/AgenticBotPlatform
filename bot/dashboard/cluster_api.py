"""Cluster API: this machine as a cluster node, and the cluster seen from here.

Any linked peer may call the node routes (that is how the cluster works); what it may do is decided by this
machine's offer, which only this machine's owner can change (desktop dashboard token).

  The node (this machine)
    GET  /api/cluster/node                       what this machine has, offers, has free, and runs (peers poll this)
    GET  /api/cluster/offer                      this machine's offer
    PUT  /api/cluster/offer                      change it (desktop dashboard only)
    POST /api/cluster/runs                       take a job (reserve its share, or 409 with the reason)
    GET  /api/cluster/runs                       jobs this machine runs (a peer sees only its own)
    GET  /api/cluster/runs/{id}                  one of them
    POST /api/cluster/runs/{id}/start | /cancel
    GET  /api/cluster/runs/{id}/logs?offset=     its log, from a byte offset (for following it)
    GET  /api/cluster/runs/{id}/files/{name}     a result file (from its out/ folder)

  The cluster (scheduling from this machine; dashboard and paired phones)
    GET  /api/cluster                            every node: health, hardware, offer, free share, load
    POST /api/cluster/refresh                    ask every peer for its report now
    POST /api/cluster/candidates                 which nodes could take a job, and why the others can't
    POST /api/cluster/jobs                       submit a job (scheduled onto the best node)
    POST /api/cluster/groups                     submit a gang (replicas) or an array (count)
    GET  /api/cluster/jobs,  GET /api/cluster/jobs/{id},  GET /api/cluster/jobs/{id}/logs?offset=
    POST /api/cluster/jobs/{id}/cancel
    GET  /api/cluster/groups,  GET /api/cluster/groups/{id}
"""
from __future__ import annotations

import asyncio
import hashlib
from typing import Any, Callable, Optional

from fastapi import Body, Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import FileResponse


def _peer_name(key: str) -> Optional[str]:
    """Which linked server a peer key belongs to."""
    from bot import db
    h = hashlib.sha256((key or "").encode("utf-8")).hexdigest()
    row = db.get_conn().execute(
        "SELECT p.name FROM peer_servers p JOIN api_keys k ON k.id = p.inbound_api_key_id "
        "WHERE k.key_hash=? AND k.revoked_at IS NULL", (h,)).fetchone()
    return row["name"] if row else None


def register(app: FastAPI, identify: Callable) -> None:
    from bot.cluster import executor, membership, offer, scheduler, store
    from bot.cluster.executor import JobError
    from bot.cluster.scheduler import ScheduleError

    class Caller:
        def __init__(self, kind: str, peer: Optional[str]) -> None:
            self.kind, self.peer = kind, peer

    def caller(kind: str = Depends(identify), x_dashboard_token: Optional[str] = Header(None)) -> Caller:
        if kind == "bridge":
            raise HTTPException(status_code=403, detail="a browser-extension key cannot use the cluster")
        peer = _peer_name(x_dashboard_token or "") if kind == "peer" else None
        if kind == "peer" and not peer:
            raise HTTPException(status_code=403, detail="unknown peer")
        return Caller(kind, peer)

    def owner(c: Caller = Depends(caller)) -> Caller:
        if c.kind != "dashboard":
            raise HTTPException(status_code=403, detail="only this machine's owner (the desktop dashboard) can do this")
        return c

    def local_user(c: Caller = Depends(caller)) -> Caller:
        if c.kind == "peer":
            raise HTTPException(status_code=403, detail="a linked server schedules from its own ABP")
        return c

    def scheduler_writer(c: Caller = Depends(caller)) -> Caller:
        if c.kind != "dashboard":
            raise HTTPException(status_code=403, detail="submitting jobs needs the desktop dashboard token")
        return c

    async def run(fn, *args, **kwargs) -> Any:
        try:
            res = fn(*args, **kwargs)
            if asyncio.iscoroutine(res):
                return await res
            return res
        except JobError as e:
            raise HTTPException(status_code=e.status, detail={"error": str(e), "code": e.code}) from e
        except ScheduleError as e:
            raise HTTPException(status_code=409, detail={"error": str(e), "code": "unschedulable",
                                                         "reasons": e.reasons}) from e
        except ValueError as e:
            raise HTTPException(status_code=400, detail={"error": str(e), "code": "invalid"}) from e

    def own_run(rid: str, c: Caller) -> dict:
        j = executor.get(rid)
        if c.peer is not None and (j.get("peer") or "").lower() != c.peer.lower():
            raise HTTPException(status_code=404, detail={"error": f"no job {rid}", "code": "not_found"})
        return j

    # ---- the node ------------------------------------------------------------------------------------------------------
    @app.get("/api/cluster/node")
    async def cluster_node(c: Caller = Depends(caller)):
        return await asyncio.to_thread(membership.this_node)

    @app.get("/api/cluster/offer")
    async def cluster_offer(c: Caller = Depends(caller)):
        return {"offer": offer.current(), "capacity": offer.budget.capacity(), "free": offer.budget.free(),
                "work_dir": str(offer.work_root())}

    @app.put("/api/cluster/offer")
    async def cluster_offer_set(body: dict = Body(...), c: Caller = Depends(owner)):
        from bot import db
        new = await run(asyncio.to_thread, offer.save, body)
        db.log_audit(actor="dashboard", action="cluster_offer", detail=str({k: body[k] for k in list(body)[:12]})[:500])
        return {"offer": new}

    @app.post("/api/cluster/runs")
    async def cluster_run_accept(body: dict = Body(...), c: Caller = Depends(caller)):
        if c.kind == "mobile":
            raise HTTPException(status_code=403, detail="a phone schedules through the cluster routes")
        if c.peer:
            from bot import power
            power.keeper.note_activity(f"a cluster job from {c.peer}")
        return await run(asyncio.to_thread, executor.accept, body, c.peer)

    @app.get("/api/cluster/runs")
    async def cluster_runs(limit: int = Query(100, ge=1, le=1000), c: Caller = Depends(caller)):
        rows = store.list_("runs", limit=limit)
        return [r for r in rows if c.peer is None or (r.get("peer") or "").lower() == c.peer.lower()]

    @app.get("/api/cluster/runs/{rid}")
    async def cluster_run(rid: str, c: Caller = Depends(caller)):
        return await run(own_run, rid, c)

    @app.post("/api/cluster/runs/{rid}/start")
    async def cluster_run_start(rid: str, c: Caller = Depends(caller)):
        await run(own_run, rid, c)
        return await run(asyncio.to_thread, executor.start, rid)

    @app.post("/api/cluster/runs/{rid}/cancel")
    async def cluster_run_cancel(rid: str, c: Caller = Depends(caller)):
        await run(own_run, rid, c)
        return await run(asyncio.to_thread, executor.cancel, rid)

    @app.get("/api/cluster/runs/{rid}/logs")
    async def cluster_run_logs(rid: str, offset: int = Query(0, ge=0), c: Caller = Depends(caller)):
        await run(own_run, rid, c)
        return await run(asyncio.to_thread, executor.logs, rid, offset)

    @app.get("/api/cluster/runs/{rid}/files/{name:path}")
    async def cluster_run_file(rid: str, name: str, c: Caller = Depends(caller)):
        await run(own_run, rid, c)
        p = await run(executor.file_path, rid, name)
        return FileResponse(str(p), filename=p.name)

    # ---- the cluster, from here ----------------------------------------------------------------------------------------
    @app.get("/api/cluster")
    async def cluster_overview(c: Caller = Depends(local_user)):
        return {"nodes": await asyncio.to_thread(membership.nodes)}

    @app.post("/api/cluster/refresh")
    async def cluster_refresh(c: Caller = Depends(local_user)):
        await membership.poll_all()
        return {"nodes": await asyncio.to_thread(membership.nodes)}

    @app.post("/api/cluster/candidates")
    async def cluster_candidates(body: dict = Body(...), c: Caller = Depends(local_user)):
        from bot.cluster.offer import Request
        fits, why_not = scheduler.candidates(Request.from_dict(body.get("req")), str(body.get("kind") or "command"))
        return {"fits": [n["name"] for n in fits], "why_not": why_not}

    @app.post("/api/cluster/jobs")
    async def cluster_submit(body: dict = Body(...), c: Caller = Depends(scheduler_writer)):
        from bot import db
        p = await run(scheduler.submit, body)
        db.log_audit(actor="dashboard", action="cluster_submit", detail=f"{p['id']} {p['kind']} -> {p.get('node')}")
        return p

    @app.post("/api/cluster/groups")
    async def cluster_group(body: dict = Body(...), c: Caller = Depends(scheduler_writer)):
        from bot import db
        g = await run(scheduler.submit_group if body.get("replicas") else scheduler.submit_array, body)
        db.log_audit(actor="dashboard", action="cluster_group", detail=f"{g['id']} {g['kind']}")
        return g

    @app.get("/api/cluster/jobs")
    async def cluster_jobs(limit: int = Query(100, ge=1, le=1000), c: Caller = Depends(local_user)):
        return store.list_("placements", limit=limit)

    @app.get("/api/cluster/jobs/{pid}")
    async def cluster_job(pid: str, c: Caller = Depends(local_user)):
        return await run(scheduler.refresh, pid)

    @app.get("/api/cluster/jobs/{pid}/logs")
    async def cluster_job_logs(pid: str, offset: int = Query(0, ge=0), c: Caller = Depends(local_user)):
        return await run(scheduler.logs, pid, offset)

    @app.post("/api/cluster/jobs/{pid}/cancel")
    async def cluster_job_cancel(pid: str, c: Caller = Depends(scheduler_writer)):
        return await run(scheduler.cancel, pid)

    @app.get("/api/cluster/groups")
    async def cluster_groups(limit: int = Query(50, ge=1, le=500), c: Caller = Depends(local_user)):
        return [{k: v for k, v in g.items() if k != "spec"} for g in store.list_("groups", limit=limit)]

    @app.get("/api/cluster/groups/{gid}")
    async def cluster_group_get(gid: str, c: Caller = Depends(local_user)):
        return await run(scheduler.group_results, gid)
