"""Server-to-server federation: linking two independent BotServer
installations (e.g. a home PC and a laptop, each with their own database,
config, and Telegram bot) so either admin can see and manage the other's
bots and status from their own dashboard — without merging databases,
sharing one Telegram bot, or standing up any new infrastructure.

Authentication uses two different tokens for two different jobs, on
purpose — this is the whole design, not an incidental detail:

- **DASHBOARD_TOKEN** never leaves the machine it belongs to. It gates the
  one local action that starts a link (minting a pairing token) and every
  other admin action in this file (link/unlink), exactly like it already
  gates everything else in the dashboard. It is never pasted into another
  server's UI and never sent over the network to a peer.
- **Server pairing token** is the only secret that actually crosses the
  network. It's short-lived (10 minutes), single-use, and purpose-built
  for exactly one thing: proving to server B, once, that whoever is
  calling /api/peers/handshake was handed a code B's own admin just
  generated — the same "generate on the target, type into the initiator"
  shape as the existing mobile pairing flow, applied to servers instead of
  phones. A leaked pairing token is far less dangerous than a leaked
  dashboard token: it expires in minutes, works exactly once, and can only
  ever be used to register one new peer link — never to read the dashboard
  token itself, view its config, or touch anything else.

The pairing token is also self-describing: it has B's own reachable
address baked in (supplied once, by B's admin, when generating it — see
_encode_pairing_token/_decode_pairing_token), so the admin linking in on A
never has to separately know or type B's address at all. Pasting the one
pairing token string into A's "Link a server" form is the entire input —
just a name (A's own label for B) and that one token.

Once the handshake completes, ongoing authentication reuses the api_keys
mechanism a paired mobile device already uses unchanged: a linked peer
server is, from the auth layer's point of view, just another api_keys
row — tagged kind='peer_server' only so the dashboard can list "Linked
Servers" separately from "Paired Devices" (see db.py's api_keys.kind
column and list_api_keys/list_devices filtering). That reuse is
deliberate: it means a peer server already has exactly the same
management surface a paired phone does (bot start/stop/restart, config
changes) with exactly the same restriction (can't mint/revoke other
keys) — no new permission model to design, audit, or get wrong.

Linking is a two-step, single-admin-action-per-side handshake:
  0. The admin of server B clicks "Generate pairing token" in B's own
     dashboard (DASHBOARD_TOKEN-gated) — nothing else required. B
     auto-detects its own LAN-reachable address (detect_own_base_url())
     and bakes it into the token itself, so the admin never has to look up
     or type an IP either. They share the resulting code with whoever is
     linking in (chat, in person, however — it's short-lived and
     single-use, so casual sharing is fine).
  1. The admin of server A pastes just that one pairing token (no address
     to look up or type) into A's "Link a server" form, along with
     whatever name A wants to call B. A mints a fresh api_keys row
     (kind='peer_server') for B to call A with, and decodes B's address
     straight out of the token.
  2. A POSTs that credential (plus A's own name and, if known, its own
     reachable base_url) and B's pairing token to B's
     /api/peers/handshake. B validates and consumes the pairing token —
     wrong or expired or already-used token means the request goes no
     further, regardless of anything else in the payload.
  3. B mints its own fresh api_keys row (kind='peer_server') for A to call
     B with, records A as a peer using the credential A just sent, and
     returns that new credential in the response.
  4. A records B as a peer using the credential B just returned.

Both sides end up with a working, independently-revocable credential for
the other, from one action per side — no manual two-way credential
copy-paste, and no long-lived secret ever crosses the network.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import os
import socket
from typing import Optional
from urllib.parse import urlparse

import httpx

from bot import db

logger = logging.getLogger("bot.peers")

HANDSHAKE_TIMEOUT_S = 15
PROXY_TIMEOUT_S = 20

# Two servers being linked are often being set up at the same time (one may
# still be mid-boot) or talking over a home LAN — a single failed connection
# attempt shouldn't force the admin to just click "Link" again and hope.
# Retried only for connection-level failures (refused/timed out/DNS), never
# for an HTTP error response (a real 401/500 answering means the server is
# up and its answer is authoritative, not worth retrying).
_RETRY_DELAYS_S = (1.0, 3.0)


class PeerError(Exception):
    pass


def normalize_base_url(base_url: str) -> str:
    """Validates and normalizes a peer's address: requires an explicit
    http(s) scheme (so a bare "192.168.1.20:8787" doesn't silently resolve
    to something unintended) and strips any trailing slash. Raises
    PeerError with a message the dashboard form can show directly."""
    base_url = (base_url or "").strip().rstrip("/")
    if not base_url:
        raise PeerError("address can't be empty")
    parsed = urlparse(base_url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise PeerError(f"{base_url!r} needs a scheme, e.g. http://192.168.1.20:8787")
    return base_url


def detect_own_base_url() -> Optional[str]:
    """Best-effort auto-detection of this machine's own LAN-reachable
    address, so generating a pairing token never requires an admin to look
    up or type an IP either — the whole flow becomes one click on both
    sides. Uses the standard "open a UDP socket toward some public
    address, ask the OS which local interface it picked" trick: no packet
    actually leaves the machine (UDP connect() is pure local route
    resolution), it just reveals which of this machine's own IPs has a
    route out, which is normally also the LAN IP another device on the
    same network would use to reach it. Returns None if there's no route
    at all (offline machine, no NIC) — the one case that still needs a
    manually-provided address."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
    except OSError:
        return None
    port = os.environ.get("DASHBOARD_PORT", "8787")
    return f"http://{ip}:{port}"


_PAIRING_TOKEN_PREFIX = "bsp1"


def _encode_pairing_token(base_url: str, secret: str) -> str:
    """Bundles this server's own address into the token handed to the
    admin, so linking in never requires separately knowing or typing it —
    see the module docstring. `.` is a safe delimiter: base64url and
    secrets.token_urlsafe's alphabets both exclude it."""
    address_part = base64.urlsafe_b64encode(base_url.encode("utf-8")).decode("ascii").rstrip("=")
    return f"{_PAIRING_TOKEN_PREFIX}.{address_part}.{secret}"


def _decode_pairing_token(token: str) -> tuple[str, str]:
    parts = (token or "").strip().split(".", 2)
    if len(parts) != 3 or parts[0] != _PAIRING_TOKEN_PREFIX or not parts[1] or not parts[2]:
        raise PeerError("that doesn't look like a valid pairing token")
    address_part, secret = parts[1], parts[2]
    padded = address_part + "=" * (-len(address_part) % 4)
    try:
        base_url = base64.urlsafe_b64decode(padded).decode("utf-8")
    except Exception as exc:
        raise PeerError("that doesn't look like a valid pairing token") from exc
    return base_url, secret


async def _post_with_retry(client: httpx.AsyncClient, url: str, **kwargs) -> httpx.Response:
    last_exc: Optional[Exception] = None
    for attempt, delay in enumerate((0.0, *_RETRY_DELAYS_S)):
        if delay:
            logger.info("retrying %s in %.0fs after a connection error: %s", url, delay, last_exc)
            await asyncio.sleep(delay)
        try:
            return await client.post(url, **kwargs)
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout) as exc:
            last_exc = exc
            continue
    raise last_exc  # type: ignore[misc]


def generate_pairing_token(base_url: Optional[str] = None) -> dict:
    """Runs on the receiving side, one DASHBOARD_TOKEN-gated local action,
    one click (see the module docstring's step 0). `base_url` is THIS
    server's own reachable address, baked into the returned token so the
    other side never needs it separately — auto-detected via
    detect_own_base_url() when not given, so nobody has to look up or type
    an address at all in the normal case. An explicit `base_url` still
    works, for the one real edge case auto-detection can't cover (a
    reverse proxy, port forwarding, an address that differs from this
    machine's own outbound-facing IP). The result is what the admin shares
    with whoever is linking in — never the real dashboard token."""
    if base_url:
        base_url = normalize_base_url(base_url)
    else:
        base_url = detect_own_base_url()
        if not base_url:
            raise PeerError("couldn't auto-detect this server's address (no network route) — provide one manually")
    secret, expires_at = db.create_server_pairing_token()
    return {"pairing_token": _encode_pairing_token(base_url, secret), "expires_at": expires_at, "base_url": base_url}


async def link_peer(name: str, pairing_token: str, my_name: str, my_base_url: Optional[str] = None) -> dict:
    """Runs on the initiating side. See module docstring for the full
    handshake. Raises PeerError on any failure — the fresh credential
    minted for the (possibly unreachable) other side is revoked again so a
    failed link attempt never leaves a dangling, never-used api_keys row."""
    base_url, remote_pairing_token = _decode_pairing_token(pairing_token)
    base_url = normalize_base_url(base_url)
    if my_base_url:
        my_base_url = normalize_base_url(my_base_url)

    inbound_key_id, inbound_plaintext = db.create_api_key(f"peer: {name}", kind="peer_server")
    try:
        async with httpx.AsyncClient(timeout=HANDSHAKE_TIMEOUT_S) as client:
            resp = await _post_with_retry(
                client,
                f"{base_url}/api/peers/handshake",
                json={
                    "name": my_name, "base_url": my_base_url, "api_key": inbound_plaintext,
                    "pairing_token": remote_pairing_token,
                },
            )
        if resp.status_code == 401:
            raise PeerError("that server rejected the pairing token — it may be wrong, expired (10 minutes), or already used")
        resp.raise_for_status()
        data = resp.json()
    except httpx.HTTPError as exc:
        db.revoke_api_key(inbound_key_id)
        raise PeerError(
            f"could not reach {base_url}: {exc} — if that address looks right, the other machine's "
            "firewall is the most common culprit: binding to 0.0.0.0 only makes the app itself listen "
            "on every interface, it doesn't open the OS firewall for other devices on the network. "
            "Add an inbound rule allowing that port (Windows: Windows Defender Firewall -> Advanced "
            "Settings -> Inbound Rules -> New Rule -> Port), then try again."
        ) from exc
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


def accept_handshake(name: str, api_key: str, base_url: Optional[str], my_name: str, pairing_token: str) -> dict:
    """Runs on the receiving side, inside the /api/peers/handshake route —
    which deliberately does NOT require DASHBOARD_TOKEN. Auth here is the
    pairing_token instead: it's checked and atomically consumed first,
    before anything else in the payload is trusted, so a wrong/expired/
    reused token rejects the whole request regardless of what else was
    sent. Mints our own credential for the caller to store and records
    them as a peer using the credential they sent us."""
    if not db.consume_server_pairing_token(pairing_token):
        raise PeerError("invalid, expired, or already-used pairing token")
    name = (name or "unnamed server").strip() or "unnamed server"
    if not api_key:
        raise PeerError("handshake payload missing api_key")
    normalized_base_url = normalize_base_url(base_url) if base_url else ""
    inbound_key_id, inbound_plaintext = db.create_api_key(f"peer: {name}", kind="peer_server")
    peer_id = db.create_peer_server(name, normalized_base_url, api_key, inbound_key_id)
    db.mark_peer_server_ok(peer_id)
    return {"api_key": inbound_plaintext, "name": my_name}


def unlink_peer(peer_id: int) -> Optional[dict]:
    row = db.delete_peer_server(peer_id)
    return dict(row) if row is not None else None


async def _proxy_get(peer_row, path: str) -> dict:
    # GET is safe to retry (idempotent) — a flaky LAN or a peer mid-restart
    # shouldn't turn into a hard failure the admin has to notice and retry
    # by hand. POST (run_bot_action) deliberately does NOT retry: a dropped
    # response after a "restart" actually landed would otherwise risk
    # restarting the target bot twice.
    try:
        async with httpx.AsyncClient(timeout=PROXY_TIMEOUT_S) as client:
            last_exc: Optional[Exception] = None
            for attempt, delay in enumerate((0.0, *_RETRY_DELAYS_S)):
                if delay:
                    await asyncio.sleep(delay)
                try:
                    resp = await client.get(
                        f"{peer_row['base_url'].rstrip('/')}{path}",
                        headers={"X-Dashboard-Token": peer_row["outbound_api_key"]},
                    )
                    break
                except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout) as exc:
                    last_exc = exc
            else:
                raise last_exc  # type: ignore[misc]
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


HEALTH_CHECK_INTERVAL_S = 60


async def health_check_all() -> None:
    """Pings every linked peer that has a known base_url and records
    whether it answered — the same db.mark_peer_server_ok/_error a real
    proxied call already triggers, just run proactively instead of only
    ever updating when an admin happens to click into a peer. Without
    this, the dashboard's Online/Unreachable status only reflects the last
    time someone opened "Manage bots" for that server, which could be
    stale for hours after a link actually broke."""
    for row in db.list_peer_servers():
        if not row["base_url"]:
            continue
        try:
            await _proxy_get(dict(row), "/api/overview")
        except PeerError:
            pass  # already recorded via mark_peer_server_error inside _proxy_get


async def health_check_forever(stop_event: asyncio.Event) -> None:
    """Background loop, one iteration per HEALTH_CHECK_INTERVAL_S, started
    alongside the other always-on tasks in bot/main.py. Exits cleanly as
    soon as stop_event is set, same shutdown contract bot/scheduler.py's
    run_forever already uses."""
    while not stop_event.is_set():
        try:
            await health_check_all()
        except Exception:
            logger.exception("peer health check pass failed")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=HEALTH_CHECK_INTERVAL_S)
        except asyncio.TimeoutError:
            pass
