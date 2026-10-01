"""/api/fileserver: managing the ABP File Server (bot/fileserver) from the Storage page, the CLI, the TUI and MCP.
(The files themselves are served by the file server's own process: bot/fileserver/server.py.)

    GET    /api/fileserver                         overview: server, array, pools, shares, users, alerts, jobs, events
    GET|PUT /api/fileserver/settings               POST /server/start | /server/stop
    GET    /api/fileserver/disks                   drives (health, SMART, risk) and volumes;  GET /stats  CPU, memory, I/O
    GET    /api/fileserver/array                   PUT {disks, parity, block_kib, exclude, allow_same_drive}
    POST   /api/fileserver/array/{sync|scrub|fix}  -> a run   ({percent} for scrub; {disk, files, target} for fix)
    GET    /api/fileserver/pools                   PUT /pools/{name} {path}; DELETE /pools/{name}
    GET    /api/fileserver/shares                  POST {name, settings}; PATCH/DELETE /shares/{name};
                                                   GET /shares/{name}/smb?platform= | /nfs
    GET    /api/fileserver/users                   POST {name, password, admin}; DELETE /users/{name}
    GET    /api/fileserver/links                   DELETE /links/{token}
    POST   /api/fileserver/mover                   -> a run
    GET    /api/fileserver/index                   stats; POST /index (-> a run); GET /search?q=&mode=; GET /duplicates
    GET    /api/fileserver/guard                   alerts, frozen shares; PUT settings; POST /guard/unfreeze {shares?}
    GET    /api/fileserver/remotes                 PUT /remotes/{name} {kind, fields}; DELETE
    GET    /api/fileserver/transfers               PUT /transfers/{name} {job}; DELETE; POST /transfers/{name}/run (-> a run, ?dry=1)
    GET    /api/fileserver/backups                 PUT /backups/{name} {repo, sources, password, every_hours, keep};
                                                   POST /backups/{name}/run|check (-> a run); GET /backups/{name}/snapshots;
                                                   POST /backups/{name}/restore {snapshot, target, include}
    GET    /api/fileserver/apps                    POST /apps {app, name, mounts, ports, env} (-> a run: pulls images)
    GET    /api/fileserver/events                  GET /runs/{id}
"""
from __future__ import annotations

import asyncio
from typing import Callable

from fastapi import Body, Depends, FastAPI, HTTPException, Query

from bot.dashboard.hosting_api import _runs, start_run
from bot.fileserver import apps, array, backup, disks, exports, guard, index, mover, service, shares, transfer
from bot.fileserver.store import FsError, update


async def _t(fn, *args, **kwargs):
    try:
        return await asyncio.to_thread(fn, *args, **kwargs)
    except FsError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


def _run(title: str, fn) -> dict:
    """A background run (the same run registry as hosting); FsError becomes the run's error."""
    def go(log):
        try:
            return fn(log)
        except FsError as e:
            from bot.hosting.store import HostingError
            raise HostingError(str(e)) from e
    return start_run(title, go)


def register(app: FastAPI, require_token: Callable) -> None:
    dep = [Depends(require_token)]
    A = "/api/fileserver"

    @app.get(A, dependencies=dep)
    async def fs_overview(deep: bool = Query(False)):
        return await _t(service.overview, deep)

    @app.get(f"{A}/settings", dependencies=dep)
    async def fs_settings():
        return service.settings()

    @app.put(f"{A}/settings", dependencies=dep)
    async def fs_settings_put(body: dict = Body(...)):
        return await _t(service.set_settings, body)

    @app.post(f"{A}/server/{{action}}", dependencies=dep)
    async def fs_server(action: str):
        if action == "start":
            from bot.hosting.store import HostingError
            try:
                return await asyncio.to_thread(service.server_start)
            except HostingError as e:
                raise HTTPException(400, str(e)) from e
        if action == "stop":
            return {"stopped": await _t(service.server_stop)}
        raise HTTPException(404, "start or stop")

    @app.get(f"{A}/disks", dependencies=dep)
    async def fs_disks():
        return await _t(disks.inventory)

    @app.get(f"{A}/stats", dependencies=dep)
    async def fs_stats():
        return await _t(disks.system_stats)

    # -- array --
    @app.get(f"{A}/array", dependencies=dep)
    async def fs_array(changes: bool = Query(False)):
        return {"config": array.config(), "status": await _t(array.status, changes)}

    @app.put(f"{A}/array", dependencies=dep)
    async def fs_array_set(body: dict = Body(...)):
        return await _t(array.configure, body.get("disks") or [], body.get("parity") or [], int(body.get("block_kib", 256)),
                        body.get("exclude"), bool(body.get("allow_same_drive")))

    @app.post(f"{A}/array/{{action}}", dependencies=dep)
    async def fs_array_action(action: str, body: dict = Body(default={})):
        if action == "sync":
            return _run("Parity sync", lambda log: array.sync(log, bool(body.get("dry_run"))))
        if action == "scrub":
            return _run("Parity scrub", lambda log: array.scrub(float(body.get("percent", 100)), log))
        if action == "fix":
            return _run("Rebuild", lambda log: array.fix(body.get("disk") or None, body.get("files") or None, body.get("target") or None, log))
        raise HTTPException(404, "sync, scrub or fix")

    # -- pools, shares, users, links --
    @app.get(f"{A}/pools", dependencies=dep)
    async def fs_pools():
        return shares.pools()

    @app.put(f"{A}/pools/{{name}}", dependencies=dep)
    async def fs_pool_set(name: str, body: dict = Body(...)):
        return await _t(shares.set_pool, name, body.get("path", ""))

    @app.delete(f"{A}/pools/{{name}}", dependencies=dep)
    async def fs_pool_rm(name: str):
        return {"removed": await _t(shares.remove_pool, name)}

    @app.get(f"{A}/shares", dependencies=dep)
    async def fs_shares():
        return [{"name": n, **s} for n, s in shares.shares().items()]

    @app.post(f"{A}/shares", dependencies=dep)
    async def fs_share_add(body: dict = Body(...)):
        return await _t(shares.create, body.get("name", ""), body.get("settings") or {})

    @app.patch(f"{A}/shares/{{name}}", dependencies=dep)
    async def fs_share_edit(name: str, body: dict = Body(...)):
        return await _t(shares.edit, name, body)

    @app.delete(f"{A}/shares/{{name}}", dependencies=dep)
    async def fs_share_rm(name: str):
        return {"removed": await _t(shares.remove, name)}

    @app.get(f"{A}/shares/{{name}}/smb", dependencies=dep)
    async def fs_smb(name: str, platform: str = Query("")):
        return await _t(exports.smb, name, platform)

    @app.get(f"{A}/shares/{{name}}/nfs", dependencies=dep)
    async def fs_nfs(name: str, clients: str = Query("192.168.0.0/16")):
        return await _t(exports.nfs, name, clients)

    @app.get(f"{A}/users", dependencies=dep)
    async def fs_users():
        return shares.users()

    @app.post(f"{A}/users", dependencies=dep)
    async def fs_user_set(body: dict = Body(...)):
        return await _t(shares.set_user, body.get("name", ""), body.get("password", ""), bool(body.get("admin")))

    @app.delete(f"{A}/users/{{name}}", dependencies=dep)
    async def fs_user_rm(name: str):
        return {"removed": await _t(shares.remove_user, name)}

    @app.get(f"{A}/links", dependencies=dep)
    async def fs_links():
        return shares.links()

    @app.delete(f"{A}/links/{{token}}", dependencies=dep)
    async def fs_link_rm(token: str):
        return {"removed": shares.remove_link(token)}

    @app.post(f"{A}/mover", dependencies=dep)
    async def fs_mover(body: dict = Body(default={})):
        return _run("Mover", lambda log: mover.run(log, body.get("share", "")))

    # -- index, guard --
    @app.get(f"{A}/index", dependencies=dep)
    async def fs_index():
        return await _t(index.stats)

    @app.post(f"{A}/index", dependencies=dep)
    async def fs_index_run(body: dict = Body(default={})):
        names = [body["share"]] if body.get("share") else list(shares.shares())
        desc = service.settings()["describe_images"] if body.get("describe_images") is None else bool(body["describe_images"])
        return _run("Index", lambda log: [index.index_share(n, log, describe_images=desc) for n in names])

    @app.get(f"{A}/search", dependencies=dep)
    async def fs_search(q: str = Query(...), mode: str = Query("auto"), share: str = Query(""), kind: str = Query(""), limit: int = Query(50)):
        return await _t(index.search, q, [share] if share else None, mode, limit, kind)

    @app.get(f"{A}/duplicates", dependencies=dep)
    async def fs_dups(share: str = Query("")):
        return await _t(index.duplicates, [share] if share else None)

    @app.get(f"{A}/guard", dependencies=dep)
    async def fs_guard():
        return {"settings": guard.settings(), "frozen": sorted(guard.frozen()), "alerts": guard.recent_alerts()}

    @app.put(f"{A}/guard", dependencies=dep)
    async def fs_guard_set(body: dict = Body(...)):
        bad = set(body) - {"enabled", "alert_at", "auto_freeze"}
        if bad:
            raise HTTPException(400, f"unknown: {sorted(bad)}")
        update("guard", {}, lambda g: g.update(body))
        return guard.settings()

    @app.post(f"{A}/guard/unfreeze", dependencies=dep)
    async def fs_unfreeze(body: dict = Body(default={})):
        return {"frozen": guard.unfreeze(body.get("shares"))}

    # -- remotes, transfers, backups --
    @app.get(f"{A}/remotes", dependencies=dep)
    async def fs_remotes():
        return transfer.remotes()

    @app.put(f"{A}/remotes/{{name}}", dependencies=dep)
    async def fs_remote_set(name: str, body: dict = Body(...)):
        return await _t(transfer.set_remote, name, body.get("kind", ""), body.get("fields") or {})

    @app.delete(f"{A}/remotes/{{name}}", dependencies=dep)
    async def fs_remote_rm(name: str):
        return {"removed": transfer.remove_remote(name)}

    @app.get(f"{A}/transfers", dependencies=dep)
    async def fs_transfers():
        return {"jobs": transfer.jobs(), "history": transfer.history(30)}

    @app.put(f"{A}/transfers/{{name}}", dependencies=dep)
    async def fs_transfer_set(name: str, body: dict = Body(...)):
        return await _t(transfer.set_job, name, body)

    @app.delete(f"{A}/transfers/{{name}}", dependencies=dep)
    async def fs_transfer_rm(name: str):
        return {"removed": transfer.remove_job(name)}

    @app.post(f"{A}/transfers/{{name}}/run", dependencies=dep)
    async def fs_transfer_run(name: str, dry: bool = Query(False)):
        return _run(f"Transfer {name}", lambda log: transfer.run(name, log, dry))

    @app.get(f"{A}/backups", dependencies=dep)
    async def fs_backups():
        return backup.jobs()

    @app.put(f"{A}/backups/{{name}}", dependencies=dep)
    async def fs_backup_set(name: str, body: dict = Body(...)):
        return await _t(backup.set_job, name, body.get("repo", ""), body.get("sources") or [], body.get("password", ""),
                        float(body.get("every_hours", 24)), body.get("exclude"), body.get("keep"))

    @app.delete(f"{A}/backups/{{name}}", dependencies=dep)
    async def fs_backup_rm(name: str):
        return {"removed": backup.remove_job(name)}

    @app.post(f"{A}/backups/{{name}}/{{action}}", dependencies=dep)
    async def fs_backup_action(name: str, action: str, body: dict = Body(default={})):
        if action == "run":
            return _run(f"Backup {name}", lambda log: backup.run_job(name, log))
        if action == "check":
            return _run(f"Check backup {name}", lambda log: backup.check(backup.job_repo(name), float(body.get("read_percent", 5))))
        if action == "restore":
            return _run(f"Restore {name}", lambda log: backup.restore(backup.job_repo(name), body["snapshot"], body["target"],
                                                                     body.get("include", ""), log))
        raise HTTPException(404, "run, check or restore")

    @app.get(f"{A}/backups/{{name}}/snapshots", dependencies=dep)
    async def fs_snaps(name: str):
        return await _t(lambda: backup.job_repo(name).snapshots())

    @app.get(f"{A}/apps", dependencies=dep)
    async def fs_apps():
        return apps.catalog()

    @app.post(f"{A}/apps", dependencies=dep)
    async def fs_app_install(body: dict = Body(...)):
        await _t(apps.compose_for, body.get("app", ""), body.get("name", ""), body.get("mounts") or {}, body.get("ports"), body.get("env"))
        return _run(f"Install {body.get('app')}", lambda log: apps.install(body["app"], body["name"], body.get("mounts") or {},
                                                                           body.get("ports"), body.get("env")))

    @app.get(f"{A}/events", dependencies=dep)
    async def fs_events(limit: int = Query(100)):
        return service.events(limit)

    @app.get(f"{A}/runs/{{rid}}", dependencies=dep)
    async def fs_run(rid: str, since: int = Query(0, ge=0)):
        run = _runs.get(rid)
        if not run:
            raise HTTPException(404, "no such run")
        return {**{k: v for k, v in run.items() if k != "log"}, "log": run["log"][since:], "log_total": len(run["log"])}
