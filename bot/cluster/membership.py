"""The cluster's nodes: this machine, and every linked ABP server (bot/peers.py), kept current by a heartbeat.

Every 15 seconds each linked peer is asked for its node description (GET /api/cluster/node, over the peer link). A
node that answered within 45 s is "ok", within 3 minutes "stale", and after that "down". A peer that runs an ABP
without the cluster layer is listed as "no cluster support" so the owner knows to update it.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Optional

from bot.cluster import executor, inventory, offer, store

logger = logging.getLogger("bot.cluster")
HEARTBEAT_S = 15
STALE_S, DOWN_S = 45, 180
_nodes: dict[str, dict] = {}        # peer name -> {"info", "seen", "latency_ms", "error"}


def node_id() -> str:
    try:
        from bot.server_identity import get_server_id
        return get_server_id()
    except Exception:  # noqa: BLE001
        return inventory.static()["hostname"]


def this_node() -> dict:
    """What this machine tells the cluster about itself (what GET /api/cluster/node returns)."""
    o = offer.current()
    ok, why = offer.available_now(o)
    running = store.list_("runs", states=("running", "starting"), limit=1000)
    held = store.list_("runs", states=("held", "accepted"), limit=1000)
    return {
        "id": node_id(), "name": inventory.static()["hostname"], "static": inventory.static(), "live": inventory.live(),
        "software": inventory.software(), "api": 1,
        "offer": {k: v for k, v in o.items() if k != "work_dir"},
        "capacity": offer.budget.capacity(o), "free": offer.budget.free(o), "available": ok, "unavailable_reason": why,
        "jobs": {"running": len(running), "waiting": len(held)}, "idle_s": offer.idle_seconds(), "at": time.time(),
    }


def _health(entry: dict) -> str:
    if entry.get("unsupported"):
        return "unsupported"
    age = time.time() - float(entry.get("seen") or 0)
    return "ok" if age <= STALE_S else "stale" if age <= DOWN_S else "down"


def nodes(include_self: bool = True) -> list[dict]:
    """Every node: {"name", "peer" (None for this machine), "health", "latency_ms", "info"}."""
    out = []
    if include_self:
        out.append({"name": inventory.static()["hostname"], "peer": None, "health": "ok", "latency_ms": 0,
                    "info": this_node()})
    try:
        from bot import db
        linked = [r["name"] for r in db.list_peer_servers()]
    except Exception:  # noqa: BLE001
        linked = []
    for name in linked:
        e = _nodes.get(name) or {}
        out.append({"name": name, "peer": name, "health": _health(e) if e else "unknown",
                    "latency_ms": e.get("latency_ms"), "error": e.get("error"), "info": e.get("info"),
                    "seen": e.get("seen")})
    return out


async def poll_peer(row) -> None:
    from bot import peers
    name = row["name"]
    e = _nodes.setdefault(name, {})
    t0 = time.monotonic()
    try:
        info = await peers.proxy(row, "GET", "/api/cluster/node", timeout=20, wake=False)
        e.update(info=info, seen=time.time(), latency_ms=round((time.monotonic() - t0) * 1000), error=None,
                 unsupported=False)
    except Exception as exc:  # noqa: BLE001
        msg = str(exc)
        e["error"] = msg[:300]
        e["unsupported"] = "404" in msg or "only /api/" in msg


async def poll_all() -> None:
    try:
        from bot import db
        rows = [r for r in db.list_peer_servers() if r["base_url"]]
    except Exception:  # noqa: BLE001
        return
    await asyncio.gather(*(poll_peer(r) for r in rows), return_exceptions=True)


async def heartbeat_forever(stop_event: asyncio.Event) -> None:
    """The cluster's background loop: refresh peers' node descriptions, and once an hour tidy old job folders."""
    last_cleanup = 0.0
    while not stop_event.is_set():
        try:
            await poll_all()
            if time.time() - last_cleanup > 3600:
                last_cleanup = time.time()
                await asyncio.to_thread(executor.cleanup)
                await asyncio.to_thread(store.prune, "runs", 30 * 86400)
        except Exception:  # noqa: BLE001
            logger.exception("cluster heartbeat failed")
        try:
            from bot.cluster import scheduler
            await scheduler.supervise_once()
        except Exception:  # noqa: BLE001
            logger.exception("cluster supervision failed")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=HEARTBEAT_S)
        except asyncio.TimeoutError:
            pass


def peer_row(name: str) -> Optional[Any]:
    from bot import peers
    try:
        return peers.find_peer(name)
    except peers.PeerError:
        return None
