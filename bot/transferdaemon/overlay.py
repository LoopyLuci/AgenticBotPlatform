"""TransferDaemon's overlay, the network ABP's linked servers find each other over.

Two machines on different networks, both behind NAT, with no firewall rule and no forwarded port: TransferDaemon's
overlay carries ABP's own traffic between them. This module is ABP's whole view of it — a thin, honest wrapper over
the overlay operations on TransferDaemon's control hub (see docs/agents/transferdaemon.md), each one a call to
``/v1/call/{op}`` like every other module operation:

    overlay.status                                     is this daemon on the overlay, and as whom
    overlay.peers                                      the routes to every other node: kind, rtt_ms, loss, mbps
    overlay.invite_create {ttl_s}                      an invite another machine can join with
    overlay.invite_accept {invite, name?}              join the node that issued it
    overlay.expose {service, local_addr, allow}        publish a local address to paired nodes only
    overlay.unexpose                                   stop publishing it
    overlay.connect {node_id|group, service}           a stable local endpoint that reaches a peer's service
    overlay.disconnect                                 drop that endpoint again
    overlay.services                                   what is published, and by whom
    overlay.proxy_status                               what the overlay itself is doing
    overlay.events                                     the event stream (route up/down, a peer joined)

Everything here is synchronous, like bot/transferdaemon/client.py (whose calls it makes); async callers use
asyncio.to_thread, the same way bot/transferdaemon/tools.py does.

Nothing here raises when TransferDaemon is simply not there or is too old to know about the overlay: ``available()``
is the gate every caller checks, and the answer is "linking still works, just directly" — the behaviour ABP had
before the overlay existed. Responses are normalised leniently (an id under ``node_id``, ``node`` or ``id``) because
these calls cross a program boundary this side does not own.
"""
from __future__ import annotations

import os
import shutil
import threading
import time
from pathlib import Path
from typing import Any, Optional

from bot.transferdaemon import client
from bot.transferdaemon.client import DaemonError

# The name ABP publishes itself under on the overlay, and who may reach it: only the nodes paired with this
# machine (its linked servers), never anyone else on the overlay.
SERVICE = "abp"
ALLOW_PAIRED = ("*paired",)
# A pairing token lives 10 minutes (db.create_server_pairing_token's own default); the TD invite inside it may
# not outlive it by much.
INVITE_TTL_S = 600
_CACHE_TTL_S = 5.0

_lock = threading.Lock()
_cache: dict[str, Any] = {"status": None, "routes": None, "at": 0.0}


# ---- responses ---------------------------------------------------------------------------------------------------------
def _first(d: Any, *keys: str) -> Any:
    """The first of these keys present in a response that may be a dict or a bare value."""
    if not isinstance(d, dict):
        return d if keys and keys[0] == "value" else None
    for k in keys:
        if d.get(k) not in (None, ""):
            return d[k]
    return None


def _rows(result: Any, *keys: str) -> list[dict]:
    """A list of rows out of a response that may be the list itself or a dict wrapping it."""
    if isinstance(result, list):
        return [r for r in result if isinstance(r, dict)]
    if isinstance(result, dict):
        for k in keys or ("peers", "routes", "items"):
            if isinstance(result.get(k), list):
                return [r for r in result[k] if isinstance(r, dict)]
    return []


def loss_fraction(value: Any) -> float:
    """Packet loss as a fraction of 1. TD reports it either way round (0.02 or 2), so both are accepted."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return 0.0
    v = v / 100.0 if v > 1 else v
    return min(max(v, 0.0), 1.0)


def route_summary(row: dict) -> dict:
    """One overlay route as the UI and the scheduler want it: which way, how far, how good, and its state."""
    return {
        "kind": _first(row, "kind", "via", "type") or "",
        "rtt_ms": _first(row, "rtt_ms", "rtt", "latency_ms"),
        "loss": _first(row, "loss", "loss_pct"),
        "mbps": _first(row, "mbps", "throughput_mbps", "bps"),
        "state": _first(row, "state", "status") or "",
        "node_id": _first(row, "node_id", "node", "peer_id", "id") or "",
        "name": _first(row, "name", "label") or "",
    }


# ---- the overlay itself -----------------------------------------------------------------------------------------------
def _call(op: str, args: Optional[dict] = None, *, timeout: float = 30.0) -> Any:
    return client.call(op, dict(args or {}), timeout=timeout)


def available() -> bool:
    """Whether this machine has a TransferDaemon that speaks the overlay at all."""
    try:
        return bool(client.find()) and bool(_call("overlay.status", timeout=10.0))
    except (DaemonError, OSError, ValueError):
        return False


def _cached(name: str, loader) -> Any:
    now = time.monotonic()
    with _lock:
        fresh = now - float(_cache["at"]) <= _CACHE_TTL_S
        if fresh and _cache.get(name) is not None:
            return _cache[name]
    value = loader()
    with _lock:
        _cache[name] = value
        _cache["at"] = now
    return value


def forget_cache() -> None:
    """Drop what is cached (a route that just changed must not be answered from the last answer)."""
    with _lock:
        _cache.update(status=None, routes=None, at=0.0)


def status(*, refresh: bool = False) -> Optional[dict]:
    """overlay.status: {node_id, name, state, ...} for this machine's own daemon, or None without an overlay."""
    def load() -> Optional[dict]:
        try:
            raw = _call("overlay.status", timeout=10.0)
        except (DaemonError, OSError, ValueError):
            return None
        if not isinstance(raw, dict):
            return None
        out = dict(raw)
        out["node_id"] = _first(raw, "node_id", "node", "id", "peer_id") or ""
        out["name"] = _first(raw, "name", "label") or ""
        out["state"] = _first(raw, "state", "status") or ""
        return out
    return load() if refresh else _cached("status", load)


def node_id() -> str:
    """This machine's TD node id: how peers address it once the overlay is the route."""
    return str((status() or {}).get("node_id") or "")


def routes(*, refresh: bool = False) -> list[dict]:
    """overlay.peers: one summary per route to another overlay node, best fields first."""
    def load() -> list[dict]:
        try:
            raw = _call("overlay.peers", timeout=15.0)
        except (DaemonError, OSError, ValueError):
            return []
        return [route_summary(r) for r in _rows(raw, "peers", "routes") if route_summary(r)["node_id"]]
    return load() if refresh else _cached("routes", load)


def route_to(node_id: str) -> Optional[dict]:
    """The route to one overlay node, or None (not connected, or no overlay)."""
    return next((r for r in routes() if r["node_id"] == str(node_id)), None)


def group_nodes(group: str) -> set[str]:
    """The node ids a TD service group covers — what a cluster gang can target."""
    want = str(group or "")
    if not want:
        return set()
    out: set[str] = set()
    try:
        raw = _call("overlay.services", timeout=15.0)
    except (DaemonError, OSError, ValueError):
        raw = None
    for row in _rows(raw, "services"):
        if want not in (str(_first(row, "group", "name") or ""), str(_first(row, "service") or "")):
            continue
        for key in ("nodes", "members", "node_ids"):
            for n in row.get(key) or []:
                out.add(str(_first(n, "node_id", "node", "id") if isinstance(n, dict) else n))
    for r in routes():
        if str(r.get("group") or "") == want:
            out.add(r["node_id"])
    return out


def proxy_status() -> dict:
    """overlay.proxy_status: what the overlay is doing, for the Linked Servers page and the agent."""
    try:
        raw = _call("overlay.proxy_status", timeout=10.0)
    except (DaemonError, OSError, ValueError) as e:
        return {"ok": False, "error": str(e)}
    return dict(raw) if isinstance(raw, dict) else {"ok": True, "detail": raw}


# ---- pairing over the overlay -----------------------------------------------------------------------------------------
def invite_create(ttl_s: int = INVITE_TTL_S) -> Optional[dict]:
    """A one-shot invite another machine pairs with. None when there is no overlay to invite anyone to."""
    try:
        raw = _call("overlay.invite_create", {"ttl_s": int(ttl_s)}, timeout=20.0)
    except (DaemonError, OSError, ValueError):
        return None
    invite = _first(raw, "invite", "code", "token", "value")
    if not invite:
        return None
    st = status() or {}
    return {"invite": str(invite), "node_id": str(_first(raw, "node_id", "node", "id") or st.get("node_id") or ""),
            "ttl_s": int(ttl_s), "expires_at": _first(raw, "expires_at", "expires")}


def invite_accept(invite: str, name: Optional[str] = None) -> Optional[dict]:
    """Pair this machine's daemon with the node that issued `invite`; returns the paired node's id and route."""
    if not invite:
        return None
    args: dict[str, Any] = {"invite": str(invite)}
    if name:
        args["name"] = str(name)
    try:
        raw = _call("overlay.invite_accept", args, timeout=30.0)
    except (DaemonError, OSError, ValueError):
        return None
    forget_cache()
    st = status(refresh=True) or {}
    peer = _first(raw, "node_id", "node", "id", "peer_id") or _first(raw, "peer")
    return {"node_id": str(peer or ""), "name": name or "", "our_node_id": st.get("node_id", "")}


def expose(local_addr: str, *, allow=ALLOW_PAIRED, service: str = SERVICE) -> Optional[dict]:
    """Publish this machine's dashboard to paired overlay nodes only. No firewall, no forwarded port."""
    try:
        raw = _call("overlay.expose", {"service": service, "local_addr": str(local_addr), "allow": list(allow)},
                    timeout=20.0)
    except (DaemonError, OSError, ValueError):
        return None
    return dict(raw) if isinstance(raw, dict) else {"service": service, "local_addr": str(local_addr)}


def unexpose(*, service: str = SERVICE) -> bool:
    try:
        _call("overlay.unexpose", {"service": service}, timeout=15.0)
    except (DaemonError, OSError, ValueError):
        return False
    forget_cache()
    return True


def connect(*, node_id: Optional[str] = None, group: Optional[str] = None, service: str = SERVICE) -> Optional[dict]:
    """A stable local endpoint that reaches `service` on one node (or on a service group).

    Returns ``{"local_addr", "route"}``: local_addr is what ABP calls from now on, route says how it gets there.
    """
    if not node_id and not group:
        return None
    args: dict[str, Any] = {"service": service}
    if node_id:
        args["node_id"] = str(node_id)
    else:
        args["group"] = str(group)
    try:
        raw = _call("overlay.connect", args, timeout=30.0)
    except (DaemonError, OSError, ValueError):
        return None
    forget_cache()
    local_addr = _first(raw, "local_addr", "addr", "endpoint", "url")
    if not local_addr:
        return None
    route = raw.get("route") if isinstance(raw, dict) else None
    return {"local_addr": str(local_addr).rstrip("/"), "route": route_summary(route) if isinstance(route, dict) else {},
            "node_id": str(node_id or _first(raw, "node_id") or ""), "group": str(group or _first(raw, "group") or "")}


def disconnect(*, node_id: Optional[str] = None, group: Optional[str] = None, service: str = SERVICE) -> bool:
    args: dict[str, Any] = {"service": service}
    if node_id:
        args["node_id"] = str(node_id)
    elif group:
        args["group"] = str(group)
    else:
        return False
    try:
        _call("overlay.disconnect", args, timeout=15.0)
    except (DaemonError, OSError, ValueError):
        return False
    forget_cache()
    return True


def services() -> list[dict]:
    """overlay.services: what every node on the overlay publishes (ABP publishes one thing: its dashboard)."""
    try:
        raw = _call("overlay.services", timeout=15.0)
    except (DaemonError, OSError, ValueError):
        return []
    return _rows(raw, "services")


def events(seconds: float = 5.0) -> list[dict]:
    """The overlay's event stream, bounded by `seconds` (a route came up, a peer joined, a service moved)."""
    try:
        raw = _call("overlay.events", {"seconds": float(seconds)}, timeout=float(seconds) + 10.0)
    except (DaemonError, OSError, ValueError):
        return []
    return _rows(raw, "events", "items")


# ---- files ------------------------------------------------------------------------------------------------------------
def downloads_dir() -> Path:
    """Where this machine's daemon writes fully received files (the daemon's own rule, so ABP looks in the
    same place: $TRANSFERD_DOWNLOADS_DIR, else the platform's local data folder plus transferdaemon/downloads)."""
    env = os.environ.get("TRANSFERD_DOWNLOADS_DIR")
    if env:
        return Path(env)
    return client.data_dir() / "downloads"


def await_download(file_name: str, timeout_s: float = 600.0) -> Optional[Path]:
    """Wait for a received file to show up whole, and return its path. None if it never arrived.

    Polling the filesystem rather than a transfer-completed event: the daemon writes the file once, at the
    end, under a name sanitised of any path separators (its own rule), so a file that exists there with the
    expected size is the file, finished."""
    want = Path(str(file_name)).name.replace("/", "_").replace("\\", "_")
    d = downloads_dir()
    deadline = time.monotonic() + max(1.0, float(timeout_s))
    while time.monotonic() < deadline:
        p = d / want
        if p.is_file() and p.stat().st_size > 0:
            return p
        time.sleep(0.5)
    return None


def send_file(path: Any, node_id: str) -> dict:
    """Send a file to an overlay node with TransferDaemon's own transfer (chunked, checked, resumable).

    The reason a big cluster result does not travel as an HTTP body: this is what the platform is for.
    """
    p = Path(str(path))
    if not p.is_file():
        raise DaemonError(f"no such file: {p}", code="not_found", status=404)
    if not node_id:
        raise DaemonError("that file has no destination node", code="not_found", status=404)
    return client.call("transfers.send_file", {"contact_id": str(node_id), "file_path": str(p)}, timeout=3600.0) or {}


def install_hint() -> dict:
    """Whether TransferDaemon is here, and whether ABP should offer to install it (pairing asks on Windows,
    Linux and macOS alike — it is the one step that makes linking work across networks). The install itself
    goes through the module install path (bot.modules.harness -> this module's adapter), so there is exactly
    one way to get it."""
    from bot.transferdaemon import harness
    installed = harness._is_checkout(harness.install_dir())
    built = harness.binary("transferd").is_file()
    return {"installed": installed, "built": built, "offer_install": not installed, "path": str(harness.install_dir()),
            "cargo": bool(shutil.which("cargo") or (Path.home() / ".cargo" / "bin" / f"cargo{harness.EXE}").is_file()),
            "note": ("TransferDaemon is not installed. Installing it (Modules -> TransferDaemon -> Install, or "
                     "td_setup {action: install}) is what lets two ABP installations link from anywhere: with it "
                     "the link rides its overlay and needs no open port and no forwarded address. Without it, "
                     "linking still works directly between machines on the same network.")}


def ensure_running(wait_s: float = 30.0) -> dict:
    """Start TransferDaemon's daemon if it is installed, and say what happened. Never installs anything."""
    from bot.transferdaemon import harness
    try:
        return harness.ensure_running(wait_s=wait_s)
    except DaemonError as e:
        return {"running": False, "error": str(e), "code": e.code}


def summary() -> dict:
    """The short form for the UI and the agent: is the overlay up, who am I, what routes exist."""
    st = status()
    return {
        "available": st is not None,
        "node_id": (st or {}).get("node_id", ""),
        "name": (st or {}).get("name", ""),
        "state": (st or {}).get("state", ""),
        "service": SERVICE,
        "routes": routes(),
        "services": len(services()),
    }
