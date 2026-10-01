"""/api/hosting: ABP Web Hosting (bot/hosting), for the Hosting page, the CLI, the TUI and MCP clients.

    GET    /api/hosting                                  overview: sites, the web server, tunnels, accounts, network
    GET    /api/hosting/network                          public / LAN addresses, NAT situation, advice, a recommendation
    GET    /api/hosting/settings                         PUT the same to change (engine, ports, ACME e-mail, CA...)
    GET    /api/hosting/providers                        what can be connected, and each provider's fields
    GET    /api/hosting/accounts                         POST {provider, name, values} to add; PATCH/DELETE /{id}
    POST   /api/hosting/accounts/{id}/verify
    GET    /api/hosting/sites                            POST {fields} to create; GET/PATCH/DELETE /sites/{id}
    GET    /api/hosting/sites/{id}/plan?mode=&account=   the go-live steps
    POST   /api/hosting/sites/{id}/go-live  {mode, account, agree_ca_terms}  -> a run
    POST   /api/hosting/sites/{id}/publish  {account?}                       -> a run
    POST   /api/hosting/sites/{id}/check                 DNS / http / https / certificate from outside
    POST   /api/hosting/edge/start | /edge/stop          GET /edge (status), GET /edge/log
    GET    /api/hosting/dns/{account}/zones              GET /dns/{account}/records?zone=; PUT {zone,name,type,values,ttl,proxied}; DELETE ?zone&name&type
    GET    /api/hosting/tunnels                          POST /tunnels/cloudflared/install, /tunnels/cloudflare/{run,stop}
    GET    /api/hosting/upnp                             POST {external_port, internal_port, protocol}; DELETE ?port&protocol
    GET    /api/hosting/certs                            POST {names, method, agree_ca_terms} -> a run (a certificate)
    GET    /api/hosting/servers/{account}/options        GET /servers/{account}; POST {name, region, size, confirm_monthly} -> a run;
                                                         DELETE /servers/{account}/{id}?confirm=<name>
    POST   /api/hosting/servers/{ssh account}/setup      install Caddy on a server -> a run
    GET    /api/hosting/runs/{id}                        a run's log lines and, when done, its result

Everything needs the dashboard token: these calls spend money (servers), publish things (deploys, DNS) and open the
machine to the internet (port forwards, tunnels).
"""
from __future__ import annotations

import asyncio
import threading
import time
import uuid
from typing import Any, Callable

from fastapi import Body, Depends, FastAPI, HTTPException, Query

from bot.hosting import accounts, acme, dns, netinfo, procs, service, tunnels, upnp
from bot.hosting.store import HostingError

_runs: dict[str, dict] = {}
_RUN_KEEP_S = 3600


def start_run(title: str, fn: Callable[[Callable[[str], None]], Any]) -> dict:
    """Run fn(log) in a thread; the page polls /runs/{id} for the log and the result."""
    rid = uuid.uuid4().hex[:12]
    run = {"id": rid, "title": title, "started": time.time(), "done": False, "log": [], "result": None, "error": None}
    _runs[rid] = run
    for k in [k for k, v in _runs.items() if v["done"] and time.time() - v["started"] > _RUN_KEEP_S]:
        _runs.pop(k, None)

    def log(msg: str) -> None:
        run["log"].append(f"{time.strftime('%H:%M:%S')} {msg}")

    def go():
        try:
            run["result"] = fn(log)
        except HostingError as e:
            run["error"] = str(e)
            log(f"failed: {e}")
        except Exception as e:  # noqa: BLE001 - shown to the person, never swallowed
            run["error"] = f"{type(e).__name__}: {e}"
            log(f"failed: {run['error']}")
        finally:
            run["done"] = True
            run["finished"] = time.time()
    threading.Thread(target=go, name=f"hosting-run-{rid}", daemon=True).start()
    return {"run": rid, "title": title}


async def _t(fn, *args, **kwargs):
    try:
        return await asyncio.to_thread(fn, *args, **kwargs)
    except HostingError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


def overview() -> dict:
    return {"sites": [service.public(service.get(sid)) for sid in service.sites()], "edge": service.edge_status(),
            "tunnels": tunnels.overview(), "accounts": accounts.listing(), "settings": service.settings(),
            "certs": acme.certificates()}


def register(app: FastAPI, require_token: Callable) -> None:
    dep = [Depends(require_token)]

    @app.get("/api/hosting", dependencies=dep)
    async def h_overview():
        return await _t(overview)

    @app.get("/api/hosting/network", dependencies=dep)
    async def h_network():
        return await _t(service.recommend)

    @app.get("/api/hosting/settings", dependencies=dep)
    async def h_settings():
        return service.settings()

    @app.put("/api/hosting/settings", dependencies=dep)
    async def h_settings_put(body: dict = Body(...)):
        return await _t(service.set_settings, body)

    @app.get("/api/hosting/providers", dependencies=dep)
    async def h_providers():
        return {k: {"label": v["label"], "caps": v["caps"], "help": v.get("help", ""),
                    "fields": [{"key": f[0], "label": f[1], "secret": f[2], "required": f[3]} for f in v["fields"]]}
                for k, v in accounts.PROVIDERS.items()}

    # -- accounts --
    @app.get("/api/hosting/accounts", dependencies=dep)
    async def h_accounts(cap: str = Query("")):
        return accounts.listing(cap or None)

    @app.post("/api/hosting/accounts", dependencies=dep)
    async def h_account_add(body: dict = Body(...)):
        acc = await _t(accounts.add, body.get("provider", ""), body.get("name", ""), body.get("values") or {})
        if body.get("verify", True):
            acc["verify"] = await _t(accounts.verify, acc["id"])
        return acc

    @app.patch("/api/hosting/accounts/{acc_id}", dependencies=dep)
    async def h_account_edit(acc_id: str, body: dict = Body(...)):
        return await _t(accounts.edit, acc_id, body)

    @app.delete("/api/hosting/accounts/{acc_id}", dependencies=dep)
    async def h_account_rm(acc_id: str):
        return {"removed": await _t(accounts.remove, acc_id)}

    @app.post("/api/hosting/accounts/{acc_id}/verify", dependencies=dep)
    async def h_account_verify(acc_id: str):
        return await _t(accounts.verify, acc_id)

    # -- sites --
    @app.get("/api/hosting/sites", dependencies=dep)
    async def h_sites():
        return [service.public(service.get(sid)) for sid in service.sites()]

    @app.post("/api/hosting/sites", dependencies=dep)
    async def h_site_add(body: dict = Body(...)):
        return service.public(await _t(service.create, body))

    @app.get("/api/hosting/sites/{sid}", dependencies=dep)
    async def h_site(sid: str):
        return service.public(await _t(service.get, sid))

    @app.patch("/api/hosting/sites/{sid}", dependencies=dep)
    async def h_site_edit(sid: str, body: dict = Body(...)):
        return service.public(await _t(service.edit, sid, body))

    @app.delete("/api/hosting/sites/{sid}", dependencies=dep)
    async def h_site_rm(sid: str):
        return {"removed": await _t(service.remove, sid)}

    @app.get("/api/hosting/sites/{sid}/plan", dependencies=dep)
    async def h_plan(sid: str, mode: str = Query(""), account: str = Query("")):
        return await _t(service.plan, sid, mode or None, account or None)

    @app.post("/api/hosting/sites/{sid}/go-live", dependencies=dep)
    async def h_go_live(sid: str, body: dict = Body(default={})):
        site = await _t(service.get, sid)
        return start_run(f"Go live: {site['name']}", lambda log: service.go_live(
            site["id"], body.get("mode") or None, body.get("account") or None, log, body.get("agree_ca_terms")))

    @app.post("/api/hosting/sites/{sid}/publish", dependencies=dep)
    async def h_publish(sid: str, body: dict = Body(default={})):
        site = await _t(service.get, sid)
        return start_run(f"Publish: {site['name']}", lambda log: service.publish(site["id"], body.get("account") or None, log))

    @app.post("/api/hosting/sites/{sid}/check", dependencies=dep)
    async def h_check(sid: str):
        return await _t(service.check, sid)

    # -- the web server --
    @app.get("/api/hosting/edge", dependencies=dep)
    async def h_edge():
        return await _t(service.edge_status)

    @app.post("/api/hosting/edge/start", dependencies=dep)
    async def h_edge_start():
        return await _t(service.edge_start)

    @app.post("/api/hosting/edge/stop", dependencies=dep)
    async def h_edge_stop():
        return {"stopped": await _t(service.edge_stop)}

    @app.get("/api/hosting/edge/log", dependencies=dep)
    async def h_edge_log(name: str = Query("edge"), lines: int = Query(120, ge=1, le=2000)):
        if name not in ("edge", "caddy", "cloudflared"):
            raise HTTPException(400, "the log is edge, caddy or cloudflared")
        return {"name": name, "text": procs.tail(name, lines)}

    # -- DNS --
    @app.get("/api/hosting/dns/{acc_id}/zones", dependencies=dep)
    async def h_zones(acc_id: str):
        return await _t(lambda: dns.provider(acc_id).zones())

    @app.get("/api/hosting/dns/{acc_id}/records", dependencies=dep)
    async def h_records(acc_id: str, zone: str = Query(...)):
        return await _t(lambda: dns.provider(acc_id).records(zone))

    @app.put("/api/hosting/dns/{acc_id}/records", dependencies=dep)
    async def h_record_set(acc_id: str, body: dict = Body(...)):
        vals = body.get("values")
        if isinstance(vals, str):
            vals = [v.strip() for v in vals.split(",") if v.strip()]
        return await _t(lambda: dns.provider(acc_id).set(body["zone"], body.get("name", "@"), body.get("type", "A"), vals or [],
                                                         int(body.get("ttl") or 300), body.get("proxied")))

    @app.delete("/api/hosting/dns/{acc_id}/records", dependencies=dep)
    async def h_record_rm(acc_id: str, zone: str = Query(...), name: str = Query(...), type: str = Query(...)):  # noqa: A002
        return {"removed": await _t(lambda: dns.provider(acc_id).delete(zone, name, type))}

    @app.get("/api/hosting/resolve", dependencies=dep)
    async def h_resolve(name: str = Query(...), type: str = Query("A")):  # noqa: A002
        return {"name": name, "type": type, "values": await _t(netinfo.resolve, name, type)}

    # -- tunnels, router --
    @app.get("/api/hosting/tunnels", dependencies=dep)
    async def h_tunnels():
        return await _t(tunnels.overview)

    @app.post("/api/hosting/tunnels/cloudflared/install", dependencies=dep)
    async def h_cfd_install():
        return start_run("Install cloudflared", lambda log: tunnels.install_cloudflared())

    @app.post("/api/hosting/tunnels/cloudflare/run", dependencies=dep)
    async def h_cf_run():
        return await _t(tunnels.cf_run)

    @app.post("/api/hosting/tunnels/cloudflare/stop", dependencies=dep)
    async def h_cf_stop():
        return {"stopped": await _t(tunnels.cf_stop)}

    @app.get("/api/hosting/upnp", dependencies=dep)
    async def h_upnp():
        def go():
            try:
                gw = upnp.discover()
                return {"available": True, "router": {k: gw.get(k) for k in ("maker", "model", "service")},
                        "external_ip": upnp.external_ip_safe(), "mappings": upnp.mappings()}
            except HostingError as e:
                return {"available": False, "reason": str(e)}
        return await asyncio.to_thread(go)

    @app.post("/api/hosting/upnp", dependencies=dep)
    async def h_upnp_add(body: dict = Body(...)):
        return await _t(upnp.add, int(body["external_port"]), int(body.get("internal_port") or body["external_port"]),
                        body.get("protocol", "TCP"), body.get("description", "web"))

    @app.delete("/api/hosting/upnp", dependencies=dep)
    async def h_upnp_rm(port: int = Query(...), protocol: str = Query("TCP")):
        return {"removed": await _t(upnp.remove, port, protocol)}

    # -- certificates --
    @app.get("/api/hosting/certs", dependencies=dep)
    async def h_certs():
        return await _t(acme.certificates)

    @app.post("/api/hosting/certs", dependencies=dep)
    async def h_cert(body: dict = Body(...)):
        st = service.settings()
        agreed = bool(body.get("agree_ca_terms") or st["agreed_ca_terms"])
        if body.get("agree_ca_terms"):
            service.set_settings({"agreed_ca_terms": True})
        names = body.get("names") or []
        if isinstance(names, str):
            names = [n for n in names.replace(",", " ").split() if n]
        return start_run(f"Certificate: {', '.join(names)}", lambda log: acme.issue(
            names, method=body.get("method", "http-01"), ca=body.get("ca") or st["ca"], email=body.get("email") or st["acme_email"],
            agree_tos=agreed, eab=body.get("eab"), directory=body.get("directory", ""), log=log))

    # -- servers --
    @app.get("/api/hosting/servers/{acc_id}/options", dependencies=dep)
    async def h_srv_options(acc_id: str):
        from bot.hosting import vps
        return await _t(lambda: vps.provider(acc_id).options())

    @app.get("/api/hosting/servers/{acc_id}", dependencies=dep)
    async def h_servers(acc_id: str):
        from bot.hosting import vps
        return await _t(lambda: vps.provider(acc_id).servers())

    @app.post("/api/hosting/servers/{acc_id}", dependencies=dep)
    async def h_srv_create(acc_id: str, body: dict = Body(...)):
        from bot.hosting import vps
        p = await _t(vps.provider, acc_id)
        if body.get("confirm_monthly") is None:
            raise HTTPException(400, "creating a server costs money: confirm its monthly price (confirm_monthly)")
        return start_run(f"Create server {body.get('name')}", lambda log: p.create(
            body["name"], body["region"], body["size"], confirm_monthly=float(body["confirm_monthly"]), image=body.get("image")))

    @app.delete("/api/hosting/servers/{acc_id}/{server_id}", dependencies=dep)
    async def h_srv_destroy(acc_id: str, server_id: str, confirm: str = Query(...)):
        from bot.hosting import vps
        p = await _t(vps.provider, acc_id)
        names = {str(s["id"]): s["name"] for s in await _t(p.servers)}
        if names.get(str(server_id)) != confirm:
            raise HTTPException(400, "type the server's name to confirm destroying it (everything on it is lost)")
        return {"destroyed": await _t(p.destroy, server_id)}

    @app.post("/api/hosting/servers/{acc_id}/setup", dependencies=dep)
    async def h_srv_setup(acc_id: str):
        from bot.hosting import deploy
        acc = await _t(accounts.get, acc_id)
        if acc["provider"] != "ssh":
            raise HTTPException(400, "set up a server through its SSH account")
        return start_run(f"Set up {acc['name']}", lambda log: deploy.server_setup(acc, log))

    @app.get("/api/hosting/runs/{rid}", dependencies=dep)
    async def h_run(rid: str, since: int = Query(0, ge=0)):
        run = _runs.get(rid)
        if not run:
            raise HTTPException(404, "no such run (runs are kept for an hour)")
        return {**{k: v for k, v in run.items() if k != "log"}, "log": run["log"][since:], "log_total": len(run["log"])}

    @app.get("/api/hosting/runs", dependencies=dep)
    async def h_runs():
        return sorted(({k: v for k, v in r.items() if k != "log"} for r in _runs.values()), key=lambda r: -r["started"])[:30]
