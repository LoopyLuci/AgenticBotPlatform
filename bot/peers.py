"""Server-to-server federation: linking two independent BotServer
installations (e.g. a home PC and a laptop, each with their own database,
config, and Telegram bot) so either admin can see and manage the other's
bots and status from their own dashboard — without merging databases,
sharing one Telegram bot, or standing up any new infrastructure.

Authentication reuses the api_keys mechanism a paired mobile device
already uses unchanged: a linked peer server is, from the auth layer's
point of view, just another api_keys row — tagged kind='peer_server' only
so the dashboard can list "Linked Servers" separately from "Paired
Devices" (see db.py's api_keys.kind column and list_api_keys/list_devices
filtering). That reuse is deliberate: it means a peer server already has
exactly the same management surface a paired phone does (bot
start/stop/restart, config changes) with exactly the same restriction
(can't mint/revoke other keys) — no new permission model to design,
audit, or get wrong.

Linking is a single-admin-action handshake: the admin of server A pastes
server B's base URL and B's own dashboard token into A's "Link a server"
form. That one call:
  1. A mints a fresh api_keys row (kind='peer_server') for B to call A with.
  2. A POSTs that credential (plus A's own name and, if known, its own
     reachable base_url) to B's /api/peers/handshake, authenticating with
     B's dashboard token — proving the admin controls both boxes, the same
     trust bar every other dashboard-token-gated action already uses.
  3. B mints its own fresh api_keys row (kind='peer_server') for A to call
     B with, records A as a peer using the credential A just sent, and
     returns that new credential in the response.
  4. A records B as a peer using the credential B just returned.

Both sides end up with a working, independently-revocable credential for
the other, from one action — no manual two-way credential copy-paste.
"""

from __future__ import annotations

from typing import Optional

import httpx

from bot import db

HANDSHAKE_TIMEOUT_S = 15
PROXY_TIMEOUT_S = 20


class PeerError(Exception):
    pass


async def link_peer(name: str, base_url: str, remote_dashboard_token: str, my_name: str, my_base_url: Optional[str] = None) -> dict:
    """Runs on the initiating side. See module docstring for the full
    handshake. Raises PeerError on any failure — the fresh credential
    minted for the (possibly unreachable) other side is revoked again so a
    failed link attempt never leaves a dangling, never-used api_keys row."""
    base_url = base_url.rstrip("/")
    if not base_url:
        raise PeerError("base_url can't be empty")

    inbound_key_id, inbound_plaintext = db.create_api_key(f"peer: {name}", kind="peer_server")
    try:
        async with httpx.AsyncClient(timeout=HANDSHAKE_TIMEOUT_S) as client:
            resp = await client.post(
                f"{base_url}/api/peers/handshake",
                headers={"X-Dashboard-Token": remote_dashboard_token},
                json={"name": my_name, "base_url": my_base_url, "api_key": inbound_plaintext},
            )
        if resp.status_code == 401:
            raise PeerError("that server rejected the dashboard token")
        resp.raise_for_status()
        data = resp.json()
    except httpx.HTTPError as exc:
        db.revoke_api_key(inbound_key_id)
        raise PeerError(f"could not reach {base_url}: {exc}") from exc
    except PeerError:
        db.revoke_api_key(inbound_key_id)
        raise

    outbound_key = data.get("api_key")
    remote_name = (data.get("name") or name).strip() or name
    if not outbound_key:
        db.revoke_api_key(inbound_key_id)
        raise PeerError("peer accepted the handshake but returned no callback credential")

    peer_id = db.create_peer_server(remote_name, base_url, outbound_key, inbound_key_id)
    db.mark_peer_server_ok(peer_id)
    return dict(db.get_peer_server(peer_id))


def accept_handshake(name: str, api_key: str, base_url: Optional[str], my_name: str) -> dict:
    """Runs on the receiving side, inside the /api/peers/handshake route
    (already gated by _require_token — only the real admin's dashboard
    token gets here). Mints our own credential for the caller to store and
    records them as a peer using the credential they sent us."""
    name = (name or "unnamed server").strip() or "unnamed server"
    if not api_key:
        raise PeerError("handshake payload missing api_key")
    inbound_key_id, inbound_plaintext = db.create_api_key(f"peer: {name}", kind="peer_server")
    peer_id = db.create_peer_server(name, (base_url or "").rstrip("/"), api_key, inbound_key_id)
    db.mark_peer_server_ok(peer_id)
    return {"api_key": inbound_plaintext, "name": my_name}


def unlink_peer(peer_id: int) -> Optional[dict]:
    row = db.delete_peer_server(peer_id)
    return dict(row) if row is not None else None


async def _proxy_get(peer_row, path: str) -> dict:
    try:
        async with httpx.AsyncClient(timeout=PROXY_TIMEOUT_S) as client:
            resp = await client.get(
                f"{peer_row['base_url'].rstrip('/')}{path}",
                headers={"X-Dashboard-Token": peer_row["outbound_api_key"]},
            )
        resp.raise_for_status()
        db.mark_peer_server_ok(peer_row["id"])
        return resp.json()
    except httpx.HTTPError as exc:
        db.mark_peer_server_error(peer_row["id"], str(exc))
        raise PeerError(str(exc)) from exc


async def _proxy_post(peer_row, path: str, json_body: Optional[dict] = None) -> dict:
    try:
        async with httpx.AsyncClient(timeout=PROXY_TIMEOUT_S) as client:
            resp = await client.post(
                f"{peer_row['base_url'].rstrip('/')}{path}",
                headers={"X-Dashboard-Token": peer_row["outbound_api_key"]},
                json=json_body,
            )
        resp.raise_for_status()
        db.mark_peer_server_ok(peer_row["id"])
        return resp.json()
    except httpx.HTTPError as exc:
        db.mark_peer_server_error(peer_row["id"], str(exc))
        raise PeerError(str(exc)) from exc


async def fetch_overview(peer_row) -> dict:
    if not peer_row["base_url"]:
        raise PeerError("this peer never shared a reachable base_url — it can call us, but we can't call it back")
    return await _proxy_get(peer_row, "/api/overview")


async def fetch_bots(peer_row) -> list:
    if not peer_row["base_url"]:
        raise PeerError("this peer never shared a reachable base_url — it can call us, but we can't call it back")
    return await _proxy_get(peer_row, "/api/bots")


_BOT_ACTIONS = {"start", "stop", "restart", "enable", "disable"}


async def run_bot_action(peer_row, instance_id: int, action: str) -> dict:
    if action not in _BOT_ACTIONS:
        raise PeerError(f"unknown action {action!r}")
    if not peer_row["base_url"]:
        raise PeerError("this peer never shared a reachable base_url — it can call us, but we can't call it back")
    return await _proxy_post(peer_row, f"/api/bots/{instance_id}/{action}")
