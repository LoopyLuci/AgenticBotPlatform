"""The bridge between ABP and the ABP Bridge browser extension (see docs/browser-extension/DESIGN.md).

Three jobs:
  1. PAIRING - trust is established by a short-lived code the person reads off the desktop app, or by the person
     approving a request on the desktop. The extension never receives a token typed by a human, and a key is minted
     only after an explicit desktop decision. Keys are `api_keys` rows of kind "browser_ext": they are valid ONLY on
     the bridge WebSocket (bot/dashboard/server.py's auth tiers refuse them everywhere else).
  2. CONNECTION HUB - one live WebSocket per paired browser profile, JSON-RPC 2.0 both ways (protocol v1): the server
     calls the extension (tab.snapshot, tab.act, ...) with deadlines and idempotency keys, and the extension calls the
     server (audit.push, models.report, ...). A dropped connection can resume within RESUME_WINDOW_S and any request
     the extension never answered is re-sent (the idempotency key makes that safe).
  3. POLICY the server enforces itself before asking the extension to do anything (browser_policy.py), in addition
     to the extension enforcing the same rules locally.

Nothing here trusts the extension with secrets: stored-login values are resolved by the caller and only ever sent to
`tab.act fill_credential` after the origin check; they are never logged.
"""
from __future__ import annotations

import asyncio
import hmac
import json
import logging
import secrets
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

from bot import db

logger = logging.getLogger(__name__)

PROTOCOL = 1
KEY_KIND = "browser_ext"
PAIR_CODE_TTL_S = 120
PAIR_CODE_MAX_ATTEMPTS = 5
PAIR_MIN_INTERVAL_S = 1.0
PAIR_REQUEST_TTL_S = 300
HELLO_TIMEOUT_S = 10.0
RESUME_WINDOW_S = 60.0
DEFAULT_DEADLINE_MS = 30_000
MAX_DEADLINE_MS = 600_000
MAX_IN_FLIGHT = 64
MAX_FRAME_BYTES = 1_048_576
EXT_ORIGIN_PREFIXES = ("chrome-extension://", "moz-extension://")
LOOPBACK = frozenset({"127.0.0.1", "::1", "localhost"})

# stable error codes (protocol v1)
ERRORS = {"E_AUTH", "E_PROTOCOL", "E_METHOD", "E_PARAMS", "E_TIMEOUT", "E_CANCELLED", "E_NO_TAB", "E_STALE_REF", "E_NOT_ALLOWED",
          "E_NEEDS_APPROVAL", "E_SENSITIVE_SITE", "E_PAGE_CHANGED", "E_DEBUGGER_UNAVAILABLE", "E_NOT_INTERACTABLE", "E_BLOCKED_BY_PAGE",
          "E_TOO_LARGE", "E_BUSY", "E_ADAPTER_BROKEN", "E_NOT_LOGGED_IN", "E_RATE_LIMITED", "E_MODEL_UNAVAILABLE", "E_OOM", "E_INTERNAL",
          "E_NOT_CONNECTED", "E_INTEGRITY"}


class BridgeError(Exception):
    def __init__(self, code: str, message: str = "", *, retryable: bool = False, data: Optional[dict] = None, hint: str = ""):
        super().__init__(f"{code}: {message}" if message else code)
        self.code = code
        self.message = message
        self.retryable = retryable
        self.data = data or {}
        self.hint = hint

    def to_error(self) -> dict:
        return {"code": self.code, "message": self.message, "data": self.data, "retryable": self.retryable, "hint": self.hint}


# ------------------------------------------------------------------------------------------ storage
def _conn():
    conn = db.get_conn()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS browser_pairings (
            key_id INTEGER PRIMARY KEY,
            extension_id TEXT NOT NULL,
            origin TEXT NOT NULL,
            browser TEXT NOT NULL DEFAULT '',
            version TEXT NOT NULL DEFAULT '',
            paired_at REAL NOT NULL,
            last_connected_at REAL
        );
        CREATE TABLE IF NOT EXISTS browser_bridge_meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
    """)
    return conn


def server_id() -> str:
    """A stable random id for this ABP install. The extension pins it after pairing (trust on first use) so a different
    server that later appears on the same port is refused."""
    conn = _conn()
    row = conn.execute("SELECT value FROM browser_bridge_meta WHERE key='server_id'").fetchone()
    if row:
        return row["value"]
    sid = uuid.uuid4().hex
    conn.execute("INSERT OR IGNORE INTO browser_bridge_meta (key, value) VALUES ('server_id', ?)", (sid,))
    conn.commit()
    return conn.execute("SELECT value FROM browser_bridge_meta WHERE key='server_id'").fetchone()["value"]


def origin_ok(origin: Optional[str]) -> Optional[str]:
    """The extension id from a valid extension Origin header, else None."""
    if not origin:
        return None
    for prefix in EXT_ORIGIN_PREFIXES:
        if origin.startswith(prefix):
            ext_id = origin[len(prefix):].strip("/")
            if ext_id and "/" not in ext_id and len(ext_id) <= 64:
                return ext_id
    return None


def is_loopback(host: Optional[str]) -> bool:
    return (host or "") in LOOPBACK


# ------------------------------------------------------------------------------------------ pairing
@dataclass
class _Code:
    code: str
    expires: float
    attempts_left: int = PAIR_CODE_MAX_ATTEMPTS
    label: str = ""


@dataclass
class _Request:
    id: str
    nonce: str
    extension_id: str
    origin: str
    browser: str
    version: str
    created: float
    state: str = "pending"            # pending | approved | denied | expired | collected
    key: Optional[str] = None


class Pairing:
    def __init__(self) -> None:
        self._code: Optional[_Code] = None
        self._requests: dict[str, _Request] = {}
        self._last_attempt = 0.0

    # ---- code flow
    def start_code(self, label: str = "") -> dict:
        code = f"{secrets.randbelow(1_000_000):06d}"
        self._code = _Code(code=code, expires=time.time() + PAIR_CODE_TTL_S, label=label)
        return {"code": code, "expires_in": PAIR_CODE_TTL_S}

    def cancel_code(self) -> None:
        self._code = None

    def code_open(self) -> bool:
        return self._code is not None and self._code.expires > time.time()

    def complete_code(self, code: str, *, extension_id: str, origin: str, browser: str = "", version: str = "") -> dict:
        now = time.time()
        if now - self._last_attempt < PAIR_MIN_INTERVAL_S:
            raise BridgeError("E_RATE_LIMITED", "too many pairing attempts; wait a second", retryable=True)
        self._last_attempt = now
        c = self._code
        if c is None or c.expires <= now:
            self._code = None
            raise BridgeError("E_AUTH", "there is no pairing code open on the desktop app (or it expired)")
        c.attempts_left -= 1
        if not hmac.compare_digest(str(code).strip(), c.code):
            if c.attempts_left <= 0:
                self._code = None
            raise BridgeError("E_AUTH", "that code is wrong", data={"attempts_left": max(0, c.attempts_left)})
        self._code = None
        return _mint(extension_id, origin, browser, version)

    # ---- request/approve flow (used by the native host and by the extension's "Connect" button)
    def request(self, *, extension_id: str, origin: str, browser: str = "", version: str = "") -> dict:
        self._expire()
        if sum(1 for r in self._requests.values() if r.state == "pending") >= 5:
            raise BridgeError("E_RATE_LIMITED", "too many pending pairing requests", retryable=True)
        req = _Request(id=uuid.uuid4().hex[:12], nonce=secrets.token_urlsafe(24), extension_id=extension_id, origin=origin,
                       browser=browser, version=version, created=time.time())
        self._requests[req.id] = req
        return {"request_id": req.id, "nonce": req.nonce, "expires_in": PAIR_REQUEST_TTL_S}

    def pending(self) -> list[dict]:
        self._expire()
        return [{"id": r.id, "extension_id": r.extension_id, "browser": r.browser, "version": r.version, "age_s": int(time.time() - r.created)}
                for r in self._requests.values() if r.state == "pending"]

    def decide(self, request_id: str, approve: bool) -> dict:
        self._expire()
        r = self._requests.get(request_id)
        if r is None or r.state != "pending":
            raise BridgeError("E_PARAMS", "no such pending pairing request")
        if approve:
            r.key = _mint(r.extension_id, r.origin, r.browser, r.version)["key"]
            r.state = "approved"
        else:
            r.state = "denied"
        return {"id": r.id, "state": r.state}

    def collect(self, request_id: str, nonce: str) -> dict:
        """The extension polls this with the nonce it got at request time. The key is handed over exactly once."""
        self._expire()
        r = self._requests.get(request_id)
        if r is None or not hmac.compare_digest(nonce, r.nonce):
            raise BridgeError("E_AUTH", "unknown pairing request")
        if r.state == "approved" and r.key:
            key, r.key, r.state = r.key, None, "collected"
            return {"state": "approved", "key": key, "server_id": server_id()}
        return {"state": r.state}

    def _expire(self) -> None:
        now = time.time()
        for r in list(self._requests.values()):
            if now - r.created > PAIR_REQUEST_TTL_S:
                if r.state == "pending":
                    r.state = "expired"
                if now - r.created > PAIR_REQUEST_TTL_S * 2:
                    self._requests.pop(r.id, None)


def _mint(extension_id: str, origin: str, browser: str, version: str) -> dict:
    label = f"browser: {browser or 'browser'} {extension_id[:8]}"
    key_id, key = db.create_api_key(label, kind=KEY_KIND)
    conn = _conn()
    conn.execute("INSERT OR REPLACE INTO browser_pairings (key_id, extension_id, origin, browser, version, paired_at) VALUES (?,?,?,?,?,?)",
                 (key_id, extension_id, origin, browser, version, time.time()))
    conn.commit()
    db.log_audit(actor="browser_bridge", action="pair", detail=f"{browser} {extension_id}")
    return {"key": key, "key_id": key_id, "server_id": server_id()}


def paired() -> list[dict]:
    rows = _conn().execute(
        "SELECT p.key_id, p.extension_id, p.browser, p.version, p.paired_at, p.last_connected_at, k.revoked_at "
        "FROM browser_pairings p JOIN api_keys k ON k.id = p.key_id WHERE k.revoked_at IS NULL ORDER BY p.paired_at DESC").fetchall()
    live = set(bridge.connections)
    return [{**dict(r), "connected": r["key_id"] in live} for r in rows]


def unpair(key_id: int) -> bool:
    row = _conn().execute("SELECT key_id FROM browser_pairings WHERE key_id=?", (key_id,)).fetchone()
    if row is None:
        return False
    db.revoke_api_key(key_id)
    db.log_audit(actor="browser_bridge", action="unpair", detail=str(key_id))
    bridge.drop(key_id, "unpaired", code=4401)
    return True


pairing = Pairing()


# ------------------------------------------------------------------------------------------ connections
Handler = Callable[["Connection", dict], Awaitable[Any]]


@dataclass
class Connection:
    key_id: int
    ws: Any
    ext: dict
    caps: dict
    session: str
    connected_at: float = field(default_factory=time.time)
    last_seen: float = field(default_factory=time.time)
    pending: dict[str, "_Pending"] = field(default_factory=dict)
    send_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    closed: bool = False
    loop: Any = None                  # the event loop that owns this socket: drop() may be called from a worker thread

    async def send(self, obj: dict) -> None:
        if self.closed:
            raise BridgeError("E_NOT_CONNECTED", "the browser is not connected", retryable=True)
        data = json.dumps(obj, separators=(",", ":"))
        if len(data.encode()) > MAX_FRAME_BYTES:
            raise BridgeError("E_TOO_LARGE", "message exceeds the frame limit")
        async with self.send_lock:
            await self.ws.send_text(data)


@dataclass
class _Pending:
    frame: dict
    future: "asyncio.Future"
    idem: str


class Bridge:
    def __init__(self) -> None:
        self.connections: dict[int, Connection] = {}
        self._orphans: dict[str, tuple[float, dict[str, _Pending]]] = {}      # session -> (dropped_at, pending)
        self.handlers: dict[str, Handler] = {}
        self.listeners: list[Callable[[str, dict], None]] = []

    # ---- registration
    def on(self, method: str) -> Callable[[Handler], Handler]:
        def deco(fn: Handler) -> Handler:
            self.handlers[method] = fn
            return fn
        return deco

    def listen(self, fn: Callable[[str, dict], None]) -> None:
        self.listeners.append(fn)

    def _emit(self, name: str, data: dict) -> None:
        for fn in self.listeners:
            try:
                fn(name, data)
            except Exception:  # noqa: BLE001 - a listener must never break the bridge
                logger.exception("bridge listener failed")

    # ---- state
    def status(self) -> dict:
        return {"protocol": PROTOCOL, "server_id": server_id(), "pairing_open": pairing.code_open(),
                "connections": [{"key_id": c.key_id, "ext": c.ext, "caps": c.caps, "session": c.session, "connected_at": c.connected_at,
                                 "last_seen": c.last_seen, "in_flight": len(c.pending)} for c in self.connections.values()]}

    def connected(self) -> bool:
        return any(not c.closed for c in self.connections.values())

    def pick(self, key_id: Optional[int] = None) -> Connection:
        if key_id is not None:
            c = self.connections.get(key_id)
        else:
            live = [c for c in self.connections.values() if not c.closed]
            c = max(live, key=lambda x: x.last_seen) if live else None
        if c is None or c.closed:
            raise BridgeError("E_NOT_CONNECTED", "no paired browser is connected. Open the browser with the ABP Bridge extension enabled.",
                              retryable=True, hint="Install/enable the extension and pair it from Settings > Browser.")
        return c

    def drop(self, key_id: int, reason: str = "", code: int = 4000) -> None:
        """Close a connection. Safe from any thread (unpair() runs in a worker thread): the close is scheduled on the socket's own loop.
        code 4401 tells the extension it was unpaired, so it stops retrying instead of reconnecting."""
        c = self.connections.pop(key_id, None)
        if c is None:
            return
        c.closed = True
        # requests nobody answered wait for a resume
        if c.pending:
            self._orphans[c.session] = (time.time(), dict(c.pending))
        try:
            coro = c.ws.close(code=code, reason=reason[:100])
            try:
                here = asyncio.get_running_loop()
            except RuntimeError:
                here = None
            if c.loop is not None and here is not c.loop:
                asyncio.run_coroutine_threadsafe(coro, c.loop)
            else:
                asyncio.ensure_future(coro)
        except Exception:  # noqa: BLE001
            logger.exception("could not close the extension connection")
        self._emit("disconnected", {"key_id": key_id, "reason": reason})

    # ---- RPC: server -> extension
    async def call(self, method: str, params: Optional[dict] = None, *, key_id: Optional[int] = None,
                   deadline_ms: int = DEFAULT_DEADLINE_MS, session: str = "", idem: Optional[str] = None,
                   approval: Optional[dict] = None) -> Any:
        conn = self.pick(key_id)
        if len(conn.pending) >= MAX_IN_FLIGHT:
            raise BridgeError("E_BUSY", "too many requests in flight to the browser", retryable=True)
        deadline_ms = max(1, min(int(deadline_ms), MAX_DEADLINE_MS))
        rid = uuid.uuid4().hex
        idem = idem or uuid.uuid4().hex
        frame = {"v": PROTOCOL, "id": rid, "method": method, "params": params or {},
                 "ctx": {"session": session, "deadline_ms": deadline_ms, "idem": idem, **({"approval": approval} if approval else {})}}
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        conn.pending[rid] = _Pending(frame, fut, idem)
        try:
            await conn.send(frame)
            return await asyncio.wait_for(fut, timeout=deadline_ms / 1000.0 + 1.0)
        except asyncio.TimeoutError:
            try:
                await conn.send({"v": PROTOCOL, "method": "cancel", "params": {"id": rid}})
            except BridgeError:
                pass
            raise BridgeError("E_TIMEOUT", f"the browser did not answer {method} within {deadline_ms} ms", retryable=True)
        finally:
            conn.pending.pop(rid, None)

    # ---- the WebSocket session (called from the route)
    async def serve(self, ws: Any, *, origin: Optional[str]) -> None:
        ext_id = origin_ok(origin)
        try:
            raw = await asyncio.wait_for(ws.receive_text(), timeout=HELLO_TIMEOUT_S)
            hello = json.loads(raw)
        except (asyncio.TimeoutError, ValueError, Exception):  # noqa: BLE001
            await ws.close(code=4400)
            return
        try:
            conn = self._handshake(hello, ext_id)
        except BridgeError as exc:
            await ws.send_text(json.dumps({"v": PROTOCOL, "id": hello.get("id"), "error": exc.to_error()}))
            await ws.close(code=4401 if exc.code == "E_AUTH" else 4400)
            return
        conn.ws = _WsAdapter(ws)
        conn.loop = asyncio.get_running_loop()
        old = self.connections.get(conn.key_id)
        if old is not None:                              # one live connection per profile: the newer one wins
            self.drop(conn.key_id, "superseded")
        self.connections[conn.key_id] = conn
        _conn().execute("UPDATE browser_pairings SET last_connected_at=?, browser=?, version=? WHERE key_id=?",
                        (time.time(), str(conn.ext.get("browser", "")), str(conn.ext.get("version", "")), conn.key_id))
        _conn().commit()
        await conn.ws.send_text(json.dumps({"v": PROTOCOL, "id": hello.get("id"), "result": {
            "server_id": server_id(), "protocol": PROTOCOL, "session": conn.session, "abp_version": _abp_version(),
            "policy": _effective_policy(), "features": {"gateway": False, "web_sessions": False}}}))
        self._emit("connected", {"key_id": conn.key_id, "ext": conn.ext})
        params = hello.get("params", hello)
        await self._resume(conn, params.get("resume") if isinstance(params, dict) else None)
        try:
            while True:
                raw = await ws.receive_text()
                conn.last_seen = time.time()
                if len(raw.encode()) > MAX_FRAME_BYTES:
                    continue
                try:
                    msg = json.loads(raw)
                except ValueError:
                    continue
                await self._on_frame(conn, msg)
        except Exception:  # noqa: BLE001 - disconnect (WebSocketDisconnect or transport error)
            pass
        finally:
            if self.connections.get(conn.key_id) is conn:
                self.drop(conn.key_id, "disconnected")

    def _handshake(self, hello: dict, ext_id: Optional[str]) -> Connection:
        params = hello.get("params", hello)
        if hello.get("method") != "hello" or not isinstance(params, dict):
            raise BridgeError("E_PROTOCOL", "the first message must be hello")
        if PROTOCOL not in (params.get("protocol") or []):
            raise BridgeError("E_PROTOCOL", f"this ABP speaks protocol {PROTOCOL}; update the extension or ABP",
                              data={"server": [PROTOCOL]})
        key = str(params.get("key") or "")
        if db.api_key_kind(key) != KEY_KIND or db.verify_api_key(key) is None:
            raise BridgeError("E_AUTH", "this browser is not paired (or was unpaired)")
        key_hash_row = _conn().execute(
            "SELECT p.key_id, p.extension_id FROM browser_pairings p JOIN api_keys k ON k.id = p.key_id "
            "WHERE k.key_hash = ? AND k.revoked_at IS NULL", (_sha(key),)).fetchone()
        if key_hash_row is None:
            raise BridgeError("E_AUTH", "this browser is not paired")
        if ext_id is None or ext_id != key_hash_row["extension_id"]:
            raise BridgeError("E_AUTH", "the connection did not come from the extension this key was issued to")
        ext = dict(params.get("ext") or {})
        ext["id"] = ext_id
        session = uuid.uuid4().hex[:16]
        return Connection(key_id=key_hash_row["key_id"], ws=None, ext=ext, caps=dict(params.get("capabilities") or {}), session=session)

    async def _resume(self, conn: Connection, resume: Optional[dict]) -> None:
        """Re-send anything the previous connection of this profile never answered; drop stale orphans."""
        now = time.time()
        for sid, (dropped, pending) in list(self._orphans.items()):
            if now - dropped > RESUME_WINDOW_S:
                for p in pending.values():
                    if not p.future.done():
                        p.future.set_exception(BridgeError("E_NOT_CONNECTED", "the browser disconnected", retryable=True))
                self._orphans.pop(sid, None)
        prev = (resume or {}).get("session") if isinstance(resume, dict) else None
        if prev and prev in self._orphans:
            _, pending = self._orphans.pop(prev)
            for rid, p in pending.items():
                if p.future.done():
                    continue
                conn.pending[rid] = p
                try:
                    await conn.send(p.frame)                   # same id + idem key: the extension de-duplicates
                except BridgeError:
                    break

    async def _on_frame(self, conn: Connection, msg: dict) -> None:
        if not isinstance(msg, dict):
            return
        mid = msg.get("id")
        if "method" not in msg:                                     # a response to one of our requests
            p = conn.pending.get(mid) if mid else None
            if p and not p.future.done():
                if "error" in msg:
                    e = msg["error"] or {}
                    p.future.set_exception(BridgeError(str(e.get("code") or "E_INTERNAL"), str(e.get("message") or ""),
                                                       retryable=bool(e.get("retryable")), data=e.get("data"), hint=str(e.get("hint") or "")))
                else:
                    p.future.set_result(msg.get("result"))
            return
        method = str(msg["method"])
        params = msg.get("params") or {}
        if method == "pong" or method == "cancel":
            return
        if method == "ping":
            if mid:
                await self._reply(conn, mid, {"t": time.time()})
            return
        handler = self.handlers.get(method)
        if handler is None:
            if mid:
                await self._reply(conn, mid, error=BridgeError("E_METHOD", f"unknown method {method}").to_error())
            elif method.startswith("event."):
                self._emit(method, {"key_id": conn.key_id, **params})
            return
        try:
            result = await handler(conn, params)
            if mid:
                await self._reply(conn, mid, result if result is not None else {})
        except BridgeError as exc:
            if mid:
                await self._reply(conn, mid, error=exc.to_error())
        except Exception:  # noqa: BLE001
            logger.exception("bridge handler %s failed", method)
            if mid:
                await self._reply(conn, mid, error=BridgeError("E_INTERNAL", "the server failed to handle that").to_error())

    async def _reply(self, conn: Connection, mid: str, result: Any = None, error: Optional[dict] = None) -> None:
        frame = {"v": PROTOCOL, "id": mid}
        frame.update({"error": error} if error else {"result": result})
        try:
            await conn.send(frame)
        except BridgeError:
            pass


class _WsAdapter:
    """FastAPI's WebSocket wrapped to the tiny surface the hub needs (also what tests fake)."""

    def __init__(self, ws: Any) -> None:
        self._ws = ws

    async def send_text(self, data: str) -> None:
        await self._ws.send_text(data)

    async def receive_text(self) -> str:
        return await self._ws.receive_text()

    async def close(self, code: int = 1000, reason: str = "") -> None:
        try:
            await self._ws.close(code=code)
        except Exception:  # noqa: BLE001
            pass


def _sha(key: str) -> str:
    import hashlib
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def _abp_version() -> str:
    try:
        from bot import __version__  # type: ignore
        return str(__version__)
    except Exception:  # noqa: BLE001
        return "unknown"


def _effective_policy() -> dict:
    from bot import browser_policy
    return browser_policy.effective()


bridge = Bridge()


@bridge.on("audit.push")
async def _audit_push(conn: Connection, params: dict) -> dict:
    for entry in (params.get("entries") or [])[:200]:
        db.log_audit(actor="browser_ext", action=str(entry.get("action", "event"))[:60], detail=str(entry.get("detail", ""))[:300])
    return {"ok": True}


@bridge.on("models.report")
async def _models_report(conn: Connection, params: dict) -> dict:
    conn.caps["models"] = params
    return {"ok": True}
