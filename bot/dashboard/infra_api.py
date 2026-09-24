"""`/api/docker/*` (Portainer-style container management) and `/api/vms/*`
(QEMU / Hyper-V / libvirt). Strict desktop-token auth only; every mutating
call is audit-logged. Thin wrappers over bot/docker_mgr.py and bot/vm_mgr.py."""
from __future__ import annotations

import asyncio
from typing import Callable, Optional

import httpx
from fastapi import Body, Depends, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, Response

from bot import db, docker_mgr as dk, terminal_broker as tb, vm_mgr as vm

# Routes that can be pointed at a linked server with ?host=<peer id>.
HOSTED_PREFIXES = ("/api/docker", "/api/vms", "/api/tailscale", "/api/infra/rules", "/api/terminals")
FORWARD_TIMEOUT_S = 900.0


async def _call(fn, *args, audit: str = "", detail: str = "", **kw):
    try:
        result = await asyncio.to_thread(fn, *args, **kw)
    except (dk.DockerError, vm.VMError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except (ValueError, TypeError, KeyError) as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    if audit:
        db.log_audit(actor="dashboard", action=audit, detail=detail[:300])
    return result


def _unknown(kind: str, name: str):
    raise HTTPException(status_code=404, detail=f"unknown {kind} '{name}'")


def _reader(fn):
    async def handler():
        return await _call(fn)
    return handler


def _peer_or_404(host: str):
    try:
        row = db.get_peer_server(int(host))
    except (TypeError, ValueError):
        row = None
    if row is None or not row["base_url"] or not row["outbound_api_key"]:
        raise HTTPException(status_code=404, detail=f"no linked server with id '{host}'")
    return row


def register(app: FastAPI, auth: Callable, ws_token_ok: Callable[[Optional[str]], bool], *,
             desktop_token_ok: Callable[[Optional[str]], bool], set_peer_access: Callable[[bool], None],
             peer_access_enabled: Callable[[], bool]) -> None:
    dep = [Depends(auth)]
    D, V = "/api/docker", "/api/vms"


    # ---- multi-host: ?host=<linked server id> sends any infra request to that server instead of this one.
    @app.middleware("http")
    async def route_to_host(request: Request, call_next):
        host = request.query_params.get("host")
        path = request.url.path
        if not host or host == "local" or not path.startswith(HOSTED_PREFIXES):
            return await call_next(request)
        # Only the desktop token may fan out to other machines; a linked peer can never be used as a relay.
        if not desktop_token_ok(request.headers.get("x-dashboard-token")):
            return JSONResponse({"detail": "invalid dashboard token"}, status_code=401)
        try:
            peer = _peer_or_404(host)
        except HTTPException as exc:
            return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
        params = [(k, v) for k, v in request.query_params.multi_items() if k != "host"]
        headers = {"X-Dashboard-Token": peer["outbound_api_key"]}
        if request.headers.get("content-type"):
            headers["Content-Type"] = request.headers["content-type"]
        try:
            async with httpx.AsyncClient(timeout=FORWARD_TIMEOUT_S) as client:
                up = await client.request(request.method, peer["base_url"].rstrip("/") + path, params=params,
                                          content=await request.body(), headers=headers)
        except httpx.HTTPError as exc:
            db.mark_peer_server_error(peer["id"], str(exc))
            return JSONResponse({"detail": f"could not reach the linked server: {exc}"}, status_code=502)
        db.mark_peer_server_ok(peer["id"])
        if up.status_code == 401:
            return JSONResponse({"detail": "that server has not allowed remote management - on it, turn on "
                                           "'Allow linked servers to manage this machine' (Containers page)"},
                                status_code=403)
        if request.method != "GET":
            db.log_audit(actor="dashboard", action="infra_remote_call",
                         detail=f"{peer['name']}: {request.method} {path}"[:300])
        return Response(up.content, status_code=up.status_code, media_type=up.headers.get("content-type"))

    @app.get("/api/infra/hosts", dependencies=[Depends(auth)])
    async def infra_hosts():
        rows = await asyncio.to_thread(db.list_peer_servers)
        return {"hosts": [{"id": "local", "name": "This machine", "local": True}] + [
            {"id": str(r["id"]), "name": r["name"], "base_url": r["base_url"], "last_seen_at": r["last_seen_at"],
             "last_error": r["last_error"], "local": False}
            for r in rows if r["outbound_api_key"] and r["base_url"]]}

    @app.get("/api/infra/peer-access", dependencies=[Depends(auth)])
    async def infra_peer_access_get():
        return {"enabled": peer_access_enabled()}

    @app.post("/api/infra/peer-access")
    async def infra_peer_access_set(request: Request, body: dict = Body(...)):
        # Strict desktop token: a linked server must not be able to grant itself access.
        if not desktop_token_ok(request.headers.get("x-dashboard-token")):
            raise HTTPException(status_code=401, detail="invalid dashboard token")
        on = bool(body.get("enabled"))
        await asyncio.to_thread(set_peer_access, on)
        db.log_audit(actor="dashboard", action="infra_peer_access", detail="enabled" if on else "disabled")
        return {"enabled": on}

    for name, fn in (("info", dk.info), ("df", dk.disk_usage), ("containers", dk.containers), ("images", dk.images),
                     ("volumes", dk.volumes), ("networks", dk.networks), ("stacks", dk.stacks),
                     ("registries", dk.registries), ("templates", dk.templates), ("stats", dk.container_stats),
                     ("events", dk.events)):
        app.add_api_route(f"{D}/{name}", _reader(fn), methods=["GET"], dependencies=dep)

    # ---- containers
    @app.get(f"{D}/containers/{{ident}}", dependencies=dep)
    async def dk_container(ident: str):
        return await _call(dk.container, ident)

    @app.get(f"{D}/containers/{{ident}}/logs", dependencies=dep)
    async def dk_logs(ident: str, tail: int = 200, since: str = "", timestamps: bool = False):
        return await _call(dk.container_logs, ident, tail, since or None, timestamps)

    @app.get(f"{D}/containers/{{ident}}/stats", dependencies=dep)
    async def dk_cstats(ident: str):
        return await _call(dk.container_stats, ident)

    @app.get(f"{D}/containers/{{ident}}/top", dependencies=dep)
    async def dk_top(ident: str):
        return await _call(dk.container_top, ident)

    @app.get(f"{D}/containers/{{ident}}/files", dependencies=dep)
    async def dk_files(ident: str, path: str = "/"):
        return await _call(dk.container_files, ident, path)

    @app.post(f"{D}/containers", dependencies=dep)
    async def dk_create(body: dict = Body(...)):
        return await _call(dk.container_create, audit="docker_container_create", detail=str(body.get("image")), **body)

    @app.post(f"{D}/containers/{{ident}}/action", dependencies=dep)
    async def dk_action(ident: str, body: dict = Body(...)):
        return await _call(dk.container_action, ident, body.get("action", ""),
                           audit="docker_container_" + str(body.get("action")), detail=ident)

    @app.post(f"{D}/containers/{{ident}}/exec", dependencies=dep)
    async def dk_exec(ident: str, body: dict = Body(...)):
        return await _call(dk.container_exec, ident, body.get("command", []), user=body.get("user"),
                           workdir=body.get("workdir"), timeout=float(body.get("timeout", 60)),
                           audit="docker_exec", detail=f"{ident}: {' '.join(map(str, body.get('command', [])))}")

    @app.post(f"{D}/containers/{{ident}}/update", dependencies=dep)
    async def dk_update(ident: str, body: dict = Body(...)):
        return await _call(dk.container_update, ident, audit="docker_container_update", detail=ident, **body)

    @app.post(f"{D}/containers/{{ident}}/rename", dependencies=dep)
    async def dk_rename(ident: str, body: dict = Body(...)):
        return await _call(dk.container_rename, ident, body.get("name", ""), audit="docker_container_rename", detail=ident)

    @app.post(f"{D}/containers/{{ident}}/commit", dependencies=dep)
    async def dk_commit(ident: str, body: dict = Body(...)):
        return await _call(dk.container_commit, ident, body.get("repository", ""), audit="docker_commit", detail=ident)

    @app.post(f"{D}/containers/{{ident}}/copy", dependencies=dep)
    async def dk_copy(ident: str, body: dict = Body(...)):
        return await _call(dk.container_copy, ident, body.get("container_path", ""), body.get("host_dir", ""),
                           to_container=bool(body.get("to_container", False)), audit="docker_copy", detail=ident)

    # ---- images
    @app.get(f"{D}/images/inspect", dependencies=dep)
    async def dk_image(ref: str):
        return await _call(dk.image, ref)

    @app.get(f"{D}/images/history", dependencies=dep)
    async def dk_image_history(ref: str):
        return await _call(dk.image_history, ref)

    @app.get(f"{D}/images/search", dependencies=dep)
    async def dk_search(term: str, limit: int = 25):
        return await _call(dk.image_search, term, limit)

    @app.post(f"{D}/images/{{op}}", dependencies=dep)
    async def dk_image_op(op: str, body: dict = Body(...)):
        table = {
            "pull": lambda: dk.image_pull(body.get("ref", "")),
            "remove": lambda: dk.image_remove(body.get("ref", ""), bool(body.get("force", False))),
            "tag": lambda: dk.image_tag(body.get("source", ""), body.get("target", "")),
            "push": lambda: dk.image_push(body.get("ref", "")),
            "build": lambda: dk.image_build(body.get("context", ""), body.get("tag", ""), body.get("dockerfile"),
                                            body.get("build_args"), bool(body.get("no_cache", False))),
        }
        if op not in table:
            _unknown("image operation", op)
        return await _call(table[op], audit=f"docker_image_{op}", detail=str(body.get("ref") or body.get("tag")))

    # ---- volumes / networks
    @app.post(f"{D}/volumes", dependencies=dep)
    async def dk_vol_create(body: dict = Body(...)):
        return await _call(dk.volume_create, body.get("name", ""), body.get("driver", "local"), body.get("labels"),
                           audit="docker_volume_create", detail=str(body.get("name")))

    @app.get(f"{D}/volumes/{{name}}", dependencies=dep)
    async def dk_vol(name: str):
        return await _call(dk.volume, name)

    @app.delete(f"{D}/volumes/{{name}}", dependencies=dep)
    async def dk_vol_rm(name: str, force: bool = False):
        return await _call(dk.volume_remove, name, force, audit="docker_volume_remove", detail=name)

    @app.post(f"{D}/networks", dependencies=dep)
    async def dk_net_create(body: dict = Body(...)):
        return await _call(dk.network_create, body.get("name", ""), body.get("driver", "bridge"), body.get("subnet"),
                           body.get("gateway"), bool(body.get("internal", False)),
                           audit="docker_network_create", detail=str(body.get("name")))

    @app.get(f"{D}/networks/{{name}}", dependencies=dep)
    async def dk_net(name: str):
        return await _call(dk.network, name)

    @app.delete(f"{D}/networks/{{name}}", dependencies=dep)
    async def dk_net_rm(name: str):
        return await _call(dk.network_remove, name, audit="docker_network_remove", detail=name)

    @app.post(f"{D}/networks/{{name}}/connect", dependencies=dep)
    async def dk_net_connect(name: str, body: dict = Body(...)):
        return await _call(dk.network_connect, name, body.get("container", ""), bool(body.get("connect", True)),
                           audit="docker_network_connect", detail=name)

    # ---- stacks
    @app.post(f"{D}/stacks", dependencies=dep)
    async def dk_stack_deploy(body: dict = Body(...)):
        return await _call(dk.stack_deploy, body.get("name", ""), body.get("compose", ""), body.get("env"),
                           audit="docker_stack_deploy", detail=str(body.get("name")))

    @app.get(f"{D}/stacks/{{name}}", dependencies=dep)
    async def dk_stack_get(name: str):
        return await _call(dk.stack_get, name)

    @app.get(f"{D}/stacks/{{name}}/services", dependencies=dep)
    async def dk_stack_services(name: str):
        return await _call(dk.stack_services, name)

    @app.get(f"{D}/stacks/{{name}}/logs", dependencies=dep)
    async def dk_stack_logs(name: str, tail: int = 200):
        return await _call(dk.stack_logs, name, tail)

    @app.post(f"{D}/stacks/{{name}}/action", dependencies=dep)
    async def dk_stack_action(name: str, body: dict = Body(...)):
        return await _call(dk.stack_action, name, body.get("action", ""),
                           audit="docker_stack_" + str(body.get("action")), detail=name)

    # ---- registries / templates / prune
    @app.post(f"{D}/registries/login", dependencies=dep)
    async def dk_login(body: dict = Body(...)):
        return await _call(dk.registry_login, body.get("server", ""), body.get("username", ""),
                           body.get("password", ""), audit="docker_registry_login", detail=str(body.get("server")))

    @app.post(f"{D}/registries/logout", dependencies=dep)
    async def dk_logout(body: dict = Body(...)):
        return await _call(dk.registry_logout, body.get("server", ""), audit="docker_registry_logout")

    @app.post(f"{D}/templates/{{template_id}}/deploy", dependencies=dep)
    async def dk_template(template_id: str, body: dict = Body(default={})):
        return await _call(dk.deploy_template, template_id, body.get("name"), body.get("overrides"),
                           audit="docker_template_deploy", detail=template_id)

    @app.post(f"{D}/prune", dependencies=dep)
    async def dk_prune(body: dict = Body(...)):
        return await _call(dk.prune, body.get("kind", ""), bool(body.get("all", False)),
                           bool(body.get("volumes", False)), audit="docker_prune", detail=str(body.get("kind")))

    # ---- VMs
    @app.get(f"{V}/backends", dependencies=dep)
    async def vm_backends():
        return await _call(vm.backends)

    @app.get(f"{V}/paths", dependencies=dep)
    async def vm_paths():
        return await _call(vm.default_paths)

    @app.get(V, dependencies=dep)
    async def vm_list():
        return await _call(vm.list_all)

    @app.post(f"{V}/qemu", dependencies=dep)
    async def vm_define(body: dict = Body(...)):
        name = body.pop("name", "")
        return await _call(vm.qemu_define, name, audit="vm_define", detail=name, **body)

    @app.get(f"{V}/qemu/{{name}}", dependencies=dep)
    async def vm_get(name: str):
        return await _call(vm.qemu_get, name)

    @app.delete(f"{V}/qemu/{{name}}", dependencies=dep)
    async def vm_delete(name: str, delete_disks: bool = False):
        return await _call(vm.qemu_delete, name, delete_disks, audit="vm_delete", detail=name)

    @app.post(f"{V}/qemu/{{name}}/{{action}}", dependencies=dep)
    async def vm_action(name: str, action: str, body: dict = Body(default={})):
        table = {
            "start": lambda: vm.qemu_start(name),
            "stop": lambda: vm.qemu_stop(name, bool(body.get("force", False))),
            "pause": lambda: vm.qemu_control(name, "pause"),
            "resume": lambda: vm.qemu_control(name, "resume"),
            "reset": lambda: vm.qemu_control(name, "reset"),
            "screenshot": lambda: vm.qemu_screenshot(name),
            "keys": lambda: vm.qemu_send_keys(name, body.get("keys", [])),
            "media": lambda: vm.qemu_change_media(name, body.get("iso")),
            "balloon": lambda: vm.qemu_balloon(name, int(body.get("memory_mb", 0))),
            "monitor": lambda: vm.qemu_monitor(name, body.get("command", "")),
            "snapshot": lambda: vm.qemu_snapshot(name, body.get("action", "list"), body.get("tag", "")),
        }
        if action not in table:
            _unknown("VM action", action)
        return await _call(table[action], audit="" if action == "screenshot" else f"vm_{action}", detail=name)

    @app.post(f"{V}/disks/{{op}}", dependencies=dep)
    async def vm_disk(op: str, body: dict = Body(...)):
        table = {
            "create": lambda: vm.disk_create(body.get("path", ""), body.get("size", ""), body.get("format", "qcow2"),
                                             body.get("backing"), bool(body.get("preallocate", False))),
            "info": lambda: vm.disk_info(body.get("path", "")),
            "resize": lambda: vm.disk_resize(body.get("path", ""), body.get("size", "")),
            "convert": lambda: vm.disk_convert(body.get("source", ""), body.get("dest", ""), body.get("format", ""),
                                               bool(body.get("compress", False))),
            "check": lambda: vm.disk_check(body.get("path", "")),
        }
        if op not in table:
            _unknown("disk operation", op)
        return await _call(table[op], audit=f"vm_disk_{op}", detail=str(body.get("path") or body.get("source")))

    @app.post(f"{V}/hyperv", dependencies=dep)
    async def hv_create(body: dict = Body(...)):
        return await _call(vm.hv_create, audit="hyperv_create", detail=str(body.get("name")), **body)

    @app.post(f"{V}/hyperv/{{name}}/{{action}}", dependencies=dep)
    async def hv_action(name: str, action: str):
        return await _call(vm.hv_action, name, action, audit=f"hyperv_{action}", detail=name)

    @app.get(f"{V}/hyperv/{{name}}/checkpoints", dependencies=dep)
    async def hv_checkpoints(name: str):
        return await _call(vm.hv_checkpoints, name)

    @app.get(f"{V}/hyperv-switches", dependencies=dep)
    async def hv_switches():
        return await _call(vm.hv_switches)

    @app.post(f"{V}/libvirt/{{name}}/{{action}}", dependencies=dep)
    async def lv_action(name: str, action: str, body: dict = Body(default={})):
        if action == "snapshot":
            return await _call(vm.lv_snapshot, name, body.get("action", "list"), body.get("tag", ""),
                               audit="libvirt_snapshot", detail=name)
        return await _call(vm.lv_action, name, action, audit=f"libvirt_{action}", detail=name)

    @app.get(f"{V}/libvirt/{{name}}", dependencies=dep)
    async def lv_info(name: str):
        return await _call(vm.lv_info, name)

    # ---- automation rules
    from bot import infra_automation as auto

    def _guard(fn):
        def run(*a, **k):
            try:
                return fn(*a, **k)
            except auto.RuleError as exc:
                raise ValueError(str(exc))
        return run

    A = "/api/infra/rules"

    @app.get(A, dependencies=dep)
    async def rules_list():
        return await _call(auto.list_rules)

    @app.post(A, dependencies=dep)
    async def rules_create(body: dict = Body(...)):
        return await _call(_guard(auto.create), body.get("name", ""), body.get("trigger", {}), body.get("action", {}),
                           int(body.get("cooldown_s", 300)), bool(body.get("enabled", True)),
                           audit="infra_rule_create", detail=str(body.get("name")))

    @app.post(f"{A}/{{rule_id}}/run", dependencies=dep)
    async def rules_run(rule_id: int):
        return await _call(_guard(auto.run_rule), rule_id, force=True, audit="infra_rule_run_now", detail=str(rule_id))

    @app.post(f"{A}/{{rule_id}}/enable", dependencies=dep)
    async def rules_enable(rule_id: int, body: dict = Body(default={})):
        return await _call(_guard(auto.set_enabled), rule_id, bool(body.get("enabled", True)),
                           audit="infra_rule_enable", detail=str(rule_id))

    @app.get(f"{A}/{{rule_id}}/history", dependencies=dep)
    async def rules_history(rule_id: int):
        return await _call(auto.history, rule_id)

    @app.delete(f"{A}/{{rule_id}}", dependencies=dep)
    async def rules_delete(rule_id: int):
        return await _call(auto.delete, rule_id, audit="infra_rule_delete", detail=str(rule_id))

    # ---- interactive terminals (container shell, VM serial console / monitor, libvirt console)
    @app.get("/api/terminals", dependencies=dep)
    async def term_list():
        return await _call(tb.list_sessions)

    @app.websocket("/api/terminals/ws")
    async def term_ws(websocket: WebSocket, kind: str, target: str, token: Optional[str] = None, cols: int = 80,
                      rows: int = 24, shell: str = "auto", user: Optional[str] = None, host: Optional[str] = None):
        # Strict desktop token only (a query param, since browsers can't set WebSocket headers): paired
        # phones and linked peers must never get a shell inside a container or VM.
        supplied = websocket.headers.get("x-dashboard-token") or token
        if not ws_token_ok(supplied):
            await websocket.close(code=4401)
            return
        if host and host != "local":
            if not desktop_token_ok(supplied):
                await websocket.close(code=4401)
                return
            await websocket.accept()
            await _relay_terminal(websocket, host, kind, target, cols, rows, shell, user)
            return
        await websocket.accept()
        try:
            session = await asyncio.to_thread(tb.open_session, kind, target, cols=cols, rows=rows, shell=shell, user=user)
        except (tb.TerminalError, dk.DockerError, vm.VMError, OSError, ImportError) as exc:
            await websocket.send_json({"type": "error", "message": str(exc)})
            await websocket.close()
            return
        await websocket.send_json({"type": "ready", "id": session.id, "kind": kind, "target": target})

        async def pump():
            try:
                while True:
                    data = await asyncio.to_thread(session.read)
                    if data is None:
                        break
                    if data:
                        await websocket.send_json({"type": "output", "data": data})
                await websocket.send_json({"type": "exit"})
            except Exception:
                pass

        pump_task = asyncio.create_task(pump())
        try:
            while True:
                msg = await websocket.receive_json()
                if msg.get("type") == "input" and isinstance(msg.get("data"), str):
                    await asyncio.to_thread(session.write, msg["data"])
                elif msg.get("type") == "resize":
                    await asyncio.to_thread(session.resize, int(msg.get("cols", 80)), int(msg.get("rows", 24)))
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            await asyncio.to_thread(tb.close_session, session)
            pump_task.cancel()


async def _relay_terminal(websocket: WebSocket, host: str, kind: str, target: str, cols: int, rows: int,
                          shell: str, user: Optional[str]) -> None:
    """Pipe a browser terminal to the same terminal WebSocket on a linked server."""
    import json
    from urllib.parse import urlencode

    import websockets

    try:
        peer = _peer_or_404(host)
    except HTTPException as exc:
        await websocket.send_json({"type": "error", "message": str(exc.detail)})
        await websocket.close()
        return
    q = {"kind": kind, "target": target, "cols": cols, "rows": rows, "shell": shell, "token": peer["outbound_api_key"]}
    if user:
        q["user"] = user
    url = peer["base_url"].rstrip("/").replace("http", "ws", 1) + "/api/terminals/ws?" + urlencode(q)
    try:
        async with websockets.connect(url, max_size=None) as upstream:
            async def down():
                async for raw in upstream:
                    await websocket.send_text(raw if isinstance(raw, str) else raw.decode())

            down_task = asyncio.create_task(down())
            try:
                while True:
                    await upstream.send(json.dumps(await websocket.receive_json()))
            except (WebSocketDisconnect, RuntimeError):
                pass
            finally:
                down_task.cancel()
    except Exception as exc:  # refused (401/403 => access not enabled), unreachable, ...
        refused = any(code in str(exc) for code in ("401", "403", "4401"))
        text = ("that server refused the connection - has it enabled 'Allow linked servers to manage this machine'?"
                if refused else f"could not reach the linked server: {exc}")
        try:
            await websocket.send_json({"type": "error", "message": text})
            await websocket.close()
        except Exception:
            pass
