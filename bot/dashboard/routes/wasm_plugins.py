"""Dashboard routes: WebAssembly plugins.

Four things a person can do from here that they cannot usefully do from a terminal: see what
an installed plugin can do and what it was actually granted, install one, switch one on or
off, and try one tool call before letting an agent near it.

The approval case is the point of the install route. `POST /api/wasm-plugins/install`
compiles the component and returns its manifest *without enabling it* - the tools, the
directories it asked for, the hosts it asked for - plus the grants that would be given if
everything were approved. A second call with the approval lists enables it. That is the same
shape as every other install ABP does: show the capabilities, then act on the answer.

    GET    /api/wasm-plugins              list, with what each asked for and was granted
    GET    /api/wasm-plugins/status        whether the feature is on at all, and why not
    POST   /api/wasm-plugins/install       read a component's manifest; optionally enable it
    POST   /api/wasm-plugins/{name}/enable
    POST   /api/wasm-plugins/{name}/disable
    POST   /api/wasm-plugins/{name}/uninstall
    POST   /api/wasm-plugins/{name}/invoke one tool call, for trying a plugin by hand
"""

from __future__ import annotations

import logging

from fastapi import Body, Depends, FastAPI, HTTPException

from bot import db
from bot.wasm_plugins import manager, runtime

logger = logging.getLogger("bot.dashboard.wasm_plugins")


def _fail(exc: Exception) -> HTTPException:
    """A refusal from the wasm plugin layer, as a 400. One helper so every route in this
    module says the same thing about the same kind of failure."""
    return HTTPException(status_code=400, detail=str(exc))


def register(app: FastAPI) -> None:
    from bot.dashboard.server import _require_token_or_api_key

    @app.get("/api/wasm-plugins/status", dependencies=[Depends(_require_token_or_api_key)])
    async def api_wasm_plugins_status():
        """Whether the feature can run, and what is active.

        `available` false means the native module is not built (or ABP_NATIVE=0). `reason`
        says which, because "the feature is off" with no reason is the worst of both.
        """
        module = runtime.require()
        return {
            "available": runtime.available(),
            "native_disabled": runtime.native_disabled(),
            "wasmtime": module.wasmtime_version() if module is not None else None,
            "version": module.__version__ if module is not None else None,
            "count": len(manager.list_plugins()) if runtime.available() else 0,
        }

    @app.get("/api/wasm-plugins", dependencies=[Depends(_require_token_or_api_key)])
    async def api_wasm_plugins_list():
        return {"plugins": manager.list_plugins() if runtime.available() else []}

    @app.post("/api/wasm-plugins/install", dependencies=[Depends(_require_token_or_api_key)])
    async def api_wasm_plugins_install(payload: dict = Body(...)):
        """Install a plugin from a local path or an https URL.

        Without `enable`, this only reads and records it: the response carries the manifest's
        capabilities (`requested`) and the grants they would produce (`granted`), which is
        what a person approves. With `enable: true` and the approval lists, it installs and
        switches it on.
        """
        source = str(payload.get("source") or payload.get("path") or "").strip()
        if not source:
            raise HTTPException(status_code=400, detail="source (a file path or an https:// URL) is required")
        try:
            info = manager.install(
                source,
                approve_preopens=payload.get("preopens"),
                approve_network=payload.get("network"),
                timeout_ms=int(payload.get("timeout_ms") or runtime.DEFAULT_TIMEOUT_MS),
                memory_mb=int(payload.get("memory_mb") or runtime.DEFAULT_MEMORY_MB),
                enabled=bool(payload.get("enable")),
            )
        except manager.WasmPluginError as exc:
            raise _fail(exc) from exc
        db.log_audit(
            actor="dashboard",
            action="wasm_plugin_install",
            detail=f"{info['name']} from {source}" + (" (enabled)" if payload.get("enable") else " (pending approval)"),
        )
        return info

    @app.post("/api/wasm-plugins/{name}/enable", dependencies=[Depends(_require_token_or_api_key)])
    async def api_wasm_plugins_enable(name: str, payload: dict = Body(default={})):
        try:
            if "preopens" in payload or "network" in payload:
                _set_grants(name, payload)
            return manager.enable(name)
        except manager.WasmPluginError as exc:
            raise _fail(exc) from exc

    @app.post("/api/wasm-plugins/{name}/disable", dependencies=[Depends(_require_token_or_api_key)])
    async def api_wasm_plugins_disable(name: str):
        try:
            return manager.disable(name)
        except manager.WasmPluginError as exc:
            raise _fail(exc) from exc

    @app.post("/api/wasm-plugins/{name}/uninstall", dependencies=[Depends(_require_token_or_api_key)])
    async def api_wasm_plugins_uninstall(name: str):
        try:
            removed = manager.uninstall(name)
        except manager.WasmPluginError as exc:
            raise _fail(exc) from exc
        if not removed:
            raise HTTPException(status_code=404, detail=f"no wasm plugin named {name!r}")
        db.log_audit(actor="dashboard", action="wasm_plugin_uninstall", detail=name)
        return {"removed": True, "name": name}

    @app.post("/api/wasm-plugins/{name}/invoke", dependencies=[Depends(_require_token_or_api_key)])
    async def api_wasm_plugins_invoke(name: str, payload: dict = Body(...)):
        """Call one tool by hand, for trying a plugin before an agent uses it.

        Audit-logged like any other call: this runs code, and a person pressing a button is
        still code that ran.
        """
        tool = str(payload.get("tool") or "").strip()
        if not tool:
            raise HTTPException(status_code=400, detail="tool is required")
        try:
            result = manager.invoke(
                name,
                tool,
                payload.get("args") or {},
                timeout_ms=int(payload.get("timeout_ms") or 0) or None,
            )
        except manager.WasmPluginError as exc:
            raise _fail(exc) from exc
        except runtime.PluginCallError as exc:
            # A deadline or a memory cap: 429 is the closest honest answer, and the detail says
            # which limit it was.
            raise HTTPException(status_code=429, detail=str(exc)) from exc
        return {"name": name, "tool": tool, "result": result}


def _set_grants(name: str, payload: dict) -> None:
    """Narrow (never widen) a plugin's grants from the dashboard.

    `manager.narrow_grants` intersects the given lists with what the plugin asked for, so a
    body naming a directory or host the plugin never asked for is refused rather than
    granted: the dashboard must not be a way around the install-time question.
    """
    try:
        requested = manager.narrow_grants(
            name,
            preopens=payload.get("preopens"),
            network=payload.get("network"),
        )
    except manager.WasmPluginError as exc:
        raise _fail(exc) from exc
    logger.info("wasm plugin %s: grants narrowed to %s", name, requested)
