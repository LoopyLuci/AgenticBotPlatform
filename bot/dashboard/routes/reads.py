"""Dashboard routes: reads.

Moved verbatim out of bot/dashboard/server.py's build_app(); the route order inside is unchanged.
"""
from __future__ import annotations

import asyncio
import functools
import os
import time
import csv
import io
from typing import Callable, Optional

from fastapi import Body, Depends, FastAPI, HTTPException, Response

from bot import bot_instances, db, desktop
from bot.config import config


def register(app: FastAPI, *, json_download: Callable) -> None:
    from bot.dashboard.server import _require_token, _require_token_or_api_key, _require_token_or_api_key_or_peer, _ts_stamp
    _json_download = json_download


    @functools.lru_cache(maxsize=1)
    def _build_info() -> dict:
        """What is running: ABP's version, and the git commit it was built from (when run from a checkout, or a build
        whose state root is one). Read once; a rebuild restarts the server."""
        import subprocess
        from bot import __version__
        from bot.envfile import PROJECT_ROOT
        info = {"app_version": __version__, "app_commit": "", "app_commit_date": "", "started_at": time.time()}
        try:
            out = subprocess.run(["git", "-C", str(PROJECT_ROOT), "log", "-1", "--format=%h|%cs"], capture_output=True,
                                 text=True, timeout=5, creationflags=0x08000000 if os.name == "nt" else 0)
            if out.returncode == 0 and "|" in out.stdout:
                info["app_commit"], info["app_commit_date"] = out.stdout.strip().split("|", 1)
        except (OSError, subprocess.SubprocessError):
            pass
        return info

    @app.get("/api/overview", dependencies=[Depends(_require_token_or_api_key_or_peer)])
    async def api_overview():
        overview = db.get_overview()
        # desktop.status() does a synchronous full-process-list scan
        # (psutil.process_iter) — off the event loop, or it stalls every
        # other request and the Telegram bots' long-polling for however
        # long that scan takes (worse under AV interference).
        d = await asyncio.get_running_loop().run_in_executor(None, desktop.status)
        overview["desktop_running"] = d.get("running", False)
        overview["desktop_pid"] = d.get("pid")
        overview["db_size_mb"] = round(db.get_db_size_bytes() / (1024 * 1024), 2)
        overview["config_version"] = config.version
        overview.update(_build_info())
        overview["default_backend"] = config.current.get("default_backend")
        overview["default_hermes_backend"] = config.current.get("default_hermes_backend")
        return overview

    @app.get("/api/jobs", dependencies=[Depends(_require_token_or_api_key)])
    def api_jobs(status: Optional[str] = None, limit: int = 50):
        rows = db.list_jobs(limit=limit, status=status)
        return [dict(r) for r in rows]

    @app.get("/api/jobs/timeseries", dependencies=[Depends(_require_token_or_api_key)])
    def api_jobs_timeseries():
        return db.get_jobs_timeseries_24h()

    @app.get("/api/jobs/by-backend", dependencies=[Depends(_require_token_or_api_key)])
    def api_jobs_by_backend():
        return db.get_jobs_by_backend_today()

    @app.get("/api/telemetry", dependencies=[Depends(_require_token_or_api_key)])
    async def api_telemetry():
        d = await asyncio.get_running_loop().run_in_executor(None, desktop.status)
        mcp_servers = desktop.list_mcp_servers()
        conn = db.get_conn()
        recent_errors = conn.execute(
            "SELECT component, COUNT(*) c FROM connections_log "
            "WHERE event='request_error' AND ts >= datetime('now','-15 minutes') GROUP BY component"
        ).fetchall()
        return {
            "desktop": d,
            "mcp_servers": mcp_servers,
            "latency_by_backend": db.get_latency_by_backend(),
            "recent_errors": {r["component"]: r["c"] for r in recent_errors},
            "connection_events": [dict(r) for r in db.get_recent_connection_events(limit=25)],
        }

    @app.get("/api/database", dependencies=[Depends(_require_token_or_api_key)])
    def api_database():
        return {
            "size_bytes": db.get_db_size_bytes(),
            "table_counts": db.get_table_counts(),
            "path": str(db.DB_PATH),
        }

    @app.get("/api/export/tables", dependencies=[Depends(_require_token)])
    def api_export_tables():
        return {"tables": db.EXPORTABLE_TABLES}

    @app.get("/api/export/{table}", dependencies=[Depends(_require_token)])
    def api_export_table(table: str, format: str = "json"):
        try:
            rows = db.export_table(table)
        except ValueError as e:
            raise HTTPException(status_code=404, detail=str(e)) from e
        stamp = _ts_stamp()
        if format == "csv":
            buf = io.StringIO()
            if rows:
                writer = csv.DictWriter(buf, fieldnames=list(rows[0].keys()))
                writer.writeheader()
                writer.writerows(rows)
            return Response(
                content=buf.getvalue().encode("utf-8"), media_type="text/csv",
                headers={"Content-Disposition": f'attachment; filename="{table}-{stamp}.csv"'},
            )
        return _json_download(rows, f"{table}-{stamp}.json")

    @app.get("/api/config", dependencies=[Depends(_require_token_or_api_key)])
    def api_config():
        # Token/api-key gated (see the route decorator) now that the server
        # can be reached from the public internet via Tailscale Funnel —
        # the TURN shared secret still never appears in it verbatim as a
        # second layer, matching the same reasoning as never echoing it
        # back after it's set.
        current = config.current
        turn_cfg = current.get("turn")
        if isinstance(turn_cfg, dict) and turn_cfg.get("secret"):
            current["turn"] = {**turn_cfg, "secret": None, "secret_set": True}
        return {
            "version": config.version,
            "current": current,
            "history": [dict(r) for r in db.list_config_history(limit=20)],
        }

    @app.get("/api/models", dependencies=[Depends(_require_token_or_api_key)])
    async def api_models(instance_id: Optional[int] = None):
        from bot.models import BACKEND_FAMILY, live_api_models, live_custom_models, live_hermes_models

        live_api = await live_api_models()
        live_hermes = live_hermes_models()
        live_custom = await live_custom_models()
        result = {
            "family": BACKEND_FAMILY,
            "current": {
                name: (config.current.get("backends", {}).get(name) or {}).get("model")
                for name in ("api", "hermes_cli", "hermes_gateway", "opencode", "openclaw")
            },
            "live": {
                "api": live_api,
                "hermes": live_hermes,
                "custom": live_custom,
            },
        }
        # instance_id opts into real per-model pricing/free-tier data for
        # that specific instance's own live Hermes gateway — see
        # bot.models.hermes_models_with_pricing. This is the payload the
        # agentic-bot-platform MCP server's list_available_models tool proxies
        # verbatim so Claude can make an actual "optimal free model"
        # decision instead of guessing from the id-suffix heuristic the
        # plain "live" section above still uses.
        if instance_id is not None:
            from bot.models import hermes_models_with_pricing

            instance = bot_instances.get_instance(instance_id)
            if instance and instance.get("backend") == "hermes_gateway":
                priced, source = await hermes_models_with_pricing(instance_id)
                result["pricing"] = priced
                result["pricing_source"] = source
        return result

    @app.get("/api/providers", dependencies=[Depends(_require_token_or_api_key)])
    async def api_providers_list():
        from bot import providers

        return {
            "providers": [
                {"name": name, "base_url": entry.get("base_url"), "protocol": entry.get("protocol", "openai"),
                 "api_key_env": entry.get("api_key_env"), "has_inline_key": bool(entry.get("api_key")),
                 "catalog_id": entry.get("catalog_id"), "module": entry.get("module")}
                for name, entry in sorted(providers.list_providers().items())
            ]
        }

    @app.post("/api/providers", dependencies=[Depends(_require_token_or_api_key)])
    async def api_providers_set(payload: dict = Body(...)):
        from bot import providers

        try:
            providers.set_provider(
                payload.get("name", ""),
                payload.get("base_url", ""),
                protocol=payload.get("protocol", "openai"),
                api_key_env=payload.get("api_key_env") or None,
                api_key=payload.get("api_key") or None,
                catalog_id=payload.get("catalog_id") or None,
                actor="dashboard",
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"ok": True}

    @app.delete("/api/providers/{name}", dependencies=[Depends(_require_token_or_api_key)])
    async def api_providers_delete(name: str):
        from bot import providers

        if not providers.delete_provider(name, actor="dashboard"):
            raise HTTPException(status_code=404, detail=f"no provider named {name!r}")
        return {"ok": True, "restorable": True}

    # Removing a provider keeps it in the provider store (bot/provider_store.py); these list what has been
    # removed, bring one back, or forget one for good. Keys are never returned, only whether one is stored.
    @app.get("/api/providers/store", dependencies=[Depends(_require_token_or_api_key)])
    async def api_providers_store(status: Optional[str] = None):
        from bot import providers

        if status not in (None, "active", "deleted"):
            raise HTTPException(status_code=400, detail="status must be 'active' or 'deleted'")
        return {"providers": providers.store_listing(status)}

    @app.post("/api/providers/store/{name}/restore", dependencies=[Depends(_require_token_or_api_key)])
    def api_providers_restore(name: str, payload: dict = Body(default={})):
        from bot import providers

        try:
            providers.restore_provider(name, api_key=(payload or {}).get("api_key") or None, actor="dashboard")
        except ValueError as exc:
            conflict = "already configured" in str(exc)
            raise HTTPException(status_code=409 if conflict else 404, detail=str(exc)) from exc
        db.log_audit(actor="dashboard", action="provider_restore", detail=name)
        return {"ok": True}

    @app.delete("/api/providers/store/{name}", dependencies=[Depends(_require_token_or_api_key)])
    def api_providers_purge(name: str):
        from bot import provider_store

        if not provider_store.purge(name):
            raise HTTPException(status_code=404, detail=f"no removed provider named {name!r}")
        db.log_audit(actor="dashboard", action="provider_purge", detail=name)
        return {"ok": True}

    @app.get("/api/providers/catalog", dependencies=[Depends(_require_token_or_api_key)])
    async def api_providers_catalog():
        from bot import model_pricing

        return {"providers": await model_pricing.list_known_providers()}

    @app.get("/api/providers/{name}/models", dependencies=[Depends(_require_token_or_api_key)])
    async def api_provider_models(name: str, refresh: bool = False):
        from bot import models as models_module
        from bot import providers

        if providers.get_provider(name) is None:
            raise HTTPException(status_code=404, detail=f"no provider named {name!r}")
        return {"models": await models_module.browse_provider_models(name, refresh=refresh)}

    @app.post("/api/providers/{name}/models/toggle", dependencies=[Depends(_require_token_or_api_key)])
    def api_provider_model_toggle(name: str, payload: dict = Body(...)):
        from bot import providers

        if providers.get_provider(name) is None:
            raise HTTPException(status_code=404, detail=f"no provider named {name!r}")
        model_id = payload.get("model_id")
        if not model_id:
            raise HTTPException(status_code=400, detail="model_id is required")
        enabled = bool(payload.get("enabled", True))
        db.set_model_toggle(name, model_id, enabled)
        db.log_audit(
            actor="dashboard", action="model_toggle",
            detail=f"{name}/{model_id}: {'enabled' if enabled else 'disabled'}",
        )
        return {"ok": True}

    @app.post("/api/providers/{name}/models/toggle-paid", dependencies=[Depends(_require_token_or_api_key)])
    async def api_provider_models_toggle_paid(name: str, payload: dict = Body(...)):
        """Bulk on/off for every non-free model of one provider — backs
        the Models page's "Turn Off All Paid"/"Turn On All Paid Models"
        buttons. Free models are never touched by this route; a model
        with unknown pricing counts as "paid" here, matching the same
        default-off convention bot.models._resolve_effective_enabled()
        already applies to it."""
        from bot import models as models_module
        from bot import providers

        if providers.get_provider(name) is None:
            raise HTTPException(status_code=404, detail=f"no provider named {name!r}")
        enabled = bool(payload.get("enabled", False))
        entries = await models_module.browse_provider_models(name)
        updates = {e["id"]: enabled for e in entries if e.get("free") is not True}
        db.bulk_set_model_toggles(name, updates)
        db.log_audit(
            actor="dashboard", action="model_toggle_paid_bulk",
            detail=f"{name}: {len(updates)} paid model(s) {'enabled' if enabled else 'disabled'}",
        )
        return {"ok": True, "count": len(updates)}
