"""Dashboard REST API — read endpoints back onto real SQLite data, mutating
endpoints require the X-Dashboard-Token header (set DASHBOARD_TOKEN in .env).

Bind stays on 127.0.0.1 by default (see bot/main.py) — that, plus the
token, is the security boundary. This has no session/cookie auth of its
own; don't expose it past localhost without putting a real reverse proxy
and auth in front of it.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Optional

from fastapi import Body, Depends, FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from bot import auth, db, desktop, envfile, outbox, setup_wizard
from bot.config import config
from bot.router import VALID_BACKENDS

STATIC_DIR = Path(__file__).resolve().parent / "static"
LOG_FILE = envfile.PROJECT_ROOT / "logs" / "bot.log"


def _require_token(x_dashboard_token: Optional[str] = Header(default=None)) -> None:
    expected = os.environ.get("DASHBOARD_TOKEN")
    if not expected:
        # No token configured: refuse mutating calls outright rather than
        # silently running with no auth at all.
        raise HTTPException(status_code=503, detail="DASHBOARD_TOKEN is not set in .env")
    if x_dashboard_token != expected:
        raise HTTPException(status_code=401, detail="invalid dashboard token")


def _require_token_or_bootstrap(x_dashboard_token: Optional[str] = Header(default=None)) -> None:
    """Same check as _require_token, except: if no DASHBOARD_TOKEN is
    configured yet, allow the request through instead of 503ing.

    Used only on the .env editor endpoints, for one reason — they're the
    only way to *set* the first token, and the strict check above would
    make that impossible (every request 503s until a token exists, but a
    token can only come to exist through a request). Once a real token is
    saved, this behaves identically to _require_token: the bootstrap
    window closes itself the moment DASHBOARD_TOKEN stops being empty.
    """
    expected = os.environ.get("DASHBOARD_TOKEN")
    if not expected:
        return
    if x_dashboard_token != expected:
        raise HTTPException(status_code=401, detail="invalid dashboard token")


def build_app() -> FastAPI:
    app = FastAPI(title="Bot Control Dashboard API")

    # The Tauri desktop shell loads its UI from a tauri://localhost origin
    # and fetch()es this API cross-origin. Both sides already sit behind
    # the localhost bind + DASHBOARD_TOKEN boundary, so an open CORS policy
    # here doesn't add real exposure — it just lets the two same-machine
    # processes talk to each other.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/")
    async def index():
        return FileResponse(STATIC_DIR / "dashboard.html")

    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    # ------------------------------------------------------------- reads --

    @app.get("/api/overview")
    async def api_overview():
        overview = db.get_overview()
        d = desktop.status()
        overview["desktop_running"] = d.get("running", False)
        overview["desktop_pid"] = d.get("pid")
        overview["db_size_mb"] = round(db.get_db_size_bytes() / (1024 * 1024), 2)
        overview["config_version"] = config.version
        overview["default_backend"] = config.current.get("default_backend")
        return overview

    @app.get("/api/jobs")
    async def api_jobs(status: Optional[str] = None, limit: int = 50):
        rows = db.list_jobs(limit=limit, status=status)
        return [dict(r) for r in rows]

    @app.get("/api/jobs/timeseries")
    async def api_jobs_timeseries():
        return db.get_jobs_timeseries_24h()

    @app.get("/api/jobs/by-backend")
    async def api_jobs_by_backend():
        return db.get_jobs_by_backend_today()

    @app.get("/api/telemetry")
    async def api_telemetry():
        d = desktop.status()
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

    @app.get("/api/database")
    async def api_database():
        return {
            "size_bytes": db.get_db_size_bytes(),
            "table_counts": db.get_table_counts(),
            "path": str(db.DB_PATH),
        }

    @app.get("/api/config")
    async def api_config():
        return {
            "version": config.version,
            "current": config.current,
            "history": [dict(r) for r in db.list_config_history(limit=20)],
        }

    @app.get("/api/mcp")
    async def api_mcp():
        return desktop.list_mcp_servers()

    @app.get("/api/mcp/{name}/logs")
    async def api_mcp_logs(name: str, lines: int = 50):
        return {"lines": desktop.tail_mcp_log(name, lines=lines)}

    @app.get("/api/logs")
    async def api_logs(lines: int = 100, level: Optional[str] = None):
        if not LOG_FILE.exists():
            return {"lines": []}
        with open(LOG_FILE, "r", encoding="utf-8", errors="replace") as f:
            all_lines = f.readlines()[-2000:]
        if level and level != "all":
            all_lines = [ln for ln in all_lines if f" {level.upper()} " in ln or f" {level.upper()}   " in ln]
        return {"lines": [ln.rstrip("\n") for ln in all_lines[-lines:]]}

    @app.get("/api/env")
    async def api_env():
        return envfile.status()

    # Contents/backups expose secret values, unlike every other GET in this
    # API — token-gated even though they're reads. _require_token_or_bootstrap
    # rather than _require_token: this is also the only path that can set
    # the first DASHBOARD_TOKEN, so it can't itself demand one already exist.
    @app.get("/api/env/content", dependencies=[Depends(_require_token_or_bootstrap)])
    async def api_env_content():
        return {"content": envfile.read_content(), "path": str(envfile.resolve())}

    @app.post("/api/env/content", dependencies=[Depends(_require_token_or_bootstrap)])
    async def api_env_content_save(payload: dict = Body(...)):
        content = payload.get("content")
        if content is None:
            raise HTTPException(status_code=400, detail="payload must be {content: str}")
        backup = envfile.write_content(content, actor="dashboard")
        return {"ok": True, "backup": backup.name if backup else None}

    @app.get("/api/env/backups", dependencies=[Depends(_require_token_or_bootstrap)])
    async def api_env_backups():
        return envfile.list_backups()

    @app.post("/api/env/backups/{name}/restore", dependencies=[Depends(_require_token_or_bootstrap)])
    async def api_env_restore(name: str):
        try:
            envfile.restore_backup(name, actor="dashboard")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        return {"ok": True}

    # ------------------------------------------------------- setup wizard --
    # Same bootstrap rule as the env editor above: this is the thing that
    # sets up the first working .env, so it can't itself require one.

    @app.get("/api/setup/status", dependencies=[Depends(_require_token_or_bootstrap)])
    async def api_setup_status():
        return setup_wizard.check_status()

    @app.post("/api/setup/generate-token", dependencies=[Depends(_require_token_or_bootstrap)])
    async def api_setup_generate_token():
        return {"token": setup_wizard.generate_dashboard_token()}

    @app.get("/api/setup/detect-desktop", dependencies=[Depends(_require_token_or_bootstrap)])
    async def api_setup_detect_desktop():
        path = desktop.find_exe_path()
        return {"path": path, "exists": bool(path and Path(path).exists())}

    @app.post("/api/setup/apply", dependencies=[Depends(_require_token_or_bootstrap)])
    async def api_setup_apply(payload: dict = Body(...)):
        backup, status = setup_wizard.apply_setup(payload, actor="setup-wizard")
        return {"ok": True, "backup": backup.name if backup else None, "status": status}

    @app.post("/api/setup/install-cli", dependencies=[Depends(_require_token)])
    async def api_setup_install_cli():
        # npm install can take a while — run off the event loop so it
        # doesn't stall every other dashboard request (jobs polling, etc.)
        # for the duration.
        result = await asyncio.to_thread(desktop.install_cli, actor="dashboard")
        return result

    # --------------------------------------------------------- platforms --
    # Reachable any time from Settings, not gated to first-run like the
    # core wizard above — same bootstrap rule though, since a fresh install
    # may want to set up a platform before DASHBOARD_TOKEN even exists.

    @app.get("/api/platforms/status", dependencies=[Depends(_require_token_or_bootstrap)])
    async def api_platforms_status():
        return setup_wizard.platform_status()

    @app.post("/api/platforms/apply", dependencies=[Depends(_require_token_or_bootstrap)])
    async def api_platforms_apply(payload: dict = Body(...)):
        backup, status = setup_wizard.apply_platform_fields(payload, actor="platforms-settings")
        return {"ok": True, "backup": backup.name if backup else None, "status": status}

    @app.get("/api/security/allowed-users")
    async def api_allowed_users():
        return [dict(r) for r in db.list_allowed_users()]

    # --------------------------------------------------------- mutations --

    @app.post("/api/desktop/start", dependencies=[Depends(_require_token)])
    async def api_desktop_start():
        try:
            desktop.start()
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        return {"ok": True}

    @app.post("/api/desktop/stop", dependencies=[Depends(_require_token)])
    async def api_desktop_stop():
        desktop.stop()
        return {"ok": True}

    @app.post("/api/desktop/restart", dependencies=[Depends(_require_token)])
    async def api_desktop_restart():
        desktop.restart()
        return {"ok": True}

    @app.post("/api/config/reload", dependencies=[Depends(_require_token)])
    async def api_config_reload():
        changed, summary = config.reload(actor="dashboard")
        return {"changed": changed, "summary": summary, "version": config.version}

    @app.post("/api/config/set", dependencies=[Depends(_require_token)])
    async def api_config_set(payload: dict = Body(...)):
        path = payload.get("path")
        value = payload.get("value")
        if not path or not isinstance(path, list):
            raise HTTPException(status_code=400, detail="payload must be {path: [...], value: ...}")
        config.set_value(path, value, actor="dashboard")
        return {"ok": True, "version": config.version}

    @app.post("/api/backend/{action_or_default}/{backend}", dependencies=[Depends(_require_token)])
    async def api_set_backend(action_or_default: str, backend: str):
        if backend not in VALID_BACKENDS:
            raise HTTPException(status_code=400, detail=f"unknown backend {backend!r}")
        if action_or_default == "default":
            config.set_value(["default_backend"], backend, actor="dashboard")
        else:
            config.set_value(["action_overrides", action_or_default, "backend"], backend, actor="dashboard")
        return {"ok": True, "version": config.version}

    @app.post("/api/mcp/{name}/enable", dependencies=[Depends(_require_token)])
    async def api_mcp_enable(name: str):
        try:
            desktop.enable_mcp(name)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        return {"ok": True}

    @app.post("/api/mcp/{name}/disable", dependencies=[Depends(_require_token)])
    async def api_mcp_disable(name: str):
        try:
            desktop.disable_mcp(name)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        return {"ok": True}

    @app.post("/api/mcp/self-register", dependencies=[Depends(_require_token)])
    async def api_mcp_self_register():
        return {"ok": True, **desktop.register_self_mcp(actor="dashboard")}

    @app.post("/api/security/allowed-users/{telegram_id}", dependencies=[Depends(_require_token)])
    async def api_add_allowed_user(telegram_id: int, name: str = ""):
        db.add_allowed_user(telegram_id, name)
        db.log_audit(actor="dashboard", action="add_allowed_user", detail=str(telegram_id))
        return {"ok": True}

    @app.delete("/api/security/allowed-users/{telegram_id}", dependencies=[Depends(_require_token)])
    async def api_remove_allowed_user(telegram_id: int):
        db.remove_allowed_user(telegram_id)
        db.log_audit(actor="dashboard", action="remove_allowed_user", detail=str(telegram_id))
        return {"ok": True}

    @app.post("/api/database/vacuum", dependencies=[Depends(_require_token)])
    async def api_vacuum():
        db.vacuum()
        return {"ok": True}

    # ------------------------------------------------------------- chat ----
    # Real conversation content, not just metadata — token-gated for reads
    # too, same reasoning as the .env editor above.

    @app.get("/api/chat/recipients", dependencies=[Depends(_require_token)])
    async def api_chat_recipients():
        from bot.platforms import discord_platform, slack_platform

        return {
            "telegram": sorted(auth.list_allowed_ids()),
            "discord": sorted(discord_platform.allowed_ids()),
            "slack": sorted(slack_platform.allowed_ids()),
            "connected": outbox.available_platforms(),
        }

    @app.get("/api/chat/messages", dependencies=[Depends(_require_token)])
    async def api_chat_messages(
        limit: int = 100,
        platform: Optional[str] = None,
        chat_id: Optional[str] = None,
        after_id: Optional[int] = None,
    ):
        rows = db.list_messages(limit=limit, platform=platform, chat_id=chat_id, after_id=after_id)
        return [dict(r) for r in rows]

    @app.post("/api/chat/send", dependencies=[Depends(_require_token)])
    async def api_chat_send(payload: dict = Body(...)):
        platform = (payload.get("platform") or "telegram").strip()
        chat_id = payload.get("chat_id")
        text = (payload.get("text") or "").strip()
        if not chat_id or not text:
            raise HTTPException(status_code=400, detail="payload must be {platform: str, chat_id: str|int, text: str}")
        try:
            await outbox.send_message(platform, chat_id, text)
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc))
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"{platform} send failed: {exc}")
        db.log_message(platform=platform, chat_id=chat_id, direction="out", source="dashboard", text=text)
        return {"ok": True}

    @app.exception_handler(Exception)
    async def unhandled(_request, exc: Exception):
        return JSONResponse(status_code=500, content={"detail": str(exc)})

    return app
