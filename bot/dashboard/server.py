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
import csv
import io
import json
import logging
import mimetypes
import os
import secrets
import socket
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import qrcode
from qrcode.image.pure import PyPNGImage
from fastapi import Body, Depends, FastAPI, File, Form, Header, HTTPException, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi import Response
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from bot import agent_control, attachments, bot_instances, db, desktop, envfile, kanban, outbox, pairing, platform_supervisor, setup_wizard, thumbnails
from bot.backends.base import BackendError
from bot import commands as bot_commands
from bot.config import config
from bot.router import VALID_BACKENDS, router
from bot.support_bot import hybrid as support_bot_hybrid
from bot.support_bot import training_data
from bot.support_bot.engine import support_bot
from bot.swarm import engine as swarm_engine
from bot.swarm import strategies as swarm_strategies

STATIC_DIR = Path(__file__).resolve().parent / "static"
LOG_FILE = envfile.PROJECT_ROOT / "logs" / "bot.log"
logger = logging.getLogger(__name__)


def _ts_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")

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


def _peer_public(row: dict) -> dict:
    """Strips the two fields no API response should ever echo back:
    outbound_api_key (the credential THIS server uses to call that peer —
    equivalent to a password) and inbound_api_key_id (an internal api_keys
    row id with no meaning to a client)."""
    return {k: v for k, v in row.items() if k not in ("outbound_api_key", "inbound_api_key_id")}


class _ConnectionManager:
    """Tracks live /api/ws sockets for broadcasting device-presence deltas,
    and (see register_device/send_to_device) for relaying WebRTC signaling
    messages directly between two specific devices' sockets — the
    rendezvous point mesh transport phase 2 needs when two devices aren't
    on the same LAN for MeshServer's direct-socket path (phase 1) to work.
    This server never looks at what's inside a signal payload; it only
    routes it to the named device's live socket, same as a STUN/TURN
    provider's signaling channel would, just built on the connection this
    server already has open. A plain in-process set is enough — this is a
    single-process server, no need for pub/sub across workers."""

    def __init__(self) -> None:
        self._sockets: set[WebSocket] = set()
        self._device_of: dict[WebSocket, int] = {}
        self._lock = asyncio.Lock()

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        async with self._lock:
            self._sockets.add(ws)

    async def register_device(self, ws: WebSocket, api_key_id: int) -> None:
        async with self._lock:
            self._device_of[ws] = api_key_id

    async def disconnect(self, ws: WebSocket) -> None:
        async with self._lock:
            self._sockets.discard(ws)
            self._device_of.pop(ws, None)

    async def broadcast(self, payload: dict) -> None:
        async with self._lock:
            sockets = list(self._sockets)
        for ws in sockets:
            try:
                await ws.send_json(payload)
            except Exception:
                await self.disconnect(ws)

    async def send_to_device(self, api_key_id: int, payload: dict) -> bool:
        """Best-effort, fire-and-forget: True only if that device currently
        has a live socket open. No queuing for an offline device — a
        WebRTC handshake that can't reach its peer right now has nothing
        to resume later anyway; the caller falls back to the server-relay
        download instead."""
        async with self._lock:
            targets = [ws for ws, dev_id in self._device_of.items() if dev_id == api_key_id]
        sent = False
        for ws in targets:
            try:
                await ws.send_json(payload)
                sent = True
            except Exception:
                await self.disconnect(ws)
        return sent


_manager = _ConnectionManager()


def _broadcast_soon(payload: dict) -> None:
    """Schedules a broadcast from a plain synchronous call site (db.py's
    on_message_logged/on_job_changed callbacks fire from inside a normal
    function call, not a coroutine) — every real caller of db.log_message/
    create_job/mark_job_* in this codebase runs on the event loop thread
    itself (a direct sqlite call inside an async handler, same as this
    file's own db.log_message call sites), so get_running_loop() succeeds
    in practice. Falls back to a logged warning rather than raising if
    that's ever not true, matching Router._invalidate()'s identical
    degraded-callback precedent — a missed live update is a real but minor
    gap (the client's own poll/reconnect logic still catches it up), not
    worth crashing the write that triggered it over."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        logger.warning("no running event loop to broadcast %s — clients will catch up on their next poll", payload.get("type"))
        return
    loop.create_task(_manager.broadcast(payload))


def _on_message_logged(message_id: int) -> None:
    row = db.get_message(message_id)
    if row is None:
        return
    _broadcast_soon({"type": "chat_message", "instance_id": row["instance_id"], "message": dict(row)})


def _on_job_changed(job_id: int) -> None:
    row = db.get_job(job_id)
    if row is None:
        return
    _broadcast_soon({"type": "job_update", "job": dict(row)})


db.on_message_logged(_on_message_logged)
db.on_job_changed(_on_job_changed)


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


def _mesh_port_header(x_mesh_port: Optional[str] = Header(default=None)) -> Optional[int]:
    return int(x_mesh_port) if x_mesh_port and x_mesh_port.isdigit() else None


def _identify_caller(
    request: Request,
    x_dashboard_token: Optional[str] = Header(default=None),
    x_device_platform: Optional[str] = Header(default=None),
    x_device_app_version: Optional[str] = Header(default=None),
    x_device_model: Optional[str] = Header(default=None),
    x_device_os_version: Optional[str] = Header(default=None),
    mesh_port: Optional[int] = Depends(_mesh_port_header),
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
    pairing label — instead of just proving that something is. X-Mesh-Port,
    if present, is that device's own self-reported mesh-listener port (see
    MeshServer.kt) — recorded alongside request.client.host so another
    device on the same LAN can be told exactly where to dial this one for a
    direct APK transfer, without this server ever brokering the bytes."""
    expected = os.environ.get("DASHBOARD_TOKEN")
    if expected and x_dashboard_token == expected:
        return "dashboard"
    client_host = request.client.host if request.client else None
    if db.verify_api_key(
        x_dashboard_token or "",
        platform=x_device_platform,
        app_version=x_device_app_version,
        device_model=x_device_model,
        os_version=x_device_os_version,
        local_ip=client_host,
        mesh_port=mesh_port,
    ) is not None:
        return "mobile"
    if not expected:
        raise HTTPException(status_code=503, detail="DASHBOARD_TOKEN is not set in .env")
    raise HTTPException(status_code=401, detail="invalid dashboard token or api key")


def _require_token_or_api_key(caller: str = Depends(_identify_caller)) -> None:
    return None


def _caller_device_id(
    request: Request,
    x_dashboard_token: Optional[str] = Header(default=None),
    x_device_platform: Optional[str] = Header(default=None),
    x_device_app_version: Optional[str] = Header(default=None),
    x_device_model: Optional[str] = Header(default=None),
    x_device_os_version: Optional[str] = Header(default=None),
    mesh_port: Optional[int] = Depends(_mesh_port_header),
) -> Optional[int]:
    """Like _require_token_or_api_key, but resolves to *which* device is
    calling instead of just "someone valid is." Returns None for the
    desktop DASHBOARD_TOKEN (not tied to any one device row) and raises 401
    for anything else invalid — so a route depending on this both
    authenticates and learns the caller's own api_keys id in one step.
    Used by the mesh APK-push routes, which need to know whose device is
    volunteering to be the transfer's origin."""
    expected = os.environ.get("DASHBOARD_TOKEN")
    if expected and x_dashboard_token == expected:
        return None
    client_host = request.client.host if request.client else None
    key_id = db.verify_api_key(
        x_dashboard_token or "",
        platform=x_device_platform,
        app_version=x_device_app_version,
        device_model=x_device_model,
        os_version=x_device_os_version,
        local_ip=client_host,
        mesh_port=mesh_port,
    )
    if key_id is None:
        raise HTTPException(status_code=401, detail="invalid dashboard token or api key")
    return key_id


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


def _require_device_id(x_dashboard_token: Optional[str] = Header(default=None)) -> int:
    """Resolves the caller to a Server Chat device id: 0 (the reserved
    desktop sentinel — see db.SERVER_CHAT_DESKTOP_DEVICE_ID) for the
    desktop's own DASHBOARD_TOKEN, or the caller's own api_keys.id for a
    paired phone's mobile key. Used only by /api/server-chat/* — every
    other mobile-reachable route treats "desktop or any paired device"
    as one undifferentiated tier (_require_token_or_api_key); Server Chat
    is the one place that actually needs to know *which* device is asking."""
    expected = os.environ.get("DASHBOARD_TOKEN")
    if expected and x_dashboard_token == expected:
        return db.SERVER_CHAT_DESKTOP_DEVICE_ID
    key_id = db.verify_api_key(x_dashboard_token or "")
    if key_id is not None:
        return key_id
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
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _lifespan(app: FastAPI):
        task = asyncio.create_task(_presence_broadcaster())
        try:
            yield
        finally:
            task.cancel()

    app = FastAPI(title="Bot Control Dashboard API", lifespan=_lifespan)

    def _json_download(data, filename: str) -> Response:
        body = json.dumps(data, indent=2, default=str).encode("utf-8")
        return Response(
            content=body, media_type="application/json",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

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

    # ------------------------------------------------------ ops endpoints --
    # Unauthenticated by design, like a load balancer's/orchestrator's health
    # probe is expected to be — neither returns anything a token would need
    # to protect (no secrets, no message content).

    @app.get("/healthz")
    async def healthz():
        try:
            db.get_conn().execute("SELECT 1")
            db_ok = True
        except Exception:
            db_ok = False
        status_code = 200 if db_ok else 503
        return JSONResponse({"status": "ok" if db_ok else "degraded", "db_ok": db_ok}, status_code=status_code)

    @app.get("/metrics")
    async def metrics():
        # Hand-rolled Prometheus text exposition format rather than the
        # prometheus_client dependency — this project's bundled venv is
        # deliberately kept minimal (see the NumPy-over-scikit-learn
        # rewrite), and a handful of gauges/counters don't need a library.
        overview = db.get_overview()
        lines = [
            "# HELP botserver_up Always 1 if this endpoint responded at all.",
            "# TYPE botserver_up gauge",
            "botserver_up 1",
            "# HELP botserver_jobs_running Jobs currently running.",
            "# TYPE botserver_jobs_running gauge",
            f"botserver_jobs_running {overview.get('jobs_running', 0)}",
            "# HELP botserver_jobs_queued Jobs currently queued.",
            "# TYPE botserver_jobs_queued gauge",
            f"botserver_jobs_queued {overview.get('jobs_queued', 0)}",
            "# HELP botserver_jobs_completed_today Jobs completed successfully today (resets at midnight local time).",
            "# TYPE botserver_jobs_completed_today counter",
            f"botserver_jobs_completed_today {overview.get('completed_today', 0)}",
            "# HELP botserver_jobs_failed_today Jobs failed today (resets at midnight local time).",
            "# TYPE botserver_jobs_failed_today counter",
            f"botserver_jobs_failed_today {overview.get('failed_today', 0)}",
            "# HELP botserver_job_success_rate_7d Fraction of jobs that succeeded over the trailing 7 days.",
            "# TYPE botserver_job_success_rate_7d gauge",
            f"botserver_job_success_rate_7d {overview.get('success_rate_7d', 0.0)}",
            "# HELP botserver_job_avg_duration_ms Average job duration in milliseconds.",
            "# TYPE botserver_job_avg_duration_ms gauge",
            f"botserver_job_avg_duration_ms {overview.get('avg_duration_ms', 0)}",
            "# HELP botserver_db_size_bytes SQLite database file size in bytes.",
            "# TYPE botserver_db_size_bytes gauge",
            f"botserver_db_size_bytes {db.get_db_size_bytes()}",
        ]
        return Response("\n".join(lines) + "\n", media_type="text/plain; version=0.0.4")

    # WhatsApp Cloud API delivers messages via a webhook Meta calls
    # directly — it can't send a dashboard token, so these two routes are
    # deliberately unauthenticated (same posture as /healthz above). The
    # POST route's real security boundary is the X-Hub-Signature-256 HMAC
    # check in whatsapp_platform.verify_signature(), not a header token.
    # See bot/platforms/whatsapp_platform.py's module docstring for setup.
    @app.get("/webhooks/whatsapp")
    async def whatsapp_verify(request: Request):
        from bot.platforms import whatsapp_platform

        params = request.query_params
        challenge = whatsapp_platform.verify_challenge(
            params.get("hub.mode", ""), params.get("hub.verify_token", ""), params.get("hub.challenge", "")
        )
        if challenge is None:
            raise HTTPException(status_code=403, detail="verification failed")
        return PlainTextResponse(challenge)

    @app.post("/webhooks/whatsapp")
    async def whatsapp_webhook(request: Request):
        from bot.platforms import whatsapp_platform

        raw = await request.body()
        if not whatsapp_platform.verify_signature(raw, request.headers.get("x-hub-signature-256", "")):
            raise HTTPException(status_code=403, detail="invalid signature")
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return {"ok": True}
        # Ack immediately — Meta retries aggressively if this endpoint is
        # slow, and a real agent turn can easily take longer than its
        # timeout. The reply goes out separately via the Graph API once
        # router.ask()/dispatch_command() finish, same as every other
        # platform's outbound path.
        asyncio.create_task(whatsapp_platform.handle_webhook_payload(payload))
        return {"ok": True}

    @app.get("/api/hotreload/status", dependencies=[Depends(_require_token)])
    async def api_hotreload_status():
        from bot import hotreload

        return hotreload.status()

    @app.post("/api/hotreload/run", dependencies=[Depends(_require_token)])
    async def api_hotreload_run():
        from bot import hotreload

        return await hotreload.trigger_manual_reload()

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
        overview["default_hermes_backend"] = config.current.get("default_hermes_backend")
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

    @app.get("/api/export/tables", dependencies=[Depends(_require_token)])
    async def api_export_tables():
        return {"tables": db.EXPORTABLE_TABLES}

    @app.get("/api/export/{table}", dependencies=[Depends(_require_token)])
    async def api_export_table(table: str, format: str = "json"):
        try:
            rows = db.export_table(table)
        except ValueError as e:
            raise HTTPException(status_code=404, detail=str(e))
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

    @app.get("/api/config")
    async def api_config():
        # This read (like most GET endpoints in this section) has no auth
        # gate, so the TURN shared secret must never appear in it verbatim
        # — same reasoning as never echoing it back after it's set. The
        # dashboard UI only needs to know whether one is configured at all.
        current = config.current
        turn_cfg = current.get("turn")
        if isinstance(turn_cfg, dict) and turn_cfg.get("secret"):
            current["turn"] = {**turn_cfg, "secret": None, "secret_set": True}
        return {
            "version": config.version,
            "current": current,
            "history": [dict(r) for r in db.list_config_history(limit=20)],
        }

    @app.get("/api/models")
    async def api_models(instance_id: Optional[int] = None):
        from bot.models import BACKEND_FAMILY, live_api_models, live_custom_models, live_hermes_models

        live_api = await live_api_models()
        live_hermes = live_hermes_models()
        live_custom = await live_custom_models()
        result = {
            "family": BACKEND_FAMILY,
            "current": {
                name: (config.current.get("backends", {}).get(name) or {}).get("model")
                for name in ("api", "hermes_cli", "hermes_gateway")
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
        # bot-server MCP server's list_available_models tool proxies
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

    @app.get("/api/providers", dependencies=[Depends(_require_token)])
    async def api_providers_list():
        from bot import providers

        return {
            "providers": [
                {"name": name, "base_url": entry.get("base_url"), "protocol": entry.get("protocol", "openai"),
                 "api_key_env": entry.get("api_key_env"), "has_inline_key": bool(entry.get("api_key"))}
                for name, entry in sorted(providers.list_providers().items())
            ]
        }

    @app.post("/api/providers", dependencies=[Depends(_require_token)])
    async def api_providers_set(payload: dict = Body(...)):
        from bot import providers

        try:
            providers.set_provider(
                payload.get("name", ""),
                payload.get("base_url", ""),
                protocol=payload.get("protocol", "openai"),
                api_key_env=payload.get("api_key_env") or None,
                api_key=payload.get("api_key") or None,
                actor="dashboard",
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return {"ok": True}

    @app.delete("/api/providers/{name}", dependencies=[Depends(_require_token)])
    async def api_providers_delete(name: str):
        from bot import providers

        if not providers.delete_provider(name, actor="dashboard"):
            raise HTTPException(status_code=404, detail=f"no provider named {name!r}")
        return {"ok": True}

    @app.get("/api/plugins", dependencies=[Depends(_require_token)])
    async def api_plugins_list():
        from bot import plugins as plugin_registry

        return {"plugins": plugin_registry.list_plugins()}

    @app.post("/api/plugins", dependencies=[Depends(_require_token)])
    async def api_plugins_install(payload: dict = Body(...)):
        from bot import plugins as plugin_registry

        try:
            info = plugin_registry.install(payload.get("path", ""))
        except plugin_registry.PluginError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        db.log_audit(actor="dashboard", action="plugin_install", detail=info["name"])
        return info

    @app.post("/api/plugins/{name}/enable", dependencies=[Depends(_require_token)])
    async def api_plugins_enable(name: str):
        from bot import plugins as plugin_registry

        try:
            info = plugin_registry.enable(name)
        except plugin_registry.PluginError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        db.log_audit(actor="dashboard", action="plugin_enable", detail=name)
        return info

    @app.post("/api/plugins/{name}/disable", dependencies=[Depends(_require_token)])
    async def api_plugins_disable(name: str):
        from bot import plugins as plugin_registry

        try:
            info = plugin_registry.disable(name)
        except plugin_registry.PluginError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        db.log_audit(actor="dashboard", action="plugin_disable", detail=name)
        return info

    @app.delete("/api/plugins/{name}", dependencies=[Depends(_require_token)])
    async def api_plugins_delete(name: str):
        from bot import plugins as plugin_registry

        if not plugin_registry.remove(name):
            raise HTTPException(status_code=404, detail=f"no plugin named {name!r}")
        db.log_audit(actor="dashboard", action="plugin_remove", detail=name)
        return {"ok": True}

    @app.get("/api/personas")
    async def api_personas():
        from bot.personas import list_personas

        return list_personas()

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

    @app.get("/api/platform-guides")
    async def api_platform_guides():
        from bot.platform_guides import PLATFORM_GUIDES

        return PLATFORM_GUIDES

    @app.post("/api/validate-field", dependencies=[Depends(_require_token_or_api_key)])
    async def api_validate_field(payload: dict = Body(...)):
        from bot.validators import validate_field

        ok, message = validate_field(payload.get("platform", ""), payload.get("field", ""), payload.get("value", ""))
        return {"ok": ok, "message": message}

    @app.get("/api/bots", dependencies=[Depends(_require_token_or_api_key)])
    async def api_bots_list():
        from bot.router import router as _router

        live = platform_supervisor.status()
        rows = bot_instances.list_instances()
        for row in rows:
            row["live_running"] = live.get(row["id"], {}).get("running", False)
            row["circuit"] = _router.circuit_status(row["id"])
        return rows

    @app.post("/api/bots/{instance_id}/circuit/reset", dependencies=[Depends(_require_token_or_api_key)])
    async def api_bots_circuit_reset(instance_id: int):
        from bot.router import router as _router

        _router.reset_circuit(instance_id)
        db.log_audit(actor="dashboard", action="circuit_breaker_reset", detail=f"instance {instance_id}")
        return {"ok": True}

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
                admin_user_ids=payload.get("admin_user_ids") or [],
                action_overrides=payload.get("action_overrides") or {},
                can_target=payload.get("can_target") or [],
                enabled=bool(payload.get("enabled", True)),
                model=payload.get("model") or None,
                custom_instructions=payload.get("custom_instructions") or None,
                persona=payload.get("persona") or None,
                hermes_home=payload.get("hermes_home") or None,
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
            if k in ("name", "platform", "backend", "enabled", "credentials", "allowed_user_ids", "admin_user_ids", "action_overrides", "can_target", "model", "custom_instructions", "persona", "hermes_home")
        }
        before = bot_instances.get_instance(instance_id)
        try:
            bot_instances.update_instance(instance_id, actor="dashboard", **fields)
        except bot_instances.ValidationError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        # A hermes_gateway backend that changed model/hermes_home gets a
        # brand-new cache slot (see Router._get_backend) — the OLD backend
        # object, still holding its spawned `hermes serve` subprocess, is
        # otherwise orphaned forever under its now-unreachable old cache
        # key. Evict it so its subprocess actually gets terminated.
        if (
            before
            and before.get("backend") == "hermes_gateway"
            and ("model" in fields or "hermes_home" in fields)
            and (fields.get("model", before.get("model")) != before.get("model")
                 or fields.get("hermes_home", before.get("hermes_home")) != before.get("hermes_home"))
        ):
            await router.evict_backend("hermes_gateway", model_override=before.get("model"), hermes_home=before.get("hermes_home"))
        return {"ok": True}

    @app.delete("/api/bots/{instance_id}", dependencies=[Depends(_require_token_or_api_key)])
    async def api_bots_delete(instance_id: int):
        await platform_supervisor.stop_instance(instance_id)
        instance = bot_instances.get_instance(instance_id)
        try:
            bot_instances.delete_instance(instance_id, actor="dashboard")
        except bot_instances.ValidationError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        if instance and instance.get("backend") == "hermes_gateway":
            await router.evict_backend(
                "hermes_gateway", model_override=instance.get("model"), hermes_home=instance.get("hermes_home")
            )
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

    # ---------------------------------------------------------- schedules --
    # Surfaces bot/scheduler.py's existing recurring-prompt store (already
    # backing /cron, /loop, /heartbeat in chat) over HTTP — previously only
    # reachable from inside a chat with the bot, invisible to the dashboard/
    # desktop UI and any future TUI.

    @app.get("/api/bots/{instance_id}/schedules", dependencies=[Depends(_require_token_or_api_key)])
    async def api_bots_schedules_list(instance_id: int):
        from bot import scheduler

        return scheduler.list_for_chat(instance_id, chat_id=None)

    @app.post("/api/bots/{instance_id}/schedules", dependencies=[Depends(_require_token_or_api_key)])
    async def api_bots_schedules_create(instance_id: int, payload: dict = Body(...)):
        from bot import scheduler

        try:
            interval_s = scheduler.parse_duration(str(payload.get("interval", "")))
            sched_id = scheduler.create(
                instance_id,
                payload.get("chat_id"),
                payload.get("kind", "cron"),
                payload.get("prompt", ""),
                interval_s,
                max_runs=payload.get("max_runs"),
                thread_id=payload.get("thread_id"),
            )
        except scheduler.ScheduleError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        db.log_audit(actor="dashboard", action="schedule_create", detail=f"instance {instance_id}, schedule {sched_id}")
        return {"ok": True, "id": sched_id}

    @app.post("/api/bots/{instance_id}/schedules/{sched_id}/pause", dependencies=[Depends(_require_token_or_api_key)])
    async def api_bots_schedules_pause(instance_id: int, sched_id: int):
        from bot import scheduler

        scheduler.pause(sched_id)
        return {"ok": True}

    @app.post("/api/bots/{instance_id}/schedules/{sched_id}/resume", dependencies=[Depends(_require_token_or_api_key)])
    async def api_bots_schedules_resume(instance_id: int, sched_id: int):
        from bot import scheduler

        scheduler.resume(sched_id)
        return {"ok": True}

    @app.delete("/api/bots/{instance_id}/schedules/{sched_id}", dependencies=[Depends(_require_token_or_api_key)])
    async def api_bots_schedules_delete(instance_id: int, sched_id: int):
        from bot import scheduler

        scheduler.remove(sched_id)
        db.log_audit(actor="dashboard", action="schedule_delete", detail=f"instance {instance_id}, schedule {sched_id}")
        return {"ok": True}

    # ----------------------------------------------------------- pairing --
    # Approving/denying a pending chat-platform pairing request — see
    # bot/pairing.py. Listing is scoped to one instance when instance_id is
    # given, otherwise every pending request across every bot (for a
    # dashboard-wide "Pending Pairings" panel).

    @app.get("/api/pairing", dependencies=[Depends(_require_token_or_api_key)])
    async def api_pairing_list(instance_id: Optional[int] = None):
        return {"pending": pairing.list_pending(instance_id)}

    @app.post("/api/pairing/{pairing_id}/approve", dependencies=[Depends(_require_token_or_api_key)])
    async def api_pairing_approve(pairing_id: int):
        try:
            row = pairing.approve(pairing_id, actor="dashboard")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return {"ok": True, "pairing": row}

    @app.post("/api/pairing/{pairing_id}/deny", dependencies=[Depends(_require_token_or_api_key)])
    async def api_pairing_deny(pairing_id: int):
        try:
            row = pairing.deny(pairing_id, actor="dashboard")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return {"ok": True, "pairing": row}

    # ------------------------------------------------------------ kanban --
    # A per-bot-instance kanban board — see bot/kanban.py, /kanban.

    @app.get("/api/kanban/boards", dependencies=[Depends(_require_token_or_api_key)])
    async def api_kanban_boards(instance_id: int):
        return {"boards": kanban.list_boards(instance_id)}

    @app.get("/api/kanban/cards", dependencies=[Depends(_require_token_or_api_key)])
    async def api_kanban_cards(instance_id: int, board: str = "default"):
        return {"cards": kanban.list_cards(instance_id, board)}

    @app.post("/api/kanban/cards", dependencies=[Depends(_require_token_or_api_key)])
    async def api_kanban_add_card(payload: dict = Body(...)):
        try:
            card = kanban.add_card(
                int(payload["instance_id"]), payload.get("board", "default"),
                payload.get("column", "todo"), payload.get("text", ""),
            )
        except kanban.KanbanError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return {"ok": True, "card": card}

    @app.post("/api/kanban/cards/{card_id}/move", dependencies=[Depends(_require_token_or_api_key)])
    async def api_kanban_move_card(card_id: int, payload: dict = Body(...)):
        try:
            card = kanban.move_card(int(payload["instance_id"]), card_id, payload.get("column", "todo"))
        except kanban.KanbanError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return {"ok": True, "card": card}

    @app.delete("/api/kanban/cards/{card_id}", dependencies=[Depends(_require_token_or_api_key)])
    async def api_kanban_delete_card(card_id: int, instance_id: int):
        ok = kanban.delete_card(instance_id, card_id)
        if not ok:
            raise HTTPException(status_code=404, detail="card not found")
        return {"ok": True}

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

    # ------------------------------------------------- Hermes delegation --
    # Configures and drives Hermes Agent's own delegate_task sub-agent
    # system (see bot/hermes_config.py's module docstring for exactly why
    # this can only ever set config + send a prompt, never invoke
    # delegate_task directly). Until per-instance HERMES_HOME isolation
    # lands, every hermes_gateway-backed instance shares one
    # ~/.hermes/config.yaml — a real, documented limitation, not hidden.

    def _require_hermes_gateway_instance(instance_id: int) -> dict:
        instance = bot_instances.get_instance(instance_id)
        if instance is None:
            raise HTTPException(status_code=404, detail=f"bot instance {instance_id} not found")
        if instance.get("backend") != "hermes_gateway":
            raise HTTPException(
                status_code=400,
                detail=f"instance {instance_id} is backed by {instance.get('backend')!r}, not hermes_gateway — "
                       "delegation configuration only applies to Hermes gateway-backed instances",
            )
        return instance

    def _require_hermes_backed_instance(instance_id: int) -> dict:
        """Looser than _require_hermes_gateway_instance — accepts both
        Hermes backends. MCP-server registration is a property of the
        underlying `hermes` install's own config.yaml, not of which
        subprocess-management strategy BotServer uses to talk to it, so
        registering bot-server's MCP server works identically for
        hermes_cli (no gateway/eviction needed at all — hermes_cli spawns
        a fresh `hermes -z` process per call, which re-reads config.yaml
        fresh every time) and hermes_gateway (needs the eviction dance
        since its persistent process only reads config at spawn time)."""
        instance = bot_instances.get_instance(instance_id)
        if instance is None:
            raise HTTPException(status_code=404, detail=f"bot instance {instance_id} not found")
        if instance.get("backend") not in ("hermes_cli", "hermes_gateway"):
            raise HTTPException(
                status_code=400,
                detail=f"instance {instance_id} is backed by {instance.get('backend')!r}, not a Hermes backend "
                       "(hermes_cli/hermes_gateway)",
            )
        return instance

    @app.get("/api/hermes/{instance_id}/delegation", dependencies=[Depends(_require_token_or_api_key)])
    async def api_hermes_delegation_get(instance_id: int):
        from bot import hermes_config

        instance = _require_hermes_gateway_instance(instance_id)
        return {"delegation": hermes_config.read_delegation_config(instance.get("hermes_home"))}

    @app.post("/api/hermes/{instance_id}/delegation", dependencies=[Depends(_require_token_or_api_key)])
    async def api_hermes_delegation_set(instance_id: int, payload: dict = Body(...)):
        from bot import hermes_config

        instance = _require_hermes_gateway_instance(instance_id)
        if payload.get("subagent_auto_approve") is True and not payload.get("confirm"):
            raise HTTPException(
                status_code=400,
                detail="subagent_auto_approve=true lets sub-agents run dangerous commands "
                       "(shell, file writes) with no human approval — pass confirm=true to acknowledge this.",
            )
        delegation = hermes_config.set_delegation_config(
            provider=payload.get("provider"),
            model=payload.get("model"),
            max_concurrent_children=payload.get("max_concurrent_children"),
            max_spawn_depth=payload.get("max_spawn_depth"),
            subagent_auto_approve=payload.get("subagent_auto_approve"),
            hermes_home=instance.get("hermes_home"),
            actor="dashboard",
        )
        return {"ok": True, "delegation": delegation}

    @app.post("/api/hermes/{instance_id}/dispatch", dependencies=[Depends(_require_token_or_api_key)])
    async def api_hermes_dispatch(instance_id: int, payload: dict = Body(...)):
        """Configures delegation defaults for this instance (if a
        worker_provider/worker_model was given, or one was auto-picked as
        the currently-cheapest free model) then sends a goal prompt asking
        Hermes's own agent to use delegate_task to fan it out — see
        bot/swarm/prompts.py's module docstring for why the prompt itself,
        not an external RPC, is the actual dispatch mechanism."""
        from bot import hermes_config
        from bot.models import hermes_models_with_pricing
        from bot.swarm.prompts import hermes_delegation_goal

        instance = _require_hermes_gateway_instance(instance_id)
        goal = (payload.get("goal") or "").strip()
        if not goal:
            raise HTTPException(status_code=400, detail="payload must include a non-empty 'goal'")

        worker_provider = payload.get("worker_provider")
        worker_model = payload.get("worker_model")
        pricing_source = None
        if not worker_provider or not worker_model:
            priced, pricing_source = await hermes_models_with_pricing(instance_id)
            for provider_name, entries in sorted(priced.items()):
                free_entry = next((e for e in sorted(entries, key=lambda e: e["id"]) if e["free"]), None)
                if free_entry:
                    worker_provider, worker_model = provider_name, free_entry["id"]
                    break

        max_children = payload.get("max_children")
        if worker_provider and worker_model:
            hermes_config.set_delegation_config(
                provider=worker_provider, model=worker_model,
                max_concurrent_children=max_children, hermes_home=instance.get("hermes_home"),
                actor="dashboard",
            )

        prompt = hermes_delegation_goal(
            goal, worker_provider=worker_provider, worker_model=worker_model, max_children=max_children,
        )
        db.log_audit(
            actor="dashboard", action="swarm_dispatch",
            detail=f"instance {instance_id} -> {worker_provider}/{worker_model}: {goal[:120]}",
        )
        try:
            result = await router.ask(prompt, action_type="swarm_dispatch", instance_id=instance_id)
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"dispatch failed: {exc}")
        return {
            "ok": True,
            "result": result.text,
            "worker_provider": worker_provider,
            "worker_model": worker_model,
            "worker_model_source": pricing_source,
        }

    @app.post("/api/hermes/{instance_id}/enable-swarm-tools", dependencies=[Depends(_require_token_or_api_key)])
    async def api_hermes_enable_swarm_tools(instance_id: int):
        """Gives this Hermes instance's own agent the same cross-instance
        organizing ability Claude gets via this MCP server and api-backend
        agents get via delegate_to_instance: registers bot-server's own
        MCP server into the instance's mcp_servers config. For
        hermes_gateway this also evicts the cached backend so the NEXT
        call spawns a fresh gateway process that actually loads it
        (mcp_servers are read at gateway startup, never hot-reloaded);
        hermes_cli needs no eviction at all — it spawns a fresh `hermes
        -z` process per call, which re-reads config.yaml fresh every
        time, so the change is already live on the very next message."""
        from bot import hermes_config

        instance = _require_hermes_backed_instance(instance_id)
        token = os.environ.get("DASHBOARD_TOKEN") or envfile.get_var("DASHBOARD_TOKEN")
        registration = hermes_config.register_botserver_mcp_server(
            hermes_home=instance.get("hermes_home"), dashboard_token=token, actor="dashboard",
        )
        note = "takes effect on this instance's next message"
        if instance.get("backend") == "hermes_gateway":
            await router.evict_backend(
                "hermes_gateway", model_override=instance.get("model"), hermes_home=instance.get("hermes_home")
            )
            note += " (fresh gateway spawn)"
        return {"ok": True, "registration": registration, "note": note}

    @app.post("/api/hermes/{instance_id}/disable-swarm-tools", dependencies=[Depends(_require_token_or_api_key)])
    async def api_hermes_disable_swarm_tools(instance_id: int):
        from bot import hermes_config

        instance = _require_hermes_backed_instance(instance_id)
        removed = hermes_config.unregister_botserver_mcp_server(hermes_home=instance.get("hermes_home"), actor="dashboard")
        if removed and instance.get("backend") == "hermes_gateway":
            await router.evict_backend(
                "hermes_gateway", model_override=instance.get("model"), hermes_home=instance.get("hermes_home")
            )
        return {"ok": True, "removed": removed}

    @app.get("/api/hermes/swarm-tools-status", dependencies=[Depends(_require_token_or_api_key)])
    async def api_hermes_swarm_tools_status():
        """For the dashboard's swarm-tools panel: every Hermes-backed
        instance (hermes_cli or hermes_gateway) with whether it currently
        has bot-server's MCP server registered in its own config (see
        hermes_config.is_botserver_mcp_registered) — a config-file read,
        not a live "is the running gateway actually connected to it"
        check, since that would require spawning/probing every
        instance's gateway just to render a panel."""
        from bot import hermes_config

        rows = []
        for instance in bot_instances.list_instances():
            if instance.get("backend") not in ("hermes_cli", "hermes_gateway"):
                continue
            rows.append({
                "id": instance["id"],
                "name": instance["name"],
                "backend": instance["backend"],
                "hermes_home": instance.get("hermes_home"),
                "swarm_tools_enabled": hermes_config.is_botserver_mcp_registered(instance.get("hermes_home")),
            })
        return {"instances": rows}

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

    @app.get("/api/snapshots", dependencies=[Depends(_require_token)])
    async def api_snapshots_list():
        from bot import snapshots

        return {"snapshots": snapshots.list_snapshots()}

    @app.post("/api/snapshots", dependencies=[Depends(_require_token)])
    async def api_snapshots_create(payload: dict = Body(default={})):
        from bot import snapshots

        manifest = snapshots.create_snapshot(label=(payload or {}).get("label") or None)
        db.log_audit(actor="dashboard", action="snapshot_create", detail=manifest["name"])
        return manifest

    @app.post("/api/snapshots/{name}/restore", dependencies=[Depends(_require_token)])
    async def api_snapshots_restore(name: str):
        from bot import snapshots

        try:
            snapshots.restore_snapshot(name)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        db.log_audit(actor="dashboard", action="snapshot_restore", detail=name)
        return {"ok": True}

    @app.delete("/api/snapshots/{name}", dependencies=[Depends(_require_token)])
    async def api_snapshots_delete(name: str):
        from bot import snapshots

        if not snapshots.delete_snapshot(name):
            raise HTTPException(status_code=404, detail=f"no snapshot named {name!r}")
        return {"ok": True}

    @app.post("/api/backend/{action_or_default}/{backend}", dependencies=[Depends(_require_token_or_api_key)])
    async def api_set_backend(action_or_default: str, backend: str):
        if backend not in VALID_BACKENDS:
            raise HTTPException(status_code=400, detail=f"unknown backend {backend!r}")
        if action_or_default == "default":
            # Claude and Hermes each keep their own default-backend slot —
            # the two are shown as separate segmented pickers in the
            # dashboard, and must stay separate in storage too: they used to
            # share config["default_backend"], so picking a Hermes default
            # silently overwrote the global Claude-oriented fallback every
            # action_type without its own override falls back to.
            from bot.models import BACKEND_FAMILY

            key = "default_hermes_backend" if BACKEND_FAMILY.get(backend) == "hermes" else "default_backend"
            config.set_value([key], backend, actor="dashboard")
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

    # Training tab — user-added phrases for the Support Bot's hybrid
    # classifier (TF-IDF centroid model + trained neural network, see
    # bot/support_bot/hybrid.py), layered on top of training_data.py's
    # hand-authored baseline. Every mutation retrains both sub-models in
    # place so it takes effect immediately, no restart.
    @app.get("/api/support-bot/training", dependencies=[Depends(_require_token_or_api_key)])
    async def api_support_bot_training_list():
        return {
            "phrases": [dict(r) for r in db.list_support_bot_phrases()],
            "intents": sorted({intent for _, intent in training_data.EXAMPLES}),
        }

    @app.post("/api/support-bot/training", dependencies=[Depends(_require_token_or_api_key)])
    async def api_support_bot_training_add(payload: dict = Body(...)):
        phrase = (payload.get("phrase") or "").strip()
        intent = (payload.get("intent") or "").strip()
        if not phrase or not intent:
            raise HTTPException(status_code=400, detail="payload must be {phrase: str, intent: str}")
        phrase_id = db.add_support_bot_phrase(phrase, intent)
        counts = support_bot_hybrid.retrain_all()
        db.log_audit(actor="dashboard", action="support_bot_phrase_add", detail=f"{phrase!r} -> {intent}")
        return {"ok": True, "id": phrase_id, "trained_on": counts}

    @app.delete("/api/support-bot/training/{phrase_id}", dependencies=[Depends(_require_token_or_api_key)])
    async def api_support_bot_training_delete(phrase_id: int):
        db.delete_support_bot_phrase(phrase_id)
        counts = support_bot_hybrid.retrain_all()
        db.log_audit(actor="dashboard", action="support_bot_phrase_delete", detail=f"id {phrase_id}")
        return {"ok": True, "trained_on": counts}

    # Self-monitoring: the hybrid classifier's own logged behavior over
    # real traffic — agreement rate between its two sub-models, unknown
    # rate, confidence trends. See bot/support_bot/hybrid.py's health().
    @app.get("/api/support-bot/health", dependencies=[Depends(_require_token_or_api_key)])
    async def api_support_bot_health():
        return support_bot_hybrid.health()

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

    @app.get("/api/chat/messages/export", dependencies=[Depends(_require_token_or_api_key)])
    async def api_chat_messages_export(instance_id: int, chat_id: Optional[str] = None, platform: Optional[str] = None):
        # No chat_id: the whole bot's merged history, matching what the
        # Chat tab itself displays (one timeline per instance, every
        # chat_id combined) — see refreshChat() in dashboard.html.
        if chat_id is None:
            data = db.export_instance_messages_data(instance_id)
        else:
            data = db.export_chat_messages_data(instance_id, chat_id, platform=platform)
        suffix = f"-{chat_id}" if chat_id else ""
        return _json_download(
            {"instance_id": instance_id, "chat_id": chat_id, "messages": data},
            f"chat-{instance_id}{suffix}-{_ts_stamp()}.json",
        )

    @app.delete("/api/chat/messages", dependencies=[Depends(_require_token_or_api_key)])
    async def api_chat_messages_delete(instance_id: int = Body(...), chat_id: Optional[str] = Body(None), platform: Optional[str] = Body(None)):
        if chat_id is None:
            count = db.delete_instance_messages(instance_id)
        else:
            count = db.delete_chat_messages(instance_id, chat_id, platform=platform)
        return {"ok": True, "deleted": count}

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
        cmd_ctx = bot_commands.CmdContext(
            instance_id=int(instance_id), instance_name=instance["name"], user_id=chat_id,
            chat_id=chat_id, actor=f"{source}:{chat_id}", session=session,
        )
        try:
            cmd_reply = await bot_commands.dispatch_command(text, cmd_ctx)
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
                filename, total_size, mime, MAX_ATTACHMENT_BYTES,
                instance_id=int(instance_id), chat_id=str(chat_id), text=text,
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

    # -------------------------------------------------------- server chat --
    # A permanent, bot-independent messaging/file channel between the
    # devices themselves — the desktop app and every paired Android phone —
    # separate from the platform-facing /api/chat/* routes above (those go
    # through a bot_instance and an external platform SDK; this never
    # does). One shared "Server Chat" group room plus a private 1:1 with
    # every other device, auto-opened the moment a device is paired — see
    # bot/db.py's server_chat_conversations comment and
    # create_conversations_for_new_device().

    @app.get("/api/server-chat/whoami")
    async def api_server_chat_whoami(device_id: int = Depends(_require_device_id)):
        return {"device_id": device_id}

    @app.get("/api/server-chat/conversations")
    async def api_server_chat_conversations(device_id: int = Depends(_require_device_id)):
        return db.list_server_chat_conversations(device_id)

    @app.get("/api/server-chat/messages")
    async def api_server_chat_messages(
        conversation_id: int,
        after_id: int = 0,
        limit: int = 100,
        device_id: int = Depends(_require_device_id),
    ):
        if not db.is_conversation_participant(conversation_id, device_id):
            raise HTTPException(status_code=404, detail="no such conversation")
        return [dict(r) for r in db.list_server_chat_messages(conversation_id, after_id=after_id, limit=limit)]

    @app.get("/api/server-chat/conversations/{conversation_id}/export")
    async def api_server_chat_export(conversation_id: int, device_id: int = Depends(_require_device_id)):
        if not db.is_conversation_participant(conversation_id, device_id):
            raise HTTPException(status_code=404, detail="no such conversation")
        data = db.export_server_chat_data(conversation_id)
        return _json_download(
            {"conversation_id": conversation_id, "messages": data},
            f"server-chat-{conversation_id}-{_ts_stamp()}.json",
        )

    @app.delete("/api/server-chat/conversations/{conversation_id}")
    async def api_server_chat_clear(conversation_id: int, device_id: int = Depends(_require_device_id)):
        if not db.is_conversation_participant(conversation_id, device_id):
            raise HTTPException(status_code=404, detail="no such conversation")
        count = db.clear_server_chat_messages(conversation_id)
        return {"ok": True, "deleted": count}

    @app.post("/api/server-chat/send")
    async def api_server_chat_send(payload: dict = Body(...), device_id: int = Depends(_require_device_id)):
        conversation_id = payload.get("conversation_id")
        text = (payload.get("text") or "").strip()
        if not isinstance(conversation_id, int) or not text:
            raise HTTPException(status_code=400, detail="payload must be {conversation_id: <int>, text: <str>}")
        if not db.is_conversation_participant(conversation_id, device_id):
            raise HTTPException(status_code=404, detail="no such conversation")
        msg_id = db.create_server_chat_message(conversation_id, device_id, text)
        return {"ok": True, "id": msg_id}

    @app.post("/api/server-chat/send-file")
    async def api_server_chat_send_file(
        conversation_id: int = Form(...),
        text: str = Form(""),
        file: UploadFile = File(...),
        device_id: int = Depends(_require_device_id),
    ):
        if not db.is_conversation_participant(conversation_id, device_id):
            raise HTTPException(status_code=404, detail="no such conversation")
        try:
            rel_path, orig_name = await attachments.safe_store_stream(file.filename, file, MAX_ATTACHMENT_BYTES)
        except ValueError as exc:
            raise HTTPException(status_code=413, detail=str(exc))
        mime = file.content_type or mimetypes.guess_type(orig_name)[0]
        size = (attachments.ATTACHMENTS_DIR / rel_path).stat().st_size
        thumb_path = await asyncio.get_running_loop().run_in_executor(
            None, thumbnails.generate_thumbnail, attachments.ATTACHMENTS_DIR / rel_path, mime, attachments.THUMBS_DIR
        )
        msg_id = db.create_server_chat_message(
            conversation_id, device_id, (text or "").strip(),
            attachment_path=rel_path, attachment_name=orig_name, attachment_mime=mime,
            attachment_size=size, thumbnail_path=thumb_path.name if thumb_path else None,
        )
        return {"ok": True, "id": msg_id}

    @app.post("/api/server-chat/uploads/init")
    async def api_server_chat_uploads_init(payload: dict = Body(...), device_id: int = Depends(_require_device_id)):
        conversation_id = payload.get("conversation_id")
        filename = payload.get("filename") or "file"
        total_size = int(payload.get("total_size") or 0)
        mime = payload.get("mime")
        text = (payload.get("text") or "").strip()
        if not isinstance(conversation_id, int):
            raise HTTPException(status_code=400, detail="payload must include conversation_id")
        if not db.is_conversation_participant(conversation_id, device_id):
            raise HTTPException(status_code=404, detail="no such conversation")
        try:
            session = attachments.create_upload_session(
                filename, total_size, mime, MAX_ATTACHMENT_BYTES,
                conversation_id=conversation_id, sender_device_id=device_id, text=text,
            )
        except ValueError as exc:
            raise HTTPException(status_code=413, detail=str(exc))
        return session

    @app.put("/api/server-chat/uploads/{session_id}/chunk/{index}")
    async def api_server_chat_uploads_chunk(session_id: str, index: int, request: Request, device_id: int = Depends(_require_device_id)):
        try:
            await attachments.write_chunk(session_id, index, request)
        except KeyError:
            raise HTTPException(status_code=404, detail="unknown or expired upload session")
        return {"ok": True}

    @app.post("/api/server-chat/uploads/{session_id}/complete")
    async def api_server_chat_uploads_complete(session_id: str, device_id: int = Depends(_require_device_id)):
        try:
            assembled = await asyncio.get_running_loop().run_in_executor(
                None, attachments.assemble_upload, session_id
            )
        except KeyError:
            raise HTTPException(status_code=404, detail="unknown or expired upload session")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        conversation_id = assembled["conversation_id"]
        rel_path = assembled["rel_path"]
        display_name = assembled["display_name"]
        mime = assembled["mime"]
        size = assembled["size"]
        text = assembled["text"]
        thumb_path = await asyncio.get_running_loop().run_in_executor(
            None, thumbnails.generate_thumbnail, attachments.ATTACHMENTS_DIR / rel_path, mime, attachments.THUMBS_DIR
        )
        msg_id = db.create_server_chat_message(
            conversation_id, assembled["sender_device_id"], text,
            attachment_path=rel_path, attachment_name=display_name, attachment_mime=mime,
            attachment_size=size, thumbnail_path=thumb_path.name if thumb_path else None,
        )
        return {"ok": True, "id": msg_id}

    @app.get("/api/server-chat/attachments/{message_id}")
    async def api_server_chat_attachment(message_id: int, device_id: int = Depends(_require_device_id)):
        row = db.get_server_chat_message(message_id)
        if row is None or not row["attachment_path"] or not db.is_conversation_participant(row["conversation_id"], device_id):
            raise HTTPException(status_code=404, detail="no such attachment")
        full_path = attachments.ATTACHMENTS_DIR / row["attachment_path"]
        if not full_path.resolve().is_relative_to(attachments.ATTACHMENTS_DIR.resolve()) or not full_path.is_file():
            raise HTTPException(status_code=404, detail="attachment file missing")
        return FileResponse(full_path, media_type=row["attachment_mime"] or "application/octet-stream", filename=row["attachment_name"] or full_path.name)

    @app.get("/api/server-chat/attachments/{message_id}/thumbnail")
    async def api_server_chat_attachment_thumbnail(message_id: int, device_id: int = Depends(_require_device_id)):
        row = db.get_server_chat_message(message_id)
        if row is None or not row["thumbnail_path"] or not db.is_conversation_participant(row["conversation_id"], device_id):
            raise HTTPException(status_code=404, detail="no such thumbnail")
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

    @app.get("/api/sessions/{session_id}/export", dependencies=[Depends(_require_token_or_api_key)])
    async def api_session_export(session_id: str):
        if session_id.startswith("legacy-"):
            data = db.export_legacy_data(int(session_id.removeprefix("legacy-")))
        else:
            data = db.export_session_data(int(session_id))
            if data is None:
                raise HTTPException(status_code=404, detail=f"session {session_id} not found")
        return _json_download(data, f"session-{session_id}-{_ts_stamp()}.json")

    @app.get("/api/sessions/export", dependencies=[Depends(_require_token_or_api_key)])
    async def api_sessions_export_all(instance_id: Optional[int] = None):
        # One combined file rather than one download per session — every
        # real session plus each affected bot's legacy ("Before sessions")
        # bucket, matching exactly what GET /api/sessions itself lists.
        rows = db.list_sessions(instance_id=instance_id, limit=1_000_000)
        bundle = [db.export_session_data(row["id"]) for row in rows]
        instance_ids = [instance_id] if instance_id is not None else [inst["id"] for inst in bot_instances.list_instances()]
        for iid in instance_ids:
            if db.count_legacy_items(iid):
                bundle.append(db.export_legacy_data(iid))
        suffix = f"-bot{instance_id}" if instance_id is not None else ""
        return _json_download({"sessions": bundle}, f"sessions-backup{suffix}-{_ts_stamp()}.json")

    @app.delete("/api/sessions/{session_id}", dependencies=[Depends(_require_token_or_api_key)])
    async def api_session_delete(session_id: str):
        if session_id.startswith("legacy-"):
            count = db.clear_legacy_items(int(session_id.removeprefix("legacy-")))
            return {"ok": True, "deleted_messages": count}
        ok = db.delete_session(int(session_id))
        if not ok:
            raise HTTPException(status_code=404, detail=f"session {session_id} not found")
        return {"ok": True}

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
        db.create_conversations_for_new_device(key_id)
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
        return [dict(r) for r in db.list_api_keys(kind="device")]

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

    # ------------------------------------------------------------ peers ----
    # Linking this BotServer installation to another one (see bot/peers.py)
    # so an admin running several boxes (a home PC, a laptop, a VPS) can
    # see and manage every one of them from any single dashboard.
    #
    # Two different tokens for two different jobs (see bot/peers.py's
    # module docstring for the full rationale): generating a pairing token
    # and link/unlink all require the strict local DASHBOARD_TOKEN, same
    # bar as every other trust-establishing action in this file — but
    # /api/peers/handshake deliberately does NOT, since its entire job is
    # to be the one endpoint a *different* server's admin calls into. Its
    # auth is the short-lived, single-use pairing token in the payload
    # itself, checked first thing inside peers.accept_handshake(). Reading
    # the list and proxying a peer's own overview/bots/actions stay on
    # _require_token_or_api_key, matching every other bot-management route
    # a paired device can already reach.

    @app.get("/api/peers/self-address", dependencies=[Depends(_require_token)])
    async def api_peers_self_address():
        from bot import peers

        return {"base_url": peers.detect_own_base_url()}

    @app.get("/api/peers/firewall-status", dependencies=[Depends(_require_token)])
    async def api_peers_firewall_status():
        from bot import firewall

        port = int(os.environ.get("DASHBOARD_PORT", "8787"))
        return firewall.status(port)

    @app.post("/api/peers/firewall-open", dependencies=[Depends(_require_token)])
    async def api_peers_firewall_open():
        from bot import firewall

        port = int(os.environ.get("DASHBOARD_PORT", "8787"))
        ok, message = firewall.open_inbound_port(port)
        if ok:
            db.log_audit(actor="dashboard", action="firewall_rule_added", detail=message)
        return {"ok": ok, "message": message}

    @app.post("/api/peers/pairing-token", dependencies=[Depends(_require_token)])
    async def api_peers_pairing_token(payload: dict = Body(default={})):
        from bot import peers

        base_url = (payload.get("base_url") or "").strip() or None
        try:
            result = peers.generate_pairing_token(base_url)
        except peers.PeerError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        db.log_audit(actor="dashboard", action="peer_pairing_token_generated", detail="generated a server pairing token")
        return result

    @app.post("/api/peers/link", dependencies=[Depends(_require_token)])
    async def api_peers_link(payload: dict = Body(...)):
        from bot import peers

        name = (payload.get("name") or "").strip()
        pairing_token = payload.get("pairing_token") or ""
        my_base_url = (payload.get("my_base_url") or "").strip() or None
        if not name or not pairing_token:
            raise HTTPException(status_code=400, detail="payload must be {name, pairing_token, my_base_url?}")
        my_name = os.environ.get("BOTSERVER_NAME") or socket.gethostname()
        try:
            peer = await peers.link_peer(name, pairing_token, my_name, my_base_url)
        except peers.PeerError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        db.log_audit(actor="dashboard", action="peer_link", detail=f"linked peer {peer['id']} ({peer['name']!r})")
        return {"ok": True, "peer": _peer_public(peer)}

    @app.post("/api/peers/handshake")
    async def api_peers_handshake(payload: dict = Body(...)):
        from bot import peers

        name = (payload.get("name") or "").strip()
        api_key = payload.get("api_key") or ""
        base_url = payload.get("base_url")
        pairing_token = payload.get("pairing_token") or ""
        my_name = os.environ.get("BOTSERVER_NAME") or socket.gethostname()
        try:
            result = peers.accept_handshake(name, api_key, base_url, my_name, pairing_token)
        except peers.PeerError as exc:
            raise HTTPException(status_code=401, detail=str(exc))
        db.log_audit(actor="dashboard", action="peer_handshake", detail=f"accepted handshake from {name!r}")
        return result

    @app.get("/api/peers", dependencies=[Depends(_require_token_or_api_key)])
    async def api_peers_list():
        return [_peer_public(dict(r)) for r in db.list_peer_servers()]

    @app.delete("/api/peers/{peer_id}", dependencies=[Depends(_require_token)])
    async def api_peers_unlink(peer_id: int):
        from bot import peers

        row = peers.unlink_peer(peer_id)
        if row is None:
            raise HTTPException(status_code=404, detail="no such linked server")
        db.log_audit(actor="dashboard", action="peer_unlink", detail=f"unlinked peer {peer_id} ({row['name']!r})")
        return {"ok": True}

    @app.get("/api/peers/{peer_id}/overview", dependencies=[Depends(_require_token_or_api_key)])
    async def api_peers_overview(peer_id: int):
        from bot import peers

        row = db.get_peer_server(peer_id)
        if row is None:
            raise HTTPException(status_code=404, detail="no such linked server")
        try:
            return await peers.fetch_overview(row)
        except peers.PeerError as exc:
            raise HTTPException(status_code=502, detail=str(exc))

    @app.get("/api/peers/{peer_id}/bots", dependencies=[Depends(_require_token_or_api_key)])
    async def api_peers_bots(peer_id: int):
        from bot import peers

        row = db.get_peer_server(peer_id)
        if row is None:
            raise HTTPException(status_code=404, detail="no such linked server")
        try:
            return await peers.fetch_bots(row)
        except peers.PeerError as exc:
            raise HTTPException(status_code=502, detail=str(exc))

    @app.post("/api/peers/{peer_id}/bots/{instance_id}/{action}", dependencies=[Depends(_require_token_or_api_key)])
    async def api_peers_bot_action(peer_id: int, instance_id: int, action: str):
        from bot import peers

        row = db.get_peer_server(peer_id)
        if row is None:
            raise HTTPException(status_code=404, detail="no such linked server")
        try:
            result = await peers.run_bot_action(row, instance_id, action)
        except peers.PeerError as exc:
            raise HTTPException(status_code=502, detail=str(exc))
        db.log_audit(actor="dashboard", action="peer_bot_action", detail=f"{action} on peer {peer_id}'s bot {instance_id}")
        return result

    # ------------------------------------------------------- android apk ---
    # Sends the last APK built on this server to one or every paired device.
    # Callable from the desktop dashboard OR from any already-paired phone
    # (Devices screen's own Send / Send to all devices buttons) — either way
    # it's the same server-mediated queue, not a direct device-to-device
    # transfer. Pull-based, deliberately: there's no reliable way to wake a
    # backgrounded phone without FCM (optional, often unconfigured), so
    # "send" just queues an apk_pushes row and the phone picks it up on its
    # own next /api/android/apk/pending poll — see bot/db.py's apk_pushes
    # table comment.

    def _latest_apk_path() -> Path:
        return envfile.PROJECT_ROOT / "android-app" / "app" / "build" / "outputs" / "apk" / "debug" / "app-debug.apk"

    def _apk_version_label(path: Path) -> str:
        return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).isoformat()

    @app.get("/api/android/apk/status", dependencies=[Depends(_require_token)])
    async def api_android_apk_status():
        path = _latest_apk_path()
        if not path.is_file():
            return {"available": False}
        stat = path.stat()
        return {
            "available": True,
            "built_at": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
            "size_bytes": stat.st_size,
        }

    @app.post("/api/android/apk/send")
    async def api_android_apk_send(payload: dict = Body(...), caller_device_id: Optional[int] = Depends(_caller_device_id)):
        api_key_id = payload.get("api_key_id")
        if not isinstance(api_key_id, int):
            raise HTTPException(status_code=400, detail="payload must be {api_key_id: <int>}")
        mesh = bool(payload.get("mesh"))
        if mesh:
            # The caller itself is the source — its own installed APK,
            # served directly to the target over the LAN by its own mesh
            # listener. This server never touches the bytes; it only mints
            # the one-time token the target will present to that listener.
            if caller_device_id is None:
                raise HTTPException(status_code=400, detail="mesh sends must come from a paired device, not the desktop dashboard")
            token = secrets.token_urlsafe(24)
            push_id = db.create_apk_push(
                api_key_id, "", version_label="mesh", origin_api_key_id=caller_device_id, mesh_token=token,
            )
            db.log_audit(actor="dashboard", action="apk_send_mesh", detail=f"queued mesh apk push {push_id} from device {caller_device_id} to {api_key_id}")
            return {"ok": True, "push_id": push_id}
        path = _latest_apk_path()
        if not path.is_file():
            raise HTTPException(status_code=400, detail="no built APK found — build one first")
        push_id = db.create_apk_push(api_key_id, str(path), version_label=_apk_version_label(path))
        db.log_audit(actor="dashboard", action="apk_send", detail=f"queued apk push {push_id} for device {api_key_id}")
        return {"ok": True, "push_id": push_id}

    @app.post("/api/android/apk/send-all")
    async def api_android_apk_send_all(payload: Optional[dict] = Body(default=None), caller_device_id: Optional[int] = Depends(_caller_device_id)):
        mesh = bool((payload or {}).get("mesh"))
        keys = [r for r in db.list_api_keys(kind="device") if not r["revoked_at"]]
        if mesh:
            if caller_device_id is None:
                raise HTTPException(status_code=400, detail="mesh sends must come from a paired device, not the desktop dashboard")
            push_ids = []
            for r in keys:
                if r["id"] == caller_device_id:
                    continue  # don't queue a push to yourself
                token = secrets.token_urlsafe(24)
                push_ids.append(db.create_apk_push(
                    r["id"], "", version_label="mesh", origin_api_key_id=caller_device_id, mesh_token=token,
                ))
            db.log_audit(actor="dashboard", action="apk_send_all_mesh", detail=f"queued mesh apk push from device {caller_device_id} for {len(push_ids)} device(s)")
            return {"ok": True, "sent_to": len(push_ids)}
        path = _latest_apk_path()
        if not path.is_file():
            raise HTTPException(status_code=400, detail="no built APK found — build one first")
        version_label = _apk_version_label(path)
        push_ids = [db.create_apk_push(r["id"], str(path), version_label=version_label) for r in keys]
        db.log_audit(actor="dashboard", action="apk_send_all", detail=f"queued apk push for {len(push_ids)} device(s)")
        return {"ok": True, "sent_to": len(push_ids)}

    @app.post("/api/android/apk/mesh/redeem", dependencies=[Depends(_require_token_or_api_key)])
    async def api_android_apk_mesh_redeem(payload: dict = Body(...), caller_device_id: Optional[int] = Depends(_caller_device_id)):
        """Called by the *origin* device's own mesh listener (not the
        target) right after it accepts an incoming socket connection and
        reads the token the target presented — this confirms with the
        server that the token is real, matches this exact push, was minted
        for this device to hand out, and hasn't already been spent, before
        the origin streams a single byte of its APK to whoever's asking."""
        push_id = payload.get("push_id")
        token = payload.get("token")
        if not isinstance(push_id, int) or not isinstance(token, str):
            raise HTTPException(status_code=400, detail="payload must be {push_id: <int>, token: <str>}")
        row = db.get_apk_push(push_id)
        if row is None or row["origin_api_key_id"] != caller_device_id:
            raise HTTPException(status_code=404, detail="no such mesh push originating from this device")
        return {"ok": db.redeem_mesh_token(push_id, token)}

    @app.get("/api/turn/credentials", dependencies=[Depends(_require_token_or_api_key)])
    async def api_turn_credentials(caller_device_id: Optional[int] = Depends(_caller_device_id)):
        """Short-lived TURN relay credentials for the WebRTC mesh fallback
        (see bot/turn.py) — minted fresh per call, never stored, so there's
        nothing here to revoke beyond letting the ttl expire. Returns
        {"enabled": false} rather than 404/403 when TURN isn't configured,
        since "no TURN available" is an expected, non-error state the
        client falls back to STUN-only for."""
        from bot import turn

        label = str(caller_device_id) if caller_device_id is not None else "desktop"
        creds = turn.credentials(user_label=label)
        if creds is None:
            return {"enabled": False}
        return {"enabled": True, **creds}

    @app.get("/api/android/apk/pending")
    async def api_android_apk_pending(api_key_id: int = Depends(_require_mobile_key_id)):
        row = db.get_pending_apk_push(api_key_id)
        if row is None:
            return {"available": False}
        result = {
            "available": True,
            "push_id": row["id"],
            "version_label": row["version_label"],
            "created_at": row["created_at"],
        }
        origin_id = row["origin_api_key_id"]
        if origin_id:
            presence = db.get_device_presence(origin_id)
            mesh: dict = {"origin_api_key_id": origin_id, "token": row["mesh_token"]}
            if presence and presence["local_ip"] and presence["mesh_port"]:
                # Handed to this device only — it's the sole party the
                # server ever tells about another device's LAN address, and
                # only for the one push actually addressed to it.
                mesh["host"] = presence["local_ip"]
                mesh["port"] = presence["mesh_port"]
            # origin_api_key_id + token are always included even without a
            # usable LAN address — they're what the WebRTC fallback needs to
            # address a signaling offer at the origin device (see
            # WebRtcMeshClient.kt) when the two devices aren't on the same
            # network for the direct-socket path above to work at all.
            result["mesh"] = mesh
        return result

    @app.get("/api/android/apk/download/{push_id}")
    async def api_android_apk_download(push_id: int, api_key_id: int = Depends(_require_mobile_key_id)):
        row = db.get_apk_push(push_id)
        if row is None or row["api_key_id"] != api_key_id:
            raise HTTPException(status_code=404, detail="no such pending push for this device")
        if row["origin_api_key_id"]:
            raise HTTPException(status_code=409, detail="this push is mesh-only — no server-side copy exists, retry the direct transfer")
        path = Path(row["apk_path"])
        if not path.is_file():
            raise HTTPException(status_code=404, detail="APK file no longer available — ask the desktop app to send again")
        db.mark_apk_push_downloaded(push_id)
        return FileResponse(path, media_type="application/vnd.android.package-archive", filename="BotServer.apk")

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
    async def ws_devices(
        websocket: WebSocket,
        token: Optional[str] = None,
        x_dashboard_token: Optional[str] = Header(default=None, alias="X-Dashboard-Token"),
    ):
        # A browser's native WebSocket API can't set a custom header on the
        # handshake the way fetch() can, so the desktop Electron client's
        # auth travels as a query param here instead of X-Dashboard-Token —
        # kept for that client. OkHttp *can* set handshake headers, so the
        # Android client sends the real header instead (via the shared
        # DynamicHostInterceptor every other request already goes through)
        # rather than putting its token in a URL, where it's more likely to
        # be logged. Either is accepted; the header wins if both are present.
        supplied = x_dashboard_token or token
        expected = os.environ.get("DASHBOARD_TOKEN")
        authed = bool(expected and supplied == expected)
        device_id = None if authed else db.verify_api_key(supplied or "")
        if not authed and device_id is None:
            await websocket.close(code=4401)
            return
        await _manager.connect(websocket)
        if device_id is not None:
            # Only a real paired device (not the desktop dashboard token) can
            # be a WebRTC signaling target/source — see send_to_device above.
            await _manager.register_device(websocket, device_id)
        try:
            devices = await asyncio.get_running_loop().run_in_executor(None, db.list_devices)
            await websocket.send_json({"type": "device_list", "devices": _annotate_online([dict(d) for d in devices])})
            while True:
                raw = await websocket.receive_text()
                if device_id is None:
                    continue  # dashboard connections don't originate signals
                try:
                    message = json.loads(raw) if raw else {}
                    if message.get("type") != "webrtc_signal":
                        continue
                    to_id = message.get("to_api_key_id")
                    if not isinstance(to_id, int):
                        continue
                    await _manager.send_to_device(
                        to_id, {"type": "webrtc_signal", "from_api_key_id": device_id, "data": message.get("data")},
                    )
                except (ValueError, TypeError, AttributeError):
                    continue  # malformed signaling message — drop it, keep the socket's device_list duty alive
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
