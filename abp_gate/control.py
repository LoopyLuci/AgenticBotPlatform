"""The gate's own control API, on its own localhost port.

This is NOT the ABP API and does not pretend to be. It is the handful of verbs
that change what is running - start, swap, rollback, sandbox, stop - and it is
authenticated with the same DASHBOARD_TOKEN every ABP client already has, read
exactly the way bot/envfile.py resolves it for the CLI and the desktop app.

Three deliberate choices:

  * a separate port (8788). The public port must survive the gate stopping and
    starting instances; a control call that could restart the thing you are
    talking to over the same socket is a much worse failure mode than two
    ports.
  * loopback only, bind 127.0.0.1, like every other ABP listener. Anything that
    can reach this port can start processes on the machine.
  * hmac.compare_digest against the token, the same comparison
    bot/dashboard/server.py uses. The token is never logged, never echoed in a
    response, and never written to the registry - `abp_cli instance list` shows
    the environment VARIABLE NAME to export, never its value.
"""

from __future__ import annotations

import hmac
import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Optional

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse

from abp_gate import __version__, manager, paths, procs, registry
from abp_gate.proxy import Router

logger = logging.getLogger("abp_gate.control")

TOKEN_HEADER = "X-Dashboard-Token"


def require_token(x_dashboard_token: Optional[str] = Header(default=None)) -> None:
    expected = manager.dashboard_token()
    if not expected:
        # No token anywhere means no ABP has ever booted on this data root.
        # Refuse rather than serve an open API that can start processes.
        raise HTTPException(status_code=503, detail="no DASHBOARD_TOKEN in this install's .env yet")
    if not x_dashboard_token or not hmac.compare_digest(str(x_dashboard_token), expected):
        raise HTTPException(status_code=401, detail="bad or missing dashboard token")


def build_app(mgr: Optional[manager.Manager] = None) -> FastAPI:
    mgr = mgr or manager.Manager()
    app = FastAPI(title="abp_gate control", version=__version__)
    app.state.manager = mgr
    auth = [Depends(require_token)]

    # ------------------------------------------------------------------ gate
    @app.get("/api/gate")
    async def gate_status():
        data = mgr.list()
        return {
            "gate": {
                "version": __version__,
                "pid": os.getpid(),
                "code_root": str(paths.code_root()),
                "state_root": str(paths.state_root()),
                "instances_dir": str(paths.instances_dir()),
                "public_ports": paths.public_ports(),
                "control_port": paths.control_port(),
                "control_url": f"http://127.0.0.1:{paths.control_port()}",
                "public_url": f"http://127.0.0.1:{paths.public_ports()[0]}",
                "token_env_var": "DASHBOARD_TOKEN",
                "since": app.state.started,
                "routing": mgr.router.targets(),
                # What happens to the instances if this process is killed, and
                # how many there may be: both are answers somebody debugging
                # "where did that python.exe come from" needs to not have to
                # read the source for.
                "instance_lifetime": mgr.instance_lifetime,
                "limits": data.get("limits"),
                "restarts": data.get("restarts"),
            },
            **data,
        }

    @app.post("/api/gate/start", dependencies=auth)
    async def gate_start(code_root: Optional[str] = None, data_root: Optional[str] = None):
        return await mgr.start_production(
            code_root=Path(code_root) if code_root else None,
            data_root=Path(data_root) if data_root else None,
        )

    @app.post("/api/gate/restart-active", dependencies=auth)
    async def gate_restart_active():
        try:
            return await mgr.restart_active()
        except manager.SwapError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/api/gate/stop", dependencies=auth)
    async def gate_stop(keep_state: bool = True):
        """Stop every instance, then ask the gate itself to exit. The CLI's
        `gate stop`; the daemon does the rest."""
        result = await mgr.stop_all()
        hook = getattr(app.state, "on_stop", None)
        if callable(hook):
            hook()
        return {**result, "gate": "stopping"}

    # -------------------------------------------------------------- instances
    @app.get("/api/instance")
    async def instance_list():
        return mgr.list()

    @app.get("/api/instance/{name}/logs")
    async def instance_logs(name: str, lines: int = 120):
        if registry.get(name) is None:
            raise HTTPException(status_code=404, detail=f"no instance named {name!r}")
        return {"name": name, "lines": lines, "log": mgr.logs(name, lines)}

    @app.get("/api/instance/{name}/lease")
    async def instance_lease(name: str):
        """The active instance's own view of the leader lease - which of the
        two "is it up?" questions the gate cannot answer by itself."""
        inst = registry.get(name)
        if inst is None or not inst.port:
            raise HTTPException(status_code=404, detail=f"no instance named {name!r}")
        import httpx

        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(f"http://127.0.0.1:{inst.port}/api/lease",
                                        headers=await manager.dashboard_headers())
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=502, detail=f"{name} did not answer: {exc}") from exc
        if resp.status_code >= 400:
            raise HTTPException(status_code=resp.status_code, detail=resp.text[:200])
        return {"name": name, "lease": resp.json()}

    @app.post("/api/instance/swap", dependencies=auth)
    async def instance_swap(code_root: str, name: Optional[str] = None, data_root: Optional[str] = None):
        if not code_root:
            raise HTTPException(status_code=400, detail="code_root is required")
        try:
            return await mgr.swap(Path(code_root), name=name,
                                  data_root=Path(data_root) if data_root else None)
        except manager.SwapError as exc:
            # 409, not 500: the request was well-formed, the swap just could
            # not be completed, and the message says exactly why.
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/api/instance/rollback", dependencies=auth)
    async def instance_rollback():
        try:
            return await mgr.rollback()
        except manager.SwapError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/api/instance/sandbox", dependencies=auth)
    async def instance_sandbox(code_root: str, name: Optional[str] = None, keep_state: bool = False):
        if not code_root:
            raise HTTPException(status_code=400, detail="code_root is required")
        try:
            return await mgr.sandbox(Path(code_root), name=name, keep_state=keep_state)
        except manager.SwapError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/api/instance/stop", dependencies=auth)
    async def instance_stop(name: str, keep_state: bool = True):
        try:
            return await mgr.stop(name, keep_state=keep_state)
        except manager.SwapError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    # ---------------------------------------------------------------- health
    @app.get("/healthz")
    async def healthz():
        """Unauthenticated, like ABP's own /healthz: a liveness probe for the
        gate process itself. It reports the routing target but nothing an
        attacker could use - no paths, no pids, no config.

        200 means there is a HEALTHY instance behind the public port. An
        instance the watcher has given up on is `failed`, and reporting that as
        a 503 is the honest answer: the port is up, ABP is not."""
        data = mgr.list()
        active = data.get("active")
        current = (data.get("instances") or {}).get(active or "") or {}
        healthy = bool(active) and current.get("health") == registry.HEALTH_HEALTHY
        return JSONResponse(
            {
                "status": "ok" if healthy else "no healthy instance",
                "gate": __version__,
                "active": active,
                "routing": mgr.router.targets(),
                "healthy": healthy,
                "error": current.get("error") or "",
            },
            status_code=200 if healthy else 503,
        )

    app.state.started = time.time()
    # Set by the daemon (abp_gate.__main__) so POST /api/gate/stop can ask it to
    # exit without this module knowing what a daemon is.
    app.state.on_stop = None
    return app


def save_gate_meta(extra: Optional[dict] = None) -> None:
    """What the CLI reads to find a running gate: the pid, the control URL and
    the public URL. Deliberately NO token - this file is readable by anything
    that can read the data root, and it is not a secret store."""
    meta = {
        "pid": os.getpid(),
        "version": __version__,
        "control_url": f"http://127.0.0.1:{paths.control_port()}",
        "public_url": f"http://127.0.0.1:{paths.public_ports()[0]}",
        "control_port": paths.control_port(),
        "public_ports": paths.public_ports(),
        "code_root": str(paths.code_root()),
        "state_root": str(paths.state_root()),
        "instances_dir": str(paths.instances_dir()),
        "token_env_var": "DASHBOARD_TOKEN",
        "started": time.time(),
        **(extra or {}),
    }
    paths.gate_meta_path().write_text(json.dumps(meta, indent=1), encoding="utf-8")
    return meta


def read_gate_meta() -> dict[str, Any]:
    try:
        return json.loads(paths.gate_meta_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def gate_running() -> bool:
    meta = read_gate_meta()
    pid = meta.get("pid")
    if not pid:
        return False
    if not procs.alive(pid):
        return False
    # Alive is not enough: the pid could be a recycled stranger, and a stale
    # gate.json must never make the CLI think it is talking to a live gate.
    return procs.owns(pid, marker="abp_gate") or procs.owns(pid, marker="__main__.py")


__all__ = ["build_app", "require_token", "read_gate_meta", "save_gate_meta", "gate_running", "TOKEN_HEADER", "Router"]