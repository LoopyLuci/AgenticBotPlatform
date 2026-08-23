"""Dashboard REST API — read endpoints back onto real SQLite data, mutating
endpoints require the X-Dashboard-Token header (set DASHBOARD_TOKEN in .env).

Bind stays on 127.0.0.1 by default (see bot/main.py) — that, plus the
token, is the security boundary. This has no session/cookie auth of its
own; don't expose it past localhost without putting a real reverse proxy
and auth in front of it.
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import mimetypes
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import qrcode
from qrcode.image.pure import PyPNGImage
from fastapi import Body, Depends, FastAPI, File, Form, Header, HTTPException, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from bot import agent_control, attachments, bot_instances, db, desktop, envfile, outbox, platform_supervisor, setup_wizard, thumbnails
from bot.backends.base import BackendError
from bot.commands import CmdContext, dispatch_command
from bot.config import config
from bot.router import VALID_BACKENDS, router
from bot.support_bot.engine import support_bot
from bot.swarm import engine as swarm_engine
from bot.swarm import strategies as swarm_strategies

STATIC_DIR = Path(__file__).resolve().parent / "static"
LOG_FILE = envfile.PROJECT_ROOT / "logs" / "bot.log"

# 25MB — Telegram bot-API's document limit, the tightest of the 3 platforms
# — the ceiling for actually relaying a file out through a bot. Above this,
# a file is stored server-only (already the established pull-based
# architecture) and clients show it as "too large to relay" instead of
# silently failing to send.
PLATFORM_RELAY_LIMIT_BYTES = 25 * 1024 * 1024
# Server-side ceiling on any one attachment, chunked uploads included —
# protects disk, not memory (every write path here is already chunked).
# Configurable since "how much local disk am I willing to give a single
# file" is a genuinely personal, per-deployment choice.
MAX_ATTACHMENT_BYTES = int(os.environ.get("MAX_ATTACHMENT_BYTES", 5 * 1024 * 1024 * 1024))
# Per-(instance, chat_id) scratch state for "Chat with Bot" mode messages —
# mirrors each platform adapter's own in-memory self._sessions dict (e.g.
# discord_platform.py's DiscordPlatformInstance), so /project and other
# session-scoped slash commands behave identically whether the message came
# from a real platform or through the Bot Server App's own real channel.
# Intentionally not persisted — same lifetime as the platform adapters' own
# equivalents.
_app_chat_sessions: dict[tuple[int, str], dict] = {}
# A paired device counts as "online" if it's made an authenticated request
# within this window — see db.verify_api_key()'s device_presence upsert.
DEVICE_ONLINE_WINDOW_S = 30


def _annotate_online(devices: list[dict]) -> list[dict]:
    now = datetime.now(timezone.utc)
    out = []
    for d in devices:
        online = False
        last_seen = d.get("last_seen")
        if last_seen:
            try:
                online = (now - datetime.fromisoformat(last_seen)).total_seconds() < DEVICE_ONLINE_WINDOW_S
            except ValueError:
                online = False
        out.append({**d, "online": online})
    return out


class _ConnectionManager:
    """Tracks live /api/ws sockets for broadcasting device-presence deltas.
    A plain in-process set is enough — this is a single-process server, no
    need for pub/sub across workers."""

    def __init__(self) -> None:
        self._sockets: set[WebSocket] = set()
        self._lock = asyncio.Lock()

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        async with self._lock:
            self._sockets.add(ws)

    async def disconnect(self, ws: WebSocket) -> None:
        async with self._lock:
            self._sockets.discard(ws)

    async def broadcast(self, payload: dict) -> None:
        async with self._lock:
            sockets = list(self._sockets)
        for ws in sockets:
            try:
                await ws.send_json(payload)
            except Exception:
                await self.disconnect(ws)


_manager = _ConnectionManager()


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


def _identify_caller(
    x_dashboard_token: Optional[str] = Header(default=None),
    x_device_platform: Optional[str] = Header(default=None),
    x_device_app_version: Optional[str] = Header(default=None),
    x_device_model: Optional[str] = Header(default=None),
    x_device_os_version: Optional[str] = Header(default=None),
) -> str:
    """Accepts either the legacy single DASHBOARD_TOKEN (desktop/dashboard)
    or a valid unrevoked mobile api_keys hash, returning which kind
    authenticated. Bot instance management (create/edit/delete, including
    platform bot tokens) and settings changes (/api/config/set) are
    reachable by mobile keys too — a deliberate choice (full parity with
    the desktop dashboard) rather than an oversight: it means a lost or
    unlocked phone can rewrite bot credentials or security settings, same
    as a lost desktop session could. Mobile-key *management itself*
    (minting/revoking other devices' keys, reading the full key list)
    stays on the strict _require_token, so a phone can't provision new
    devices on its own authority.

    The optional X-Device-Platform/X-Device-App-Version/X-Device-Model/
    X-Device-OS-Version headers (sent by the Android app on every request)
    feed device_presence so the Devices view can show what's actually
    connected — real hardware model and OS release, not just the user-typed
    pairing label — instead of just proving that something is."""
    expected = os.environ.get("DASHBOARD_TOKEN")
    if expected and x_dashboard_token == expected:
        return "dashboard"
    if db.verify_api_key(
        x_dashboard_token or "",
        platform=x_device_platform,
        app_version=x_device_app_version,
        device_model=x_device_model,
        os_version=x_device_os_version,
    ) is not None:
        return "mobile"
    if not expected:
        raise HTTPException(status_code=503, detail="DASHBOARD_TOKEN is not set in .env")
    raise HTTPException(status_code=401, detail="invalid dashboard token or api key")


def _require_token_or_api_key(caller: str = Depends(_identify_caller)) -> None:
    return None


def _caller_thread_identity(
    x_dashboard_token: Optional[str] = Header(default=None),
    x_device_platform: Optional[str] = Header(default=None),
    x_device_app_version: Optional[str] = Header(default=None),
    x_device_model: Optional[str] = Header(default=None),
    x_device_os_version: Optional[str] = Header(default=None),
) -> tuple[str, str, str]:
    """Like _identify_caller, but resolves to a real, stable per-caller
    thread identity — (source, chat_id, username) — instead of just
    "dashboard"/"mobile". Used by "Chat with Bot" (POST
    /api/chat/send-to-bot): each distinct caller (the desktop dashboard, or
    each individually-paired phone/tablet) gets its own persistent chat_id,
    the same way each real Telegram user gets their own chat — so /project
    and other session-scoped commands stay correctly separated per device
    instead of every device sharing one conversation thread. Derived
    entirely from auth already on the request; the client never gets to
    declare its own identity."""
    expected = os.environ.get("DASHBOARD_TOKEN")
    if expected and x_dashboard_token == expected:
        return "dashboard", "dashboard", "Dashboard"
    key_id = db.verify_api_key(
        x_dashboard_token or "",
        platform=x_device_platform,
        app_version=x_device_app_version,
        device_model=x_device_model,
        os_version=x_device_os_version,
    )
    if key_id is not None:
        label = next((r["label"] for r in db.list_api_keys() if r["id"] == key_id), f"device {key_id}")
        return "mobile", f"device:{key_id}", label
    if not expected:
        raise HTTPException(status_code=503, detail="DASHBOARD_TOKEN is not set in .env")
    raise HTTPException(status_code=401, detail="invalid dashboard token or api key")


def _require_mobile_key_id(x_dashboard_token: Optional[str] = Header(default=None)) -> int:
    """Used only by /api/push/register — a push registration is meaningless
    without knowing which device's api_keys row to attach the FCM token to,
    so (unlike every other mobile-reachable route) this one requires an
    actual mobile key specifically, not the desktop DASHBOARD_TOKEN too."""
    key_id = db.verify_api_key(x_dashboard_token or "")
    if key_id is None:
        raise HTTPException(status_code=401, detail="a valid mobile api key is required")
    return key_id


def build_app() -> FastAPI:
    app = FastAPI(title="Bot Control Dashboard API")

    # The Tauri desktop shell loads its UI from an origin that fetch()es
    # this API cross-origin — that's the one real cross-origin browser
    # client this app has, so it's the only origin allowed. Plain
    # dashboard.html usage (browser or Android WebView-less native client)
    # never needs CORS at all: the HTML and the API are served from the
    # same origin, and native Retrofit/OkHttp requests (the Android app)
    # send no Origin header for CORS to ever block. Wide open ("*") was
    # previously "safe" only because the bind stayed loopback-only — once
    # this server is reachable beyond localhost (e.g. over a Tailscale
    # tailnet, see docs/mobile-access.md), a browser-based client anywhere
    # on that network could otherwise read this API cross-origin using the
    # visitor's own browser session, so the origin list is kept narrow.
    #
    # `tauri://localhost` is the scheme on macOS/Linux (WKWebView/
    # webkit2gtk support fully custom URI schemes) — Windows' WebView2
    # can't host a document at a non-http(s) origin, so Tauri v2 serves the
    # app there as `http://tauri.localhost` instead. Confirmed live on this
    # Windows build (not assumed): every request from the actual running
    # desktop shell carries `Origin: http://tauri.localhost`, which neither
    # the old literal `tauri://localhost` entry nor the 127.0.0.1/localhost
    # regex matched — silently breaking every fetch() the desktop app's own
    # UI made (boot readiness check included) since CORS was narrowed from
    # wide-open, while curl/native clients (no Origin header) stayed fine
    # and masked it. Both schemes are listed so this keeps working on any
    # future non-Windows build too.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["tauri://localhost", "http://tauri.localhost"],
        allow_origin_regex=r"http://(127\.0\.0\.1|localhost|tauri\.localhost)(:\d+)?",
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.on_event("startup")
    async def _on_startup():
        asyncio.create_task(_presence_broadcaster())

    async def _presence_broadcaster():
        """Periodically diffs the device-presence snapshot and pushes it to
        every open /api/ws socket — a device going on/offline between polls
        is what makes the Devices view feel live, not just "eventually
        consistent within 15s"."""
        last_snapshot = ""
        while True:
            await asyncio.sleep(5)
            try:
                devices = await asyncio.get_running_loop().run_in_executor(None, db.list_devices)
                annotated = _annotate_online([dict(d) for d in devices])
                snapshot = json.dumps(annotated, sort_keys=True)
                if snapshot != last_snapshot:
                    last_snapshot = snapshot
                    await _manager.broadcast({"type": "device_list", "devices": annotated})
            except Exception:
                pass

    @app.get("/")
    async def index():
        return FileResponse(STATIC_DIR / "dashboard.html")

    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    # ------------------------------------------------------------- reads --

    @app.get("/api/overview")
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
        overview["default_backend"] = config.current.get("default_backend")
        return overview

    @app.get("/api/jobs", dependencies=[Depends(_require_token_or_api_key)])
    async def api_jobs(status: Optional[str] = None, limit: int = 50):
        rows = db.list_jobs(limit=limit, status=status)
        return [dict(r) for r in rows]

    @app.get("/api/jobs/timeseries", dependencies=[Depends(_require_token_or_api_key)])
    async def api_jobs_timeseries():
        return db.get_jobs_timeseries_24h()

    @app.get("/api/jobs/by-backend", dependencies=[Depends(_require_token_or_api_key)])
    async def api_jobs_by_backend():
        return db.get_jobs_by_backend_today()

    @app.get("/api/telemetry")
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

    @app.get("/api/models")
    async def api_models():
        from bot.models import KNOWN_MODELS

        return {"known": KNOWN_MODELS, "current": {
            name: (config.current.get("backends", {}).get(name) or {}).get("model")
            for name in ("api", "hermes_cli", "hermes_gateway")
        }}

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

    # -------------------------------------------------------------- bots --
    # DB-backed bot instances — replaces the fixed one-per-platform model.
    # Token-gated, no bootstrap exception (bot instance management is never
    # needed before DASHBOARD_TOKEN itself exists) — but unlike most write
    # routes, create/update/delete/restart accept a mobile device key too,
    # not just the desktop token: full parity with the desktop dashboard's
    # Bots tab, including submitting/editing platform bot tokens from the
    # phone. See _identify_caller()'s docstring for the tradeoff.

    @app.get("/api/bots", dependencies=[Depends(_require_token_or_api_key)])
    async def api_bots_list():
        live = platform_supervisor.status()
        rows = bot_instances.list_instances()
        for row in rows:
            row["live_running"] = live.get(row["id"], {}).get("running", False)
        return rows

    @app.get("/api/bots/backups", dependencies=[Depends(_require_token)])
    async def api_bots_backups():
        return bot_instances.list_backups()

    @app.post("/api/bots/backups/{name}/restore", dependencies=[Depends(_require_token)])
    async def api_bots_restore(name: str):
        try:
            bot_instances.restore_backup(name, actor="dashboard")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        return {"ok": True}

    @app.get("/api/bots/{instance_id}", dependencies=[Depends(_require_token_or_api_key)])
    async def api_bots_get(instance_id: int):
        row = bot_instances.get_instance(instance_id)
        if row is None:
            raise HTTPException(status_code=404, detail=f"bot instance {instance_id} not found")
        return row

    @app.post("/api/bots", dependencies=[Depends(_require_token_or_api_key)])
    async def api_bots_create(payload: dict = Body(...)):
        try:
            instance_id = bot_instances.create_instance(
                name=payload.get("name", ""),
                platform=payload.get("platform", ""),
                backend=payload.get("backend", "cli"),
                credentials=payload.get("credentials") or {},
                allowed_user_ids=payload.get("allowed_user_ids") or [],
                action_overrides=payload.get("action_overrides") or {},
                enabled=bool(payload.get("enabled", True)),
                model=payload.get("model") or None,
                actor="dashboard",
            )
        except bot_instances.ValidationError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        row = bot_instances.get_instance(instance_id)
        if row and row["enabled"]:
            # The row is saved either way — a bad token/connection issue
            # surfaces as a start failure recorded on the row itself
            # (bot_instances.last_error), not a lost bot.
            try:
                await platform_supervisor.start_instance(row)
            except Exception:
                pass
        return {"ok": True, "id": instance_id}

    @app.put("/api/bots/{instance_id}", dependencies=[Depends(_require_token_or_api_key)])
    async def api_bots_update(instance_id: int, payload: dict = Body(...)):
        fields = {
            k: v
            for k, v in payload.items()
            if k in ("name", "platform", "backend", "enabled", "credentials", "allowed_user_ids", "action_overrides", "can_target", "model")
        }
        try:
            bot_instances.update_instance(instance_id, actor="dashboard", **fields)
        except bot_instances.ValidationError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return {"ok": True}

    @app.delete("/api/bots/{instance_id}", dependencies=[Depends(_require_token_or_api_key)])
    async def api_bots_delete(instance_id: int):
        await platform_supervisor.stop_instance(instance_id)
        try:
            bot_instances.delete_instance(instance_id, actor="dashboard")
        except bot_instances.ValidationError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        return {"ok": True}

    @app.post("/api/bots/{instance_id}/enable", dependencies=[Depends(_require_token_or_api_key)])
    async def api_bots_enable(instance_id: int):
        try:
            bot_instances.enable_instance(instance_id, actor="dashboard")
        except bot_instances.ValidationError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        row = bot_instances.get_instance(instance_id)
        if row:
            await platform_supervisor.start_instance(row)
        return {"ok": True}

    @app.post("/api/bots/{instance_id}/disable", dependencies=[Depends(_require_token_or_api_key)])
    async def api_bots_disable(instance_id: int):
        try:
            bot_instances.disable_instance(instance_id, actor="dashboard")
        except bot_instances.ValidationError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        await platform_supervisor.stop_instance(instance_id)
        return {"ok": True}

    @app.post("/api/bots/{instance_id}/start", dependencies=[Depends(_require_token_or_api_key)])
    async def api_bots_start(instance_id: int):
        row = bot_instances.get_instance(instance_id)
        if row is None:
            raise HTTPException(status_code=404, detail=f"bot instance {instance_id} not found")
        try:
            await platform_supervisor.start_instance(row)
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"failed to start: {exc}")
        return {"ok": True}

    @app.post("/api/bots/{instance_id}/stop", dependencies=[Depends(_require_token_or_api_key)])
    async def api_bots_stop(instance_id: int):
        await platform_supervisor.stop_instance(instance_id)
        return {"ok": True}

    @app.post("/api/bots/{instance_id}/restart", dependencies=[Depends(_require_token_or_api_key)])
    async def api_bots_restart(instance_id: int):
        await platform_supervisor.restart_instance(instance_id)
        return {"ok": True}

    @app.post("/api/bots/{instance_id}/session/new", dependencies=[Depends(_require_token_or_api_key)])
    async def api_bots_new_session(instance_id: int):
        # Opens a real new chat in Claude Desktop / Hermes for this instance
        # and links it — see router.create_session(). Only ui/hermes_gateway
        # backends support this; other backends 400.
        from bot.backends.base import BackendError
        from bot.router import router

        if bot_instances.get_instance(instance_id) is None:
            raise HTTPException(status_code=404, detail=f"bot instance {instance_id} not found")
        try:
            key = await router.create_session(instance_id)
        except BackendError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return {"ok": True, "desktop_session_key": key}

    # ------------------------------------------------------------ swarms --
    # A swarm is a named group of bot instances plus a strategy for how
    # they collaborate — see bot/swarm/strategies.py. Strictly token-gated,
    # same reasoning as /api/bots.

    def _referenced_instance_ids(strategy: str, cfg: dict) -> list[int]:
        if strategy == "custom":
            return [s.get("instance_id") for s in (cfg.get("steps") or []) if s.get("instance_id")]
        ids = list(cfg.get("members") or [])
        for key in ("synthesizer", "leader", "planner", "aggregator"):
            if cfg.get(key):
                ids.append(cfg[key])
        return ids

    def _validate_swarm(strategy: str, cfg: dict) -> None:
        if strategy not in swarm_strategies.STRATEGIES:
            raise HTTPException(status_code=400, detail=f"unknown strategy {strategy!r}")
        ids = _referenced_instance_ids(strategy, cfg)
        if not ids:
            raise HTTPException(status_code=400, detail="swarm config references no bot instances")
        for iid in ids:
            if bot_instances.get_instance(iid) is None:
                raise HTTPException(status_code=400, detail=f"bot instance {iid} referenced in config doesn't exist")

    @app.get("/api/swarms", dependencies=[Depends(_require_token)])
    async def api_swarms_list():
        return [dict(r) | {"config": json.loads(r["config"]), "enabled": bool(r["enabled"])} for r in db.list_swarms()]

    # /api/swarms/runs* declared before /api/swarms/{swarm_id} — FastAPI
    # matches routes in declaration order, and {swarm_id}:int would
    # otherwise swallow the literal path segment "runs" as an int and
    # 422 on it before this route is ever reached.
    @app.get("/api/swarms/runs", dependencies=[Depends(_require_token)])
    async def api_swarm_runs_list(swarm_id: Optional[int] = None, limit: int = 50):
        return [swarm_engine.swarm_run_to_dict(r) for r in db.list_swarm_runs(swarm_id=swarm_id, limit=limit)]

    @app.get("/api/swarms/runs/{swarm_run_id}", dependencies=[Depends(_require_token)])
    async def api_swarm_run_get(swarm_run_id: str):
        row = db.get_swarm_run(swarm_run_id)
        if row is None:
            raise HTTPException(status_code=404, detail=f"swarm run {swarm_run_id} not found")
        return swarm_engine.swarm_run_to_dict(row)

    @app.post("/api/swarms/runs/{swarm_run_id}/cancel", dependencies=[Depends(_require_token)])
    async def api_swarm_run_cancel(swarm_run_id: str):
        cancelled = swarm_engine.cancel_run(swarm_run_id)
        return {"ok": True, "cancelled": cancelled}

    @app.get("/api/swarms/{swarm_id}", dependencies=[Depends(_require_token)])
    async def api_swarms_get(swarm_id: int):
        row = db.get_swarm(swarm_id)
        if row is None:
            raise HTTPException(status_code=404, detail=f"swarm {swarm_id} not found")
        return dict(row) | {"config": json.loads(row["config"]), "enabled": bool(row["enabled"])}

    @app.post("/api/swarms", dependencies=[Depends(_require_token)])
    async def api_swarms_create(payload: dict = Body(...)):
        name = (payload.get("name") or "").strip()
        strategy = payload.get("strategy", "")
        cfg = payload.get("config") or {}
        if not name:
            raise HTTPException(status_code=400, detail="name is required")
        _validate_swarm(strategy, cfg)
        try:
            swarm_id = db.create_swarm(name, strategy, json.dumps(cfg), enabled=bool(payload.get("enabled", True)))
        except Exception as exc:
            if "UNIQUE" in str(exc):
                raise HTTPException(status_code=400, detail=f"a swarm named {name!r} already exists")
            raise
        db.log_audit(actor="dashboard", action="swarm_create", detail=f"created {name!r} ({strategy})")
        return {"ok": True, "id": swarm_id}

    @app.put("/api/swarms/{swarm_id}", dependencies=[Depends(_require_token)])
    async def api_swarms_update(swarm_id: int, payload: dict = Body(...)):
        if db.get_swarm(swarm_id) is None:
            raise HTTPException(status_code=404, detail=f"swarm {swarm_id} not found")
        fields: dict = {}
        if "name" in payload:
            fields["name"] = payload["name"]
        if "enabled" in payload:
            fields["enabled"] = bool(payload["enabled"])
        if "strategy" in payload or "config" in payload:
            current = db.get_swarm(swarm_id)
            strategy = payload.get("strategy", current["strategy"])
            cfg = payload.get("config", json.loads(current["config"]))
            _validate_swarm(strategy, cfg)
            fields["strategy"] = strategy
            fields["config"] = json.dumps(cfg)
        db.update_swarm(swarm_id, **fields)
        db.log_audit(actor="dashboard", action="swarm_update", detail=f"updated swarm {swarm_id}")
        return {"ok": True}

    @app.delete("/api/swarms/{swarm_id}", dependencies=[Depends(_require_token)])
    async def api_swarms_delete(swarm_id: int):
        if db.get_swarm(swarm_id) is None:
            raise HTTPException(status_code=404, detail=f"swarm {swarm_id} not found")
        db.delete_swarm(swarm_id)
        db.log_audit(actor="dashboard", action="swarm_delete", detail=f"deleted swarm {swarm_id}")
        return {"ok": True}

    @app.post("/api/swarms/{swarm_id}/enable", dependencies=[Depends(_require_token)])
    async def api_swarms_enable(swarm_id: int):
        if db.get_swarm(swarm_id) is None:
            raise HTTPException(status_code=404, detail=f"swarm {swarm_id} not found")
        db.update_swarm(swarm_id, enabled=True)
        return {"ok": True}

    @app.post("/api/swarms/{swarm_id}/disable", dependencies=[Depends(_require_token)])
    async def api_swarms_disable(swarm_id: int):
        if db.get_swarm(swarm_id) is None:
            raise HTTPException(status_code=404, detail=f"swarm {swarm_id} not found")
        db.update_swarm(swarm_id, enabled=False)
        return {"ok": True}

    @app.post("/api/swarms/{swarm_id}/run", dependencies=[Depends(_require_token)])
    async def api_swarms_run(swarm_id: int, payload: dict = Body(...)):
        prompt = (payload.get("prompt") or "").strip()
        if not prompt:
            raise HTTPException(status_code=400, detail="payload must be {prompt: str}")
        requested_by = "dashboard"
        source_instance = payload.get("source_instance")
        if source_instance is not None:
            source = agent_control.resolve_instance(source_instance)
            if source is None:
                raise HTTPException(status_code=404, detail=f"source instance {source_instance!r} not found")
            swarm_row = db.get_swarm(swarm_id)
            if swarm_row is None:
                raise HTTPException(status_code=404, detail=f"swarm {swarm_id} not found")
            member_ids = _referenced_instance_ids(swarm_row["strategy"], json.loads(swarm_row["config"]))
            denied = [iid for iid in member_ids if not agent_control.can_target(source["id"], iid)]
            if denied:
                raise HTTPException(
                    status_code=403,
                    detail=f"{source['name']} is not permitted to target instance(s) {denied} under the current allowlist",
                )
            requested_by = f"agent:{source['name']}"
        try:
            swarm_run_id = swarm_engine.start_swarm_run(swarm_id, prompt, requested_by=requested_by)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        return {"ok": True, "swarm_run_id": swarm_run_id}

    # ------------------------------------------------------ agent control --
    # Lets one bot instance's own AI session ask another instance a one-off
    # question via router.ask(), subject to agent_control's trust_all/
    # allowlist toggle. source_instance is self-declared by the caller (see
    # bot/agent_control.py's module docstring) — not cryptographically
    # verified, an accepted tradeoff for this single-operator app.

    @app.post("/api/agent/ask", dependencies=[Depends(_require_token)])
    async def api_agent_ask(payload: dict = Body(...)):
        prompt = (payload.get("prompt") or "").strip()
        if not prompt:
            raise HTTPException(status_code=400, detail="payload must be {source_instance, target_instance, prompt}")
        source = agent_control.resolve_instance(payload.get("source_instance"))
        if source is None:
            raise HTTPException(status_code=404, detail=f"source instance {payload.get('source_instance')!r} not found")
        target = agent_control.resolve_instance(payload.get("target_instance"))
        if target is None:
            raise HTTPException(status_code=404, detail=f"target instance {payload.get('target_instance')!r} not found")
        if not agent_control.can_target(source["id"], target["id"]):
            raise HTTPException(
                status_code=403,
                detail=f"{source['name']} is not permitted to target {target['name']} under the current allowlist",
            )
        db.log_audit(
            actor=f"agent:{source['name']}", action="agent_ask",
            detail=f"-> {target['name']}: {prompt[:120]}",
        )
        try:
            result = await router.ask(prompt, action_type="agent_relay", instance_id=target["id"])
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"ask failed: {exc}")
        return {"ok": True, "result": result.text}

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

    @app.post("/api/config/set", dependencies=[Depends(_require_token_or_api_key)])
    async def api_config_set(payload: dict = Body(...)):
        path = payload.get("path")
        value = payload.get("value")
        if not path or not isinstance(path, list):
            raise HTTPException(status_code=400, detail="payload must be {path: [...], value: ...}")
        config.set_value(path, value, actor="dashboard")
        return {"ok": True, "version": config.version}

    @app.post("/api/backend/{action_or_default}/{backend}", dependencies=[Depends(_require_token_or_api_key)])
    async def api_set_backend(action_or_default: str, backend: str):
        if backend not in VALID_BACKENDS:
            raise HTTPException(status_code=400, detail=f"unknown backend {backend!r}")
        if action_or_default == "default":
            config.set_value(["default_backend"], backend, actor="dashboard")
        else:
            config.set_value(["action_overrides", action_or_default, "backend"], backend, actor="dashboard")
        return {"ok": True, "version": config.version}

    # ---------------------------------------------------------- support bot
    # The local, dependency-free management assistant (bot/support_bot/) —
    # same auth tier as /api/bots and /api/config/set since it can trigger
    # the same actions those routes do, just via natural language.

    @app.post("/api/support-bot/ask", dependencies=[Depends(_require_token_or_api_key)])
    async def api_support_bot_ask(payload: dict = Body(...)):
        text = (payload.get("text") or "").strip()
        if not text:
            raise HTTPException(status_code=400, detail="payload must be {text: ...}")
        reply = await support_bot.handle(text, actor="support-bot")
        return {
            "text": reply.text,
            "intent": reply.intent,
            "needs_confirm": reply.needs_confirm,
            "confirm_token": reply.confirm_token,
            "applied": reply.applied,
        }

    @app.post("/api/support-bot/confirm", dependencies=[Depends(_require_token_or_api_key)])
    async def api_support_bot_confirm(payload: dict = Body(...)):
        token = (payload.get("token") or "").strip()
        if not token:
            raise HTTPException(status_code=400, detail="payload must be {token: ...}")
        reply = await support_bot.confirm(token, actor="support-bot")
        return {
            "text": reply.text,
            "intent": reply.intent,
            "needs_confirm": reply.needs_confirm,
            "confirm_token": reply.confirm_token,
            "applied": reply.applied,
        }

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

    @app.get("/api/chat/recipients", dependencies=[Depends(_require_token_or_api_key)])
    async def api_chat_recipients():
        connected = set(outbox.available_instances())
        return {
            "instances": [
                {
                    "id": inst["id"],
                    "name": inst["name"],
                    "platform": inst["platform"],
                    "allowed_ids": sorted(inst["allowed_user_ids"], key=str),
                    "connected": inst["id"] in connected,
                }
                for inst in bot_instances.list_instances()
            ]
        }

    @app.get("/api/chat/messages", dependencies=[Depends(_require_token_or_api_key)])
    async def api_chat_messages(
        limit: int = 100,
        platform: Optional[str] = None,
        chat_id: Optional[str] = None,
        after_id: Optional[int] = None,
        instance_id: Optional[int] = None,
    ):
        rows = db.list_messages(
            limit=limit, platform=platform, chat_id=chat_id, after_id=after_id, instance_id=instance_id
        )
        return [dict(r) for r in rows]

    @app.post("/api/chat/send", dependencies=[Depends(_require_token_or_api_key)])
    async def api_chat_send(payload: dict = Body(...)):
        instance_id = payload.get("instance_id")
        chat_id = payload.get("chat_id")
        text = (payload.get("text") or "").strip()
        if not instance_id or not chat_id or not text:
            raise HTTPException(status_code=400, detail="payload must be {instance_id: int, chat_id: str|int, text: str}")
        instance = bot_instances.get_instance(int(instance_id))
        if instance is None:
            raise HTTPException(status_code=404, detail=f"bot instance {instance_id} not found")
        try:
            await outbox.send_message(int(instance_id), chat_id, text)
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc))
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"send failed: {exc}")
        db.log_message(
            platform=instance["platform"], chat_id=chat_id, direction="out", source="dashboard",
            text=text, instance_id=int(instance_id),
        )
        return {"ok": True}

    # ------------------------------------------------- "Chat with Bot" mode -
    # The Chat tab's other mode: /api/chat/send (above) is "Send from
    # Server" — the dashboard/app pushes a message OUT, through outbox.py +
    # a live platform SDK, appearing to a real Telegram/Discord/Slack user as
    # if it came from the bot. This is the reverse direction: a real message
    # FROM the dashboard operator or a real paired device TO the bot, using
    # the exact same CmdContext/dispatch_command/router.ask() pipeline every
    # Telegram/Discord/Slack handler uses (see e.g. discord_platform.py's
    # on_message) — genuinely processed, genuinely replied to. Nothing here
    # is simulated: the sender's identity comes from real request auth (see
    # _caller_thread_identity), not a client-declared value, and is logged
    # as platform="app" — the Bot Server App's own real channel — rather
    # than disguised as whichever platform the target instance happens to
    # also use. Never touches outbox.py or any platform SDK.
    @app.post("/api/chat/send-to-bot", dependencies=[Depends(_require_token_or_api_key)])
    async def api_chat_send_to_bot(
        payload: dict = Body(...),
        identity: tuple[str, str, str] = Depends(_caller_thread_identity),
    ):
        instance_id = payload.get("instance_id")
        text = (payload.get("text") or "").strip()
        if not instance_id or not text:
            raise HTTPException(status_code=400, detail="payload must be {instance_id: int, text: str}")
        instance = bot_instances.get_instance(int(instance_id))
        if instance is None:
            raise HTTPException(status_code=404, detail=f"bot instance {instance_id} not found")
        source, chat_id, username = identity
        db.log_message(
            platform="app", chat_id=chat_id, user_id=chat_id, username=username,
            direction="in", source=source, text=text, instance_id=int(instance_id),
        )
        session = _app_chat_sessions.setdefault((int(instance_id), chat_id), {})
        cmd_ctx = CmdContext(
            instance_id=int(instance_id), instance_name=instance["name"], user_id=chat_id,
            chat_id=chat_id, actor=f"{source}:{chat_id}", session=session,
        )
        try:
            cmd_reply = await dispatch_command(text, cmd_ctx)
            if cmd_reply is not None:
                reply_text = cmd_reply
            else:
                result = await router.ask(
                    text, action_type=session.get("action_type", "quick_question"), user_id=chat_id,
                    context={"cwd": session["project_cwd"]} if session.get("project_cwd") else None,
                    instance_id=int(instance_id), chat_id=chat_id,
                )
                reply_text = result.text
        except BackendError as exc:
            reply_text = f"Backend failed: {exc}"
        db.log_message(
            platform="app", chat_id=chat_id, direction="out", source="bot",
            text=reply_text, instance_id=int(instance_id),
        )
        return {"ok": True, "reply": reply_text}

    @app.post("/api/chat/send-file", dependencies=[Depends(_require_token_or_api_key)])
    async def api_chat_send_file(
        instance_id: int = Form(...),
        chat_id: str = Form(...),
        text: str = Form(""),
        file: UploadFile = File(...),
    ):
        instance = bot_instances.get_instance(instance_id)
        if instance is None:
            raise HTTPException(status_code=404, detail=f"bot instance {instance_id} not found")
        # Kept as the simple one-shot path for small files — the chunked
        # /api/uploads/* flow below is what desktop/Android use for
        # anything sizeable, but there's no reason to force a 3-request
        # dance for a 200KB image. Still capped at the platform relay limit
        # since this path always relays immediately.
        try:
            rel_path, orig_name = await attachments.safe_store_stream(file.filename, file, PLATFORM_RELAY_LIMIT_BYTES)
        except ValueError as exc:
            raise HTTPException(status_code=413, detail=str(exc))
        caption = (text or "").strip()
        try:
            await outbox.send_file(
                instance_id, chat_id, str(attachments.ATTACHMENTS_DIR / rel_path), orig_name, caption or None
            )
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc))
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"send failed: {exc}")
        mime = file.content_type or mimetypes.guess_type(orig_name)[0]
        size = (attachments.ATTACHMENTS_DIR / rel_path).stat().st_size
        thumb_path = await asyncio.get_running_loop().run_in_executor(
            None, thumbnails.generate_thumbnail, attachments.ATTACHMENTS_DIR / rel_path, mime, attachments.THUMBS_DIR
        )
        msg_id = db.log_message(
            platform=instance["platform"], chat_id=chat_id, direction="out", source="dashboard",
            text=caption, instance_id=instance_id,
            attachment_path=rel_path, attachment_name=orig_name, attachment_mime=mime,
            attachment_size=size, thumbnail_path=thumb_path.name if thumb_path else None,
        )
        return {"ok": True, "id": msg_id}

    @app.post("/api/uploads/init", dependencies=[Depends(_require_token_or_api_key)])
    async def api_uploads_init(payload: dict = Body(...)):
        """Step 1 of the chunked-upload protocol — declares intent (which
        chat, how big, what filename) and gets back a session id plus the
        chunk size to use. See bot/attachments.py's create_upload_session
        docstring for why sessions live in memory rather than the DB."""
        instance_id = payload.get("instance_id")
        chat_id = payload.get("chat_id")
        filename = payload.get("filename") or "file"
        total_size = int(payload.get("total_size") or 0)
        mime = payload.get("mime")
        text = (payload.get("text") or "").strip()
        if not instance_id or not chat_id:
            raise HTTPException(status_code=400, detail="payload must include instance_id and chat_id")
        if bot_instances.get_instance(int(instance_id)) is None:
            raise HTTPException(status_code=404, detail=f"bot instance {instance_id} not found")
        try:
            session = attachments.create_upload_session(
                filename, total_size, mime, int(instance_id), str(chat_id), text, MAX_ATTACHMENT_BYTES
            )
        except ValueError as exc:
            raise HTTPException(status_code=413, detail=str(exc))
        return session

    @app.put("/api/uploads/{session_id}/chunk/{index}", dependencies=[Depends(_require_token_or_api_key)])
    async def api_uploads_chunk(session_id: str, index: int, request: Request):
        """Step 2, called once per chunk (any order, retriable) — the raw
        request body is streamed straight to disk, see attachments.write_chunk."""
        try:
            await attachments.write_chunk(session_id, index, request)
        except KeyError:
            raise HTTPException(status_code=404, detail="unknown or expired upload session")
        return {"ok": True}

    @app.post("/api/uploads/{session_id}/complete", dependencies=[Depends(_require_token_or_api_key)])
    async def api_uploads_complete(session_id: str):
        """Step 3 — assembles the chunks (off the event loop, can be
        gigabytes), relays through the bot if it's within the platform's
        own size limit, and always stores it server-side either way so it's
        pullable from any paired device regardless of relay outcome."""
        try:
            assembled = await asyncio.get_running_loop().run_in_executor(
                None, attachments.assemble_upload, session_id
            )
        except KeyError:
            raise HTTPException(status_code=404, detail="unknown or expired upload session")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        instance_id = assembled["instance_id"]
        chat_id = assembled["chat_id"]
        rel_path = assembled["rel_path"]
        display_name = assembled["display_name"]
        mime = assembled["mime"]
        size = assembled["size"]
        text = assembled["text"]
        instance = bot_instances.get_instance(instance_id)
        if instance is None:
            (attachments.ATTACHMENTS_DIR / rel_path).unlink(missing_ok=True)
            raise HTTPException(status_code=404, detail=f"bot instance {instance_id} not found")
        thumb_path = await asyncio.get_running_loop().run_in_executor(
            None, thumbnails.generate_thumbnail, attachments.ATTACHMENTS_DIR / rel_path, mime, attachments.THUMBS_DIR
        )
        relayed = size <= PLATFORM_RELAY_LIMIT_BYTES
        if relayed:
            try:
                await outbox.send_file(
                    instance_id, chat_id, str(attachments.ATTACHMENTS_DIR / rel_path), display_name, text or None
                )
            except RuntimeError as exc:
                raise HTTPException(status_code=503, detail=str(exc))
            except Exception as exc:
                raise HTTPException(status_code=502, detail=f"send failed: {exc}")
        msg_id = db.log_message(
            platform=instance["platform"], chat_id=chat_id, direction="out", source="dashboard",
            text=text, instance_id=instance_id,
            attachment_path=rel_path, attachment_name=display_name, attachment_mime=mime,
            attachment_size=size, thumbnail_path=thumb_path.name if thumb_path else None,
        )
        return {"ok": True, "id": msg_id, "relayed": relayed}

    @app.get("/api/chat/attachments/{message_id}", dependencies=[Depends(_require_token_or_api_key)])
    async def api_chat_attachment(message_id: int):
        row = db.get_message(message_id)
        if row is None or not row["attachment_path"]:
            raise HTTPException(status_code=404, detail="no attachment on this message")
        full_path = attachments.ATTACHMENTS_DIR / row["attachment_path"]
        if not full_path.resolve().is_relative_to(attachments.ATTACHMENTS_DIR.resolve()) or not full_path.is_file():
            raise HTTPException(status_code=404, detail="attachment file missing")
        return FileResponse(
            full_path,
            media_type=row["attachment_mime"] or "application/octet-stream",
            filename=row["attachment_name"] or full_path.name,
        )

    @app.get("/api/chat/attachments/{message_id}/thumbnail", dependencies=[Depends(_require_token_or_api_key)])
    async def api_chat_attachment_thumbnail(message_id: int):
        row = db.get_message(message_id)
        if row is None or not row["thumbnail_path"]:
            raise HTTPException(status_code=404, detail="no thumbnail for this attachment")
        full_path = attachments.THUMBS_DIR / row["thumbnail_path"]
        if not full_path.resolve().is_relative_to(attachments.THUMBS_DIR.resolve()) or not full_path.is_file():
            raise HTTPException(status_code=404, detail="thumbnail file missing")
        return FileResponse(full_path, media_type="image/jpeg")

    @app.get("/api/sessions", dependencies=[Depends(_require_token_or_api_key)])
    async def api_sessions(
        instance_id: Optional[int] = None,
        q: Optional[str] = None,
        since: Optional[str] = None,
        until: Optional[str] = None,
        limit: int = 50,
    ):
        rows = [dict(r) for r in db.list_sessions(instance_id=instance_id, q=q, since=since, until=until, limit=limit)]
        legacy = []
        # Legacy rows have no per-item timestamp/title to filter on, so a
        # search or date-range query — which the real title/last_activity_at
        # columns can satisfy — should exclude this synthetic bucket rather
        # than always showing it regardless of the filter.
        instance_ids = (
            [] if (q or since or until)
            else [instance_id] if instance_id is not None
            else [inst["id"] for inst in bot_instances.list_instances()]
        )
        for iid in instance_ids:
            count = db.count_legacy_items(iid)
            if count:
                legacy.append({
                    "id": f"legacy-{iid}",
                    "instance_id": iid,
                    "chat_id": None,
                    "title": "Before sessions",
                    "started_at": None,
                    "last_activity_at": None,
                    "item_count": count,
                    "legacy": True,
                })
        return rows + legacy

    @app.get("/api/sessions/{session_id}", dependencies=[Depends(_require_token_or_api_key)])
    async def api_session_detail(session_id: str):
        if session_id.startswith("legacy-"):
            iid = int(session_id.removeprefix("legacy-"))
            items = db.get_legacy_items(iid)
            session = {
                "id": session_id, "instance_id": iid, "chat_id": None,
                "title": "Before sessions", "started_at": None, "last_activity_at": None,
                "item_count": len(items["messages"]) + len(items["jobs"]), "legacy": True,
            }
        else:
            row = db.get_session(int(session_id))
            if row is None:
                raise HTTPException(status_code=404, detail=f"session {session_id} not found")
            session = dict(row)
            items = db.get_session_items(int(session_id))
        return {
            "session": session,
            "messages": [dict(r) for r in items["messages"]],
            "jobs": [dict(r) for r in items["jobs"]],
        }

    # -------------------------------------------------------- mobile keys --
    # Per-device credentials the Android app pairs with. Creating a key
    # accepts either the desktop token or an existing mobile key — an
    # already-paired phone can mint a sibling key to onboard a new device
    # without the PC. Listing and revoking stay strictly desktop-only:
    # a phone can bring a new device online but can't see or kill other
    # devices' access, so revoking a lost/stolen phone from the desktop
    # still cuts off anything it minted too.

    @app.post("/api/mobile-keys", dependencies=[Depends(_require_token_or_api_key)])
    async def api_mobile_keys_create(payload: dict = Body(...), caller: str = Depends(_identify_caller)):
        label = (payload.get("label") or "").strip() or "Unnamed device"
        key_id, plaintext = db.create_api_key(label)
        host = (payload.get("host") or "").strip()
        # host2 is an optional second, independent path to the same server
        # (e.g. a Tailscale hostname alongside a LAN IP) — the Android app
        # fails over between them automatically if one stops answering.
        host2 = (payload.get("host2") or "").strip()
        params = [f"key={plaintext}"]
        if host:
            params.insert(0, f"host={host}")
        if host2:
            params.append(f"host2={host2}")
        pair_uri = "botserver://pair?" + "&".join(params)
        img = qrcode.make(pair_uri, image_factory=PyPNGImage)
        buf = io.BytesIO()
        img.save(buf)
        qr_png_base64 = base64.b64encode(buf.getvalue()).decode("ascii")
        db.log_audit(actor=caller, action="mobile_key_create", detail=f"created key {key_id!r} ({label!r})")
        devices = await asyncio.get_running_loop().run_in_executor(None, db.list_devices)
        await _manager.broadcast({"type": "device_list", "devices": _annotate_online([dict(d) for d in devices])})
        return {"id": key_id, "label": label, "key": plaintext, "qr_png_base64": qr_png_base64}

    @app.get("/api/mobile-keys", dependencies=[Depends(_require_token)])
    async def api_mobile_keys_list():
        return [dict(r) for r in db.list_api_keys()]

    @app.put("/api/mobile-keys/{key_id}", dependencies=[Depends(_require_token)])
    async def api_mobile_keys_update(key_id: int, payload: dict = Body(...)):
        try:
            db.update_api_key_label(key_id, payload.get("label", ""))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        db.log_audit(actor="dashboard", action="mobile_key_rename", detail=f"renamed key {key_id}")
        devices = await asyncio.get_running_loop().run_in_executor(None, db.list_devices)
        await _manager.broadcast({"type": "device_list", "devices": _annotate_online([dict(d) for d in devices])})
        return {"ok": True}

    @app.delete("/api/mobile-keys/{key_id}", dependencies=[Depends(_require_token)])
    async def api_mobile_keys_revoke(key_id: int):
        db.revoke_api_key(key_id)
        db.log_audit(actor="dashboard", action="mobile_key_revoke", detail=f"revoked key {key_id}")
        devices = await asyncio.get_running_loop().run_in_executor(None, db.list_devices)
        await _manager.broadcast({"type": "device_list", "devices": _annotate_online([dict(d) for d in devices])})
        return {"ok": True}

    @app.post("/api/mobile-keys/purge-revoked", dependencies=[Depends(_require_token)])
    async def api_mobile_keys_purge_revoked():
        n = db.purge_revoked_keys()
        db.log_audit(actor="dashboard", action="mobile_keys_purge_revoked", detail=f"removed {n} revoked key(s)")
        return {"ok": True, "purged": n}

    # ------------------------------------------------------------ devices --
    # Live presence view — /api/devices for the initial snapshot on screen
    # load, /api/ws for deltas after that. Same _require_token_or_api_key
    # scope as chat/sessions: any paired device can see the presence list
    # (that's the point — a phone should see the tablet come online), only
    # mobile-key create/revoke (device *management*) stays desktop-only.

    @app.get("/api/devices", dependencies=[Depends(_require_token_or_api_key)])
    async def api_devices():
        devices = await asyncio.get_running_loop().run_in_executor(None, db.list_devices)
        return _annotate_online([dict(d) for d in devices])

    @app.websocket("/api/ws")
    async def ws_devices(websocket: WebSocket, token: Optional[str] = None):
        # Browser WebSocket/OkHttp can't set a custom header on the
        # handshake the way fetch() can, so auth travels as a query param
        # here instead of X-Dashboard-Token — the one deliberate exception
        # to this API's usual header-based auth.
        expected = os.environ.get("DASHBOARD_TOKEN")
        authed = bool(expected and token == expected)
        if not authed:
            authed = db.verify_api_key(token or "") is not None
        if not authed:
            await websocket.close(code=4401)
            return
        await _manager.connect(websocket)
        try:
            devices = await asyncio.get_running_loop().run_in_executor(None, db.list_devices)
            await websocket.send_json({"type": "device_list", "devices": _annotate_online([dict(d) for d in devices])})
            while True:
                await websocket.receive_text()
        except WebSocketDisconnect:
            pass
        finally:
            await _manager.disconnect(websocket)

    @app.post("/api/push/register")
    async def api_push_register(payload: dict = Body(...), api_key_id: int = Depends(_require_mobile_key_id)):
        fcm_token = (payload.get("fcm_token") or "").strip()
        if not fcm_token:
            raise HTTPException(status_code=400, detail="payload must be {fcm_token: str}")
        db.upsert_push_token(api_key_id, fcm_token)
        return {"ok": True}

    @app.exception_handler(Exception)
    async def unhandled(_request, exc: Exception):
        return JSONResponse(status_code=500, content={"detail": str(exc)})

    return app
