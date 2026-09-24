"""`/api/tailscale/*` — every Tailscale feature bot/tailscale_mgr.py implements.

Strict desktop-token auth only (never paired-device or peer keys): these
routes can change network exposure (Funnel), ACLs and auth keys. Every
mutating call is written to the audit log; secrets are never logged.
"""
from __future__ import annotations

import asyncio
from typing import Any, Callable, Optional

from fastapi import Body, Depends, FastAPI, HTTPException, Query

from bot import db, tailscale_mgr as ts


async def _call(fn, *args, audit: Optional[str] = None, detail: str = "", **kwargs):
    try:
        result = await asyncio.to_thread(fn, *args, **kwargs)
    except ts.TailscaleError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    if audit:
        db.log_audit(actor="dashboard", action=f"tailscale_{audit}", detail=detail[:300])
    return result


def _reader(fn):
    async def handler():
        return await _call(fn)
    return handler


def register(app: FastAPI, auth: Callable) -> None:
    dep = [Depends(auth)]
    P = "/api/tailscale"

    @app.get(f"{P}/overview", dependencies=dep)
    async def ts_overview():
        out: dict[str, Any] = {"installed": ts.is_installed(), "prefs_schema": {k: v[1] for k, v in ts.PREFS.items()}}
        if out["installed"]:
            for key, fn in (("status", ts.status), ("prefs", ts.prefs), ("version", ts.version)):
                try:
                    out[key] = await asyncio.to_thread(fn)
                except ts.TailscaleError as exc:
                    out[key] = {"error": str(exc)}
        return out

    for name, fn in (("status", ts.status), ("prefs", ts.prefs), ("ips", ts.ips), ("netcheck", ts.netcheck),
                     ("exit-nodes", ts.exit_nodes), ("version", ts.version), ("metrics", ts.metrics),
                     ("dns-status", ts.dns_status), ("accounts", ts.list_accounts), ("lock", ts.lock_status),
                     ("drive", ts.drive_list), ("app-connector-routes", ts.app_connector_routes),
                     ("file-targets", ts.file_targets), ("update-check", ts.update)):
        app.add_api_route(f"{P}/{name}", _reader(fn), methods=["GET"], dependencies=dep)

    @app.get(f"{P}/whois", dependencies=dep)
    async def ts_whois(address: str):
        return await _call(ts.whois, address)

    @app.get(f"{P}/ping", dependencies=dep)
    async def ts_ping(target: str, count: int = 3):
        return await _call(ts.ping, target, count)

    # ---- settings & connection
    @app.post(f"{P}/prefs", dependencies=dep)
    async def ts_set_prefs(body: dict = Body(...)):
        return await _call(ts.set_prefs, body, audit="set_prefs", detail=",".join(sorted(body)))

    @app.post(f"{P}/up", dependencies=dep)
    async def ts_up(body: dict = Body(default={})):
        return await _call(ts.up, body.get("auth_key"), body.get("hostname"), body.get("prefs"), audit="up")

    @app.post(f"{P}/down", dependencies=dep)
    async def ts_down():
        return await _call(ts.down, audit="down")

    @app.post(f"{P}/login", dependencies=dep)
    async def ts_login(body: dict = Body(default={})):
        return await _call(ts.login, body.get("auth_key"), audit="login")

    @app.post(f"{P}/logout", dependencies=dep)
    async def ts_logout():
        return await _call(ts.logout, audit="logout")

    @app.post(f"{P}/switch", dependencies=dep)
    async def ts_switch(body: dict = Body(...)):
        return await _call(ts.switch_account, body.get("account", ""), audit="switch")

    @app.post(f"{P}/update", dependencies=dep)
    async def ts_update(body: dict = Body(default={})):
        return await _call(ts.update, bool(body.get("check_only", False)), audit="update")

    # ---- Serve / Funnel
    @app.get(f"{P}/serve", dependencies=dep)
    async def ts_serve_status(funnel: bool = False):
        return await _call(ts.serve_status, funnel=funnel)

    @app.post(f"{P}/serve", dependencies=dep)
    async def ts_serve_set(body: dict = Body(...)):
        funnel = bool(body.get("funnel", False))
        return await _call(
            ts.serve_set, body.get("target", ""), funnel=funnel, mode=body.get("mode", "https"),
            port=int(body.get("port", 443)), path=body.get("path"),
            audit="funnel_on" if funnel else "serve_on",
            detail=f"{body.get('mode', 'https')}:{body.get('port', 443)} -> {body.get('target')}",
        )

    @app.post(f"{P}/serve/off", dependencies=dep)
    async def ts_serve_off(body: dict = Body(default={})):
        funnel = bool(body.get("funnel", False))
        return await _call(ts.serve_off, funnel=funnel, mode=body.get("mode", "https"),
                           port=int(body.get("port", 443)), path=body.get("path"),
                           audit="funnel_off" if funnel else "serve_off")

    @app.post(f"{P}/serve/reset", dependencies=dep)
    async def ts_serve_reset(body: dict = Body(default={})):
        return await _call(ts.serve_reset, funnel=bool(body.get("funnel", False)), audit="serve_reset")

    @app.get(f"{P}/serve/config", dependencies=dep)
    async def ts_serve_get_config():
        return await _call(ts.serve_get_config)

    @app.post(f"{P}/serve/config", dependencies=dep)
    async def ts_serve_set_config(body: dict = Body(...)):
        import json
        return await _call(ts.serve_set_config, json.dumps(body), audit="serve_set_config")

    # ---- certs / files / drive
    @app.post(f"{P}/cert", dependencies=dep)
    async def ts_cert(body: dict = Body(...)):
        return await _call(ts.cert, body.get("domain", ""), audit="cert")

    @app.post(f"{P}/file/send", dependencies=dep)
    async def ts_file_send(body: dict = Body(...)):
        return await _call(ts.file_send, body.get("paths", []), body.get("target", ""), audit="file_send")

    @app.post(f"{P}/file/receive", dependencies=dep)
    async def ts_file_receive(body: dict = Body(...)):
        return await _call(ts.file_receive, body.get("directory", ""), audit="file_receive")

    @app.post(f"{P}/drive/share", dependencies=dep)
    async def ts_drive_share(body: dict = Body(...)):
        return await _call(ts.drive_share, body.get("name", ""), body.get("path", ""), audit="drive_share")

    @app.post(f"{P}/drive/unshare", dependencies=dep)
    async def ts_drive_unshare(body: dict = Body(...)):
        return await _call(ts.drive_unshare, body.get("name", ""), audit="drive_unshare")

    # ---- control-plane API (needs TAILSCALE_API_KEY)
    @app.get(f"{P}/api/devices", dependencies=dep)
    async def ts_devices():
        return await _call(ts.devices)

    @app.get(f"{P}/api/devices/{{device_id}}", dependencies=dep)
    async def ts_device(device_id: str):
        return await _call(ts.device, device_id)

    @app.delete(f"{P}/api/devices/{{device_id}}", dependencies=dep)
    async def ts_device_delete(device_id: str):
        return await _call(ts.device_delete, device_id, audit="device_delete", detail=device_id)

    @app.post(f"{P}/api/devices/{{device_id}}/{{action}}", dependencies=dep)
    async def ts_device_action(device_id: str, action: str, body: dict = Body(default={})):
        table = {
            "authorize": lambda: ts.device_authorize(device_id, body.get("authorized", True)),
            "expire": lambda: ts.device_expire(device_id),
            "tags": lambda: ts.device_set_tags(device_id, body.get("tags", [])),
            "name": lambda: ts.device_set_name(device_id, body.get("name", "")),
            "key-expiry": lambda: ts.device_set_key_expiry(device_id, bool(body.get("disabled", False))),
            "routes": lambda: ts.device_set_routes(device_id, body.get("routes", [])),
        }
        if action not in table:
            raise HTTPException(status_code=404, detail=f"unknown device action '{action}'")
        return await _call(table[action], audit=f"device_{action}", detail=device_id)

    @app.get(f"{P}/api/devices/{{device_id}}/routes", dependencies=dep)
    async def ts_device_routes(device_id: str):
        return await _call(ts.device_routes, device_id)

    @app.get(f"{P}/api/acl", dependencies=dep)
    async def ts_acl_get():
        return await _call(ts.acl_get)

    @app.post(f"{P}/api/acl/validate", dependencies=dep)
    async def ts_acl_validate(body: dict = Body(...)):
        return await _call(ts.acl_validate, body)

    @app.post(f"{P}/api/acl", dependencies=dep)
    async def ts_acl_set(body: dict = Body(...)):
        return await _call(ts.acl_set, body, audit="acl_set")

    @app.get(f"{P}/api/dns", dependencies=dep)
    async def ts_dns_get():
        return await _call(ts.dns_get)

    @app.post(f"{P}/api/dns", dependencies=dep)
    async def ts_dns_set(body: dict = Body(...)):
        res: dict[str, Any] = {}
        if "nameservers" in body:
            res["nameservers"] = await _call(ts.dns_set_nameservers, body["nameservers"], audit="dns_nameservers")
        if "search_paths" in body:
            res["search_paths"] = await _call(ts.dns_set_search_paths, body["search_paths"], audit="dns_search")
        if "magic_dns" in body:
            res["magic_dns"] = await _call(ts.dns_set_magic, bool(body["magic_dns"]), audit="dns_magic")
        if "split_dns" in body:
            res["split_dns"] = await _call(ts.dns_set_split, body["split_dns"], audit="dns_split")
        return res

    @app.get(f"{P}/api/keys", dependencies=dep)
    async def ts_keys():
        return await _call(ts.keys_list)

    @app.post(f"{P}/api/keys", dependencies=dep)
    async def ts_key_create(body: dict = Body(default={})):
        return await _call(
            ts.key_create, reusable=bool(body.get("reusable", False)), ephemeral=bool(body.get("ephemeral", False)),
            preauthorized=bool(body.get("preauthorized", True)), tags=body.get("tags"),
            expiry_seconds=int(body.get("expiry_seconds", 3600)), description=body.get("description", ""),
            audit="key_create",
        )

    @app.delete(f"{P}/api/keys/{{key_id}}", dependencies=dep)
    async def ts_key_delete(key_id: str):
        return await _call(ts.key_delete, key_id, audit="key_delete", detail=key_id)

    @app.get(f"{P}/api/settings", dependencies=dep)
    async def ts_settings_get():
        return await _call(ts.settings_get)

    @app.patch(f"{P}/api/settings", dependencies=dep)
    async def ts_settings_set(body: dict = Body(...)):
        return await _call(ts.settings_set, body, audit="tailnet_settings", detail=",".join(sorted(body)))

    @app.get(f"{P}/api/users", dependencies=dep)
    async def ts_users():
        return await _call(ts.users)

    @app.get(f"{P}/api/webhooks", dependencies=dep)
    async def ts_webhooks():
        return await _call(ts.webhooks)

    @app.api_route(f"{P}/api/raw/{{path:path}}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"], dependencies=dep,
                   include_in_schema=False)
    async def ts_raw(path: str, method: str = Query("GET"), body: Any = Body(default=None)):
        """Any other allow-listed control-plane endpoint (webhooks, posture, invites, services, log streaming...)."""
        m = method.upper()
        return await _call(ts.api, m, path, body, audit=None if m == "GET" else "api_raw", detail=f"{m} {path}")
