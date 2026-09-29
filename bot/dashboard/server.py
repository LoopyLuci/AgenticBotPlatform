"""Dashboard REST API — read endpoints back onto real SQLite data, mutating
endpoints require the X-Dashboard-Token header (set DASHBOARD_TOKEN in .env).

Bind stays on 127.0.0.1 by default (see bot/main.py) — that, plus the
token, is the security boundary. This has no session/cookie auth of its
own; don't expose it past localhost without putting a real reverse proxy
and auth in front of it.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, Header, HTTPException, Request, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from fastapi import Response
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from bot import db, envfile
from bot import tasks as bg

STATIC_DIR = Path(__file__).resolve().parent / "static"
# Serves the desktop app's own UI source fresh from disk (see
# bot/ui_customize.py's module docstring and the "never needs a rebuild"
# fix) — the compiled Tauri window navigates here once its embedded boot
# sequence confirms this server is up, so a plain file edit (or a Customize
# UI apply) takes effect on the window's next reload with no cargo tauri
# build in between. Only meaningful on a dev checkout where desktop-app/
# actually exists alongside bot/ — absent in a headless-only deployment,
# in which case this mount is simply never reachable, not an error.
DESKTOP_UI_DIR = envfile.CODE_ROOT / "desktop-app" / "ui"
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
# from a real platform or through the Agentic Bot Platform App's own real channel.
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
    function call, not a coroutine). Those writes may run on the event loop
    or on a worker thread (sync route handlers run in FastAPI's threadpool),
    so this goes through bg.spawn_soon, which reaches the main loop from
    either. Without any live loop it logs and skips — a missed live update
    is a minor gap (clients catch up on their next poll), not worth failing
    the write that triggered it over."""
    if not bg.spawn_soon(lambda: _manager.broadcast(payload), name="ws-broadcast"):
        logger.warning("no running event loop to broadcast %s — clients will catch up on their next poll", payload.get("type"))


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


def _on_job_tool_event(job_id: int) -> None:
    events = db.list_job_tool_events(job_id)
    if not events:
        return
    _broadcast_soon({"type": "job_tool_event", "job_id": job_id, "event": dict(events[-1])})


def _on_job_children_set(job_id: int) -> None:
    children = db.list_job_children(job_id)
    _broadcast_soon({"type": "job_children_update", "job_id": job_id, "children": [dict(c) for c in children]})


db.on_message_logged(_on_message_logged)
db.on_job_changed(_on_job_changed)
db.on_job_tool_event(_on_job_tool_event)
db.on_job_children_set(_on_job_children_set)


def _on_activity_entry(entry) -> None:
    # Same best-effort/degrade-to-next-poll shape as every other
    # _broadcast_soon call site — see its own docstring. Log records can
    # originate from any thread (a background job, an executor callback),
    # not just the event loop thread that's actually able to broadcast
    # live, so a missed live push here is expected and fine: the Activity
    # tab's own poll/reconnect logic still picks it up.
    _broadcast_soon({"type": "activity_entry", "entry": entry.__dict__})


from bot import activity_log as _activity_log  # noqa: E402 — after db.on_*() registration, matching this block's own convention

_activity_log.subscribe(_on_activity_entry)


def _tokens_match(provided: Optional[str], expected: str) -> bool:
    """Constant-time comparison for every secret check in this file — a
    plain ==/!= (what every one of these used until this fix) leaks
    timing information proportional to how many leading bytes match,
    since CPython's string comparison short-circuits on the first
    mismatch. Every check here gates the entire dashboard/mobile API
    surface, so this is the one place worth being careful, not a
    theoretical nitpick. hmac.compare_digest requires both arguments to
    actually be strings (or both bytes) — a missing header (None) is
    just treated as "doesn't match" rather than raising a TypeError."""
    if provided is None:
        return False
    return hmac.compare_digest(provided, expected)


def _require_token(x_dashboard_token: Optional[str] = Header(default=None)) -> None:
    expected = os.environ.get("DASHBOARD_TOKEN")
    if not expected:
        # No token configured: refuse mutating calls outright rather than
        # silently running with no auth at all.
        raise HTTPException(status_code=503, detail="DASHBOARD_TOKEN is not set in .env")
    if not _tokens_match(x_dashboard_token, expected):
        raise HTTPException(status_code=401, detail="invalid dashboard token")


def peer_infra_enabled() -> bool:
    """Whether linked servers may manage this machine's containers/VMs/Tailscale. Off unless the
    owner turned it on (Containers page, or `abp env set PEER_INFRA_ACCESS 1`)."""
    return (envfile.get_var("PEER_INFRA_ACCESS") or "").strip().lower() in ("1", "true", "yes", "on")


def infra_token_ok(supplied: Optional[str]) -> bool:
    """The desktop token always; a linked peer server's key only while peer infra access is switched on."""
    expected = os.environ.get("DASHBOARD_TOKEN")
    if expected and _tokens_match(supplied, expected):
        return True
    return bool(
        supplied and peer_infra_enabled() and db.api_key_kind(supplied) == "peer_server"
        and db.verify_api_key(supplied) is not None
    )


def _require_infra_access(x_dashboard_token: Optional[str] = Header(default=None)) -> None:
    if not os.environ.get("DASHBOARD_TOKEN"):
        raise HTTPException(status_code=503, detail="DASHBOARD_TOKEN is not set in .env")
    if not infra_token_ok(x_dashboard_token):
        raise HTTPException(status_code=401, detail="invalid dashboard token, or this server does not allow linked servers to manage it")


_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1"})
# api_keys kinds that are not paired phones: a linked server, or the browser extension (bot/browser_bridge.py).
# Neither has desktop-equivalent access; each reaches only its own narrow routes.
_NON_DEVICE_KINDS = ("peer_server", "browser_ext")


def _require_token_or_bootstrap(request: Request, x_dashboard_token: Optional[str] = Header(default=None)) -> None:
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
        # This window exists so the very first token can be set. It must
        # never be open to the network: bot/main.py now always generates a
        # token at startup, so in practice it is closed — but if it ever
        # is open (a blank value slipping through), only a caller on this
        # same machine may use it, never anyone who can merely reach the
        # port (Docker, Tailscale Funnel).
        client_host = request.client.host if request.client else ""
        if client_host not in _LOOPBACK_HOSTS:
            raise HTTPException(status_code=503, detail="DASHBOARD_TOKEN is not set; configure it from this machine")
        return
    if not _tokens_match(x_dashboard_token, expected):
        raise HTTPException(status_code=401, detail="invalid dashboard token")


_LOCAL_PAGE_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
_FORWARDING_HEADERS = ("x-forwarded-for", "x-forwarded-host", "x-forwarded-proto", "forwarded", "x-real-ip")


def _is_local_page_request(request: Request) -> bool:
    """True only for a plain page load by a browser or webview on THIS machine.

    The dashboard token is never typed in: the page is handed it when it is loaded from here. So this has to be
    strict. The client must be loopback and the Host header must name loopback (a name that resolves to it, a
    Funnel or a reverse proxy in front does not count). Nothing may say the request was forwarded. And it must
    not be a script fetching the page from another web app: those carry an Origin header or a cors / cross-site
    Sec-Fetch marker, which a page load never does."""
    client = request.client.host if request.client else ""
    if client not in _LOOPBACK_HOSTS:
        return False
    host = urlsplit("//" + (request.headers.get("host") or "")).hostname or ""
    if host not in _LOCAL_PAGE_HOSTS:
        return False
    if any(request.headers.get(h) for h in _FORWARDING_HEADERS):
        return False
    if request.headers.get("origin"):
        return False
    if request.headers.get("sec-fetch-mode") == "cors" or request.headers.get("sec-fetch-site") == "cross-site":
        return False
    return True


def _page_with_token(path: Path, request: Request) -> HTMLResponse:
    """An HTML page of the dashboard, with the auto-generated DASHBOARD_TOKEN placed in it for a local page load
    (see _is_local_page_request). A page that carries the token is never stored anywhere; any other keeps the
    revalidate-every-time caching the dashboard has always had."""
    html = path.read_text(encoding="utf-8")
    token = os.environ.get("DASHBOARD_TOKEN")
    cache = "no-cache"
    # The pages hold no inline script of their own (it all lives in /static/*.js), so the policy can forbid it:
    # an injected <script> or on*= attribute never runs, and with the token reachable from script that is the
    # difference between an escaping bug and a full takeover. The one inline script that must exist, the token
    # below, carries this response's nonce.
    nonce = secrets.token_urlsafe(18)
    if token and _is_local_page_request(request):
        literal = json.dumps(token).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
        snippet = f'<script nonce="{nonce}">window.__ABP_TOKEN__={literal};</script>'
        html = html.replace("</head>", snippet + "</head>", 1)
        cache = "no-store"
    return HTMLResponse(html, headers={"Cache-Control": cache, "Content-Security-Policy": page_csp(nonce)})


def page_csp(nonce: str) -> str:
    """The dashboard pages' policy. Styles keep 'unsafe-inline' (hundreds of style attributes; CSS injection
    can't run code); script, objects, framing and form targets are locked down."""
    return ("default-src 'self'; "
            f"script-src 'self' 'nonce-{nonce}'; "
            "style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data: blob:; "
            "font-src 'self' data:; "
            "connect-src 'self' ws: wss:; "
            "media-src 'self' blob: data:; "
            "worker-src 'self' blob:; "
            "frame-src 'self'; "
            "object-src 'none'; base-uri 'self'; form-action 'self'; frame-ancestors 'self'")


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
    if expected and _tokens_match(x_dashboard_token, expected):
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
        # A linked peer server (bot/peers.py) authenticates with a key of
        # kind "peer_server", minted by the same api_keys table a paired
        # phone's key lives in — verify_api_key() itself doesn't (and
        # shouldn't) care which, but callers of _identify_caller do: a peer
        # gets a deliberately narrower surface (see
        # _require_token_or_api_key below) than a full mobile device, which
        # has desktop-equivalent access by design.
        kind = db.api_key_kind(x_dashboard_token or "")
        if kind == "peer_server":
            return "peer"
        if kind == "browser_ext":
            return "bridge"
        return "mobile"
    if not expected:
        raise HTTPException(status_code=503, detail="DASHBOARD_TOKEN is not set in .env")
    raise HTTPException(status_code=401, detail="invalid dashboard token or api key")


def _require_token_or_api_key(caller: str = Depends(_identify_caller)) -> None:
    """The default tier for most routes: desktop dashboard or a paired
    mobile device, but NOT a linked peer server — a peer's own key is meant
    for the narrow monitoring/lifecycle surface bot/peers.py actually calls
    (see _require_token_or_api_key_or_peer), never full remote
    administration of this instance (config, providers, credentials, hooks,
    security settings, ...). Routes a peer legitimately needs use that
    other dependency explicitly instead of this one."""
    if caller == "peer":
        raise HTTPException(status_code=403, detail="a linked peer server cannot call this endpoint")
    if caller == "bridge":
        raise HTTPException(status_code=403, detail="a browser-extension key cannot call this endpoint")
    return None


def control_auth(area: str, *, write: bool):
    """Auth for a module's routes (vm-harness, hermes-manager, power): the desktop dashboard token; a paired phone for
    reads; and a linked peer server only when this machine's owner allowed that area (peers.remote_control in
    config/backends.yaml, off by default)."""
    def dependency(caller: str = Depends(_identify_caller)) -> None:
        if caller == "dashboard":
            return None
        if caller == "peer":
            from bot import peers
            if area in peers.allowed_control_areas():
                from bot import power
                power.keeper.note_activity(f"a linked server using {area}")
                return None
            raise HTTPException(status_code=403, detail=f"this server does not let linked servers control {area} "
                                                        f"(its owner can allow it: peers.remote_control)")
        if caller == "mobile" and not write:
            return None
        raise HTTPException(status_code=403, detail="this endpoint needs the desktop dashboard token")
    return dependency


def _require_token_or_api_key_or_peer(caller: str = Depends(_identify_caller)) -> None:
    """Like _require_token_or_api_key, but also allows a linked peer
    server's own key — for the small set of routes bot/peers.py's proxy
    actually calls (overview, bot list/show, bot lifecycle actions)."""
    if caller == "bridge":
        raise HTTPException(status_code=403, detail="a browser-extension key cannot call this endpoint")
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
    if expected and _tokens_match(x_dashboard_token, expected):
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
    # A linked peer server's key also lives in api_keys and would otherwise
    # pass verify_api_key() same as a real paired phone — but these routes
    # are all Android/mesh device-management, which a peer server has no
    # business calling (see _identify_caller's own "peer" carve-out).
    if key_id is not None and db.api_key_kind(x_dashboard_token or "") in _NON_DEVICE_KINDS:
        key_id = None
    if key_id is None:
        raise HTTPException(status_code=401, detail="invalid dashboard token or api key")
    return key_id


def _require_tier(minimum: str):
    """Dependency factory: the desktop DASHBOARD_TOKEN always passes; a
    paired device's key must be at permission tier `minimum` or higher.
    Guards the few REST routes that are effectively "run a command as the
    server user" (agent hooks), which the flat "any paired device = desktop
    parity" model would otherwise hand to a phone at tier `none` — the
    `unrestricted` tier already means "may run_shell without an approval
    prompt", so it is the honest bar for creating one."""
    from bot import device_tiers

    def _dep(device_id: Optional[int] = Depends(_caller_device_id)) -> None:
        if device_id is None:
            return
        row = db.get_api_key(device_id)
        tier = row["permission_tier"] if row else "none"
        if device_tiers.TIER_RANK.get(tier, 0) < device_tiers.TIER_RANK[minimum]:
            raise HTTPException(
                status_code=403,
                detail=f"this route needs permission tier {minimum!r} or higher (this device is {tier!r})",
            )

    return _dep


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
    if expected and _tokens_match(x_dashboard_token, expected):
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
    if expected and _tokens_match(x_dashboard_token, expected):
        return db.SERVER_CHAT_DESKTOP_DEVICE_ID
    key_id = db.verify_api_key(x_dashboard_token or "")
    if key_id is not None and db.api_key_kind(x_dashboard_token or "") in _NON_DEVICE_KINDS:
        key_id = None
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
    if key_id is not None and db.api_key_kind(x_dashboard_token or "") in _NON_DEVICE_KINDS:
        key_id = None
    if key_id is None:
        raise HTTPException(status_code=401, detail="a valid mobile api key is required")
    return key_id


_SUPPORT_BOT_TRAINING_OPS_SKILL = """Support Bot intent-classifier training operations.

Use the support_bot_* MCP tools for all of this — never hand-edit
bot/support_bot/training_data.py for routine data growth; that file is
the hand-authored BASELINE, not where ongoing training happens.

- support_bot_generate_training_data(module_id=None, target_per_intent=20)
  — runs the free-model-only synthetic swarm until every targeted intent
  reaches target_per_intent examples. Pass module_id to scope one
  Knowledge Module at a time (see support_bot_list_knowledge_modules for
  the list) instead of the whole system.
- support_bot_list_pending_examples(status="pending", module_id=None) /
  support_bot_review_pending_example(pending_id, decision) — the human
  review queue. decision is approve/reject/revert.
- Auto-approve rule: a (phrase, intent) pair 2+ independent free models
  produce with near-identical wording in the SAME generation batch is
  auto-approved straight into the live training set (still visible in
  the pending list, filtered to status=approved, with an audit trail —
  never silent). Anything less certain lands as a normal pending item
  for a human to review.
- support_bot_list_knowledge_modules() / support_bot_set_module_enabled()
  / support_bot_retrain_module() — Knowledge Modules are independently
  trainable/toggleable groups of intents (e.g. "bots", "mcp", "backups").
  Disabling a module never deletes its trained model; re-enabling is
  instant.
"""


def _seed_support_bot_training_ops_skill() -> None:
    """Idempotent — registers the support-bot-training-ops runtime skill
    exactly once (checks for it by name first), never overwrites an
    operator's own edits to it on a later run. Wrapped defensively: a
    DB hiccup here must never prevent the dashboard app from building."""
    try:
        if db.get_skill(None, "support-bot-training-ops") is not None:
            return
        from bot import skills as skills_module

        skills_module.create(
            None, "support-bot-training-ops",
            "Support Bot intent-classifier training operations — generate training data, "
            "review pending examples, and manage Knowledge Modules via the support_bot_* MCP tools.",
            _SUPPORT_BOT_TRAINING_OPS_SKILL, global_=True,
        )
    except Exception:
        pass


class _NoCacheStaticFiles(StaticFiles):
    """Plain StaticFiles sends no Cache-Control header at all, which makes
    a browser (or a WebView2-embedded one, i.e. the desktop app) apply its
    own heuristic freshness lifetime from Last-Modified — often long
    enough that a real page reload keeps serving JS/HTML from BEFORE an
    app update even though the file on disk changed, with no error and no
    visible sign anything is stale. Confirmed live: after rebuilding with
    a real fix, the actual browser-triggered <script> load kept returning
    the pre-fix bytes (same request that a plain fetch()/curl — bypassing
    whatever heuristic cache the navigation path used — correctly saw as
    fresh), while ETag/Last-Modified were already present and correct.
    `no-cache` (not `no-store`) still lets the ETag-based 304 flow work —
    a client that already has the current version costs a small cheap
    revalidation request, not a full re-download — but a client's own
    cache duration decision is REMOVED as a source of ever seeing stale
    JS/HTML after an update."""

    def file_response(self, *args, **kwargs) -> Response:
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-cache"
        return response


def build_app() -> FastAPI:
    _seed_support_bot_training_ops_skill()

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _lifespan(app: FastAPI):
        # Sync route handlers run in FastAPI's threadpool; bg.spawn_soon needs
        # this loop to hand their background work (broadcasts, pushes) back to.
        bg.bind_loop(asyncio.get_running_loop())
        task = asyncio.create_task(_presence_broadcaster())
        # Keep-awake: the thread that holds (or releases) the OS's "don't sleep" request per power.keep_awake.
        from bot import power
        power.keeper.start()
        ssh_update_task = asyncio.create_task(_ssh_toolkit_auto_update_loop())
        try:
            yield
        finally:
            task.cancel()
            ssh_update_task.cancel()
            power.keeper.stop()

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

    @app.middleware("http")
    async def _security_headers(request: Request, call_next):
        """Baseline hardening headers on every response. Deliberately NOT a
        script-src policy: the dashboard is one large page of inline
        script/handlers, so a script-src that blocks inline would break it
        outright, and a policy that allows it ('unsafe-inline') would only
        look protective. What these DO buy: no MIME sniffing of an uploaded
        or user-influenced response into HTML/JS, no framing by another
        site (clickjacking), no <base>/<object> injection, and no Referer
        leaking the URL (which can carry a WebSocket token) to other hosts.
        The real XSS defence is escaping at each sink."""
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault(
            "Content-Security-Policy", "object-src 'none'; base-uri 'self'; frame-ancestors 'self'",
        )
        return response

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
                # This loop runs for the whole life of the process — a
                # silently-swallowed exception here doesn't just miss one
                # broadcast, it degrades the Devices view's "live" feel
                # indefinitely with nothing in the logs to explain why.
                logger.exception("presence broadcaster iteration failed")

    async def _ssh_toolkit_auto_update_loop():
        """Background check for bot.ssh_toolkit.run_auto_update_check() -
        never runs at all when the "auto_update" setting is "never" (the
        default), so an install that hasn't opted in pays no cost beyond
        this one cheap config read every cycle."""
        from bot import ssh_toolkit

        while True:
            await asyncio.sleep(6 * 3600)
            try:
                await ssh_toolkit.run_auto_update_check()
            except Exception:
                logger.exception("ssh_toolkit auto-update loop iteration failed")

    @app.get("/")
    async def index(request: Request):
        return _page_with_token(STATIC_DIR / "dashboard.html", request)

    # The desktop app's own UI, served here too once the bot is up. Registered before the static mount below so
    # these two paths get the token; every other file under /desktop-ui/ is served as it always was.
    @app.get("/desktop-ui/")
    @app.get("/desktop-ui/index.html")
    def desktop_ui_index(request: Request):
        page = DESKTOP_UI_DIR / "index.html"
        if not page.is_file():
            raise HTTPException(status_code=404, detail="desktop UI is not installed")
        return _page_with_token(page, request)

    app.mount("/static", _NoCacheStaticFiles(directory=str(STATIC_DIR)), name="static")

    if DESKTOP_UI_DIR.is_dir():
        # html=True auto-serves index.html at /desktop-ui/ and correctly
        # resolves its relative main.js/assets/* references against this
        # same mount — StaticFiles already reads fresh from disk on every
        # request server-side (no server caching layer); _NoCacheStaticFiles
        # is what stops the CLIENT (a browser's, or WebView2's, own
        # heuristic cache) from separately deciding to keep serving an
        # old version anyway — see its own docstring for how that was
        # confirmed live.
        app.mount("/desktop-ui", _NoCacheStaticFiles(directory=str(DESKTOP_UI_DIR), html=True), name="desktop-ui")

    # CI/CD control plane (/api/cicd/*) — thin wrappers over abp_cicd.service.
    from bot.dashboard import cicd_api

    cicd_api.register(app, _require_token_or_api_key)

    # Tailscale management (/api/tailscale/*) — strict desktop-token auth.
    from bot.dashboard import tailscale_api

    tailscale_api.register(app, _require_infra_access)

    # Containers (Docker, Portainer-style) and VMs (QEMU / Hyper-V / libvirt).
    from bot.dashboard import infra_api

    infra_api.register(
        app, _require_infra_access, infra_token_ok,
        desktop_token_ok=lambda supplied: bool(os.environ.get("DASHBOARD_TOKEN"))
        and _tokens_match(supplied, os.environ["DASHBOARD_TOKEN"]),
        set_peer_access=lambda on: envfile.set_var("PEER_INFRA_ACCESS", "1" if on else "0", actor="dashboard"),
        peer_access_enabled=peer_infra_enabled,
    )

    # Browser-extension bridge (/api/browser/*): pairing, the extension WebSocket, policy, RPC.
    from bot.dashboard import browser_api

    browser_api.register(app, _require_token)

    from bot.dashboard import browser_gateway_api

    browser_gateway_api.register(app, _require_token)

    # Agent security (/api/agent/permissions, /api/mcp/pins, ...): rules, pins, untrusted-content marks.
    from bot.dashboard import agent_security_api

    agent_security_api.register(app, _require_token_or_api_key, _require_token)

    # Model knowledge and allowances (/api/models/info, /usage, /find, /limits).
    from bot.dashboard import models_info_api

    models_info_api.register(app, _require_token_or_api_key, _require_token)

    # Approvals as reviewable objects (/api/approvals): a preview of what the agent wants to do, and a way to decide.
    from bot.dashboard import approvals_api

    def _caller_is_owner(device_id: Optional[int] = Depends(_caller_device_id)) -> bool:
        return device_id is None          # the dashboard token itself, not a paired device's key

    approvals_api.register(app, _require_token_or_api_key, _require_token, _caller_is_owner)

    # Channels added in P7 (SMS and iMessage webhooks), the canvas, and paired-phone nodes.
    from bot.dashboard import channels_api

    channels_api.register(app, _require_token_or_api_key, _require_token, _caller_device_id)

    # The ABP Agents page: every native-agent setting, an overview with readiness checks, and the tool inventory.
    from bot.dashboard import agent_config_api

    agent_config_api.register(app, _require_token_or_api_key, _require_token)

    # Sentinel (ADR-0011): self-preservation status, backups, CVE scan, fingerprinted errors.
    from bot.dashboard import sentinel_api

    sentinel_api.register(app, _require_token_or_api_key, _require_token)

    # The model router: its decisions and reasoning, what it learned, training, and its editable policy.
    from bot.dashboard import router_api

    router_api.register(app, _require_token_or_api_key, _require_token)

    # Unsloth Studio: load and serve its models, download, train, export, and every other operation it offers.
    from bot.dashboard import unsloth_api

    unsloth_api.register(app, _require_token_or_api_key, _require_token)

    # Ollama: pull, load and serve its models, build models from Modelfiles or GGUF files, and every route it serves.
    from bot.dashboard import ollama_api

    ollama_api.register(app, _require_token_or_api_key, _require_token)

    # VM-Harness: a separate program ABP installs, updates and drives (VMs, containers, and its own window).
    from bot.dashboard import vm_harness_api

    vm_harness_api.register(app, control_auth("vm-harness", write=False), control_auth("vm-harness", write=True))

    # Hermes Manager: a separate program ABP installs, updates and drives (Hermes's gateway, logs, config, backups...
    # and its own window).
    from bot.dashboard import hermes_manager_api

    hermes_manager_api.register(app, control_auth("hermes-manager", write=False), control_auth("hermes-manager", write=True))

    # TransferDaemon: a separate program ABP builds, updates and drives (encrypted messages and files between devices,
    # its window, terminal UI and relays).
    from bot.dashboard import transferdaemon_api

    transferdaemon_api.register(app, control_auth("transferdaemon", write=False), control_auth("transferdaemon", write=True))

    # Modules: every separate program ABP installs, updates, builds and drives, on one page (bot/modules/).
    from bot.dashboard import modules_api

    modules_api.register(app, control_auth("modules", write=False), control_auth("modules", write=True))

    # Power: keep this machine awake while it is in use, wake other machines (Wake-on-LAN).
    from bot.dashboard import power_api

    power_api.register(app, control_auth("power", write=False), control_auth("power", write=True))

    # Editor integrations: install the VS Code extension, show the ACP command.
    from bot.dashboard import editors_api

    editors_api.register(app, _require_token_or_api_key, _require_token)

    # Every other route area lives in bot/dashboard/routes/, registered in this exact order.
    from bot.dashboard import routes as _routes  # noqa: F401 — the package
    import importlib

    for _area in ('ops_endpoints', 'reads', 'files', 'setup_wizard', 'platforms', 'bots', 'schedules', 'pairing', 'kanban', 'swarms', 'agent_control', 'hermes_delegation', 'shared_context', 'delegation', 'mutations', 'chat', 'server_chat', 'mobile_keys', 'peers', 'android_apk', 'devices'):
        _mod = importlib.import_module(f"bot.dashboard.routes.{_area}")
        if "json_download" in _mod.register.__code__.co_varnames:
            _mod.register(app, json_download=_json_download)
        else:
            _mod.register(app)

    return app
