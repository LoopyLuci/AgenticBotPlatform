"""Server-to-server federation: linking two independent AgenticBotPlatform
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

**SSH auto-pairing** (see _setup_own_ssh/_finish_ssh_setup below) rides
along on the exact same handshake, for the same reason the credential
exchange does: it's the one channel both sides have already authenticated
each other over, so no separate manual key-copying step (generate a key,
scp the .pub file, paste it into authorized_keys, fix Windows' ACL
requirement by hand — the exact multi-step dance this replaces) is needed.
Each side generates its own ed25519 keypair (SSH Toolkit's own
New-SshLinkKeypair — never a private key crossing the network, only each
side's public key), includes its public key and OS username in its half of
the handshake, and on receiving the other's: trusts their public key
locally (Install-SshLinkTrustedKey) and registers a connection back to
them (Add-SshLinkConnection, using the OS username they reported and the
address already exchanged for the API link itself). This is deliberately
best-effort and never fails the underlying API pairing: SSH Toolkit is an
optional subsystem (see bot/ssh_toolkit.py's own "fails closed, cleanly"
philosophy) — a peer link with no SSH connectivity set up is still a
completely valid, working peer link, just without a ready-made SSH
connection alongside it. Callers that want the API pairing without SSH
side effects (a same-host test, a bare pairing) pass setup_ssh=False.

**TransferDaemon carries the link** (bot/transferdaemon/overlay.py).
The address baked into the pairing token is a best guess — a LAN
address, and on a machine behind NAT or on a VPN it is an address nobody
outside can reach at all (10.x, 100.64.x, a CGNAT address). So the token
also carries this machine's TransferDaemon **overlay invite**
(overlay.invite_create) whenever it has one, and:

  - the linking side accepts that invite (overlay.invite_accept), which
    pairs the two TransferDaemon daemons as part of the same single step;
  - each side publishes its own dashboard on the overlay to paired nodes
    only (overlay.expose, allow ["*paired"]) and asks for a stable local
    endpoint to the other (overlay.connect) — which is what the handshake,
    and everything after it, actually travels over.

An unreachable advertised address therefore never breaks a link: it just
isn't the route any more. The direct URL stays one route among the
others — the fallback, and the only route when TransferDaemon is not
installed anywhere, in which case everything here behaves exactly as it
did before.

Everything goes over it: heartbeats, the peer proxy, cluster offers and
placement, logs, and files big enough to be worth TransferDaemon's own
transfer (transfers.send_file). Nothing in the default flow asks anyone to
open a port — bot/firewall.py stays what it always was, an explicit manual
action for a direct address on a private network.
"""

from __future__ import annotations

import asyncio
import base64
import getpass
import json
import logging
import os
import re
import socket
import time
from typing import Any, Optional
from urllib.parse import urlparse

import httpx

from bot import db
from bot.transferdaemon.client import DaemonError

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

# peer id -> {"local_addr", "kind", "rtt_ms", "loss", "mbps", "state", "at"}: what TransferDaemon's overlay
# handed us the last time we asked, per linked peer. In memory on purpose — it is derived state, rebuilt
# from overlay.connect on every heartbeat, and nothing is lost by ABP restarting (the first pass re-asks).
_routes: dict[int, dict] = {}


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


_PAIRING_TOKEN_PREFIX = "bsp2"
_LEGACY_PAIRING_TOKEN_PREFIX = "bsp1"


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _unb64(part: str) -> bytes:
    return base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))


def _encode_pairing_token(base_url: str, secret: str, td: Optional[dict] = None) -> str:
    """Bundles this server's own address — and, when TransferDaemon is
    running here, its overlay invite — into the token handed to the admin,
    so linking in never requires separately knowing or typing an address
    and never has to touch a firewall (see the module docstring). `.` is a
    safe delimiter: base64url and secrets.token_urlsafe's alphabets both
    exclude it.

    The TD part is what makes pairing work across networks: the two
    TransferDaemon daemons pair with each other as part of the same one
    click, and the handshake then travels over the overlay instead of the
    advertised LAN address — which behind NAT (10.0.x.x, a CGNAT address,
    a VPN) is unreachable and would otherwise break the link."""
    address_part = _b64(base_url.encode("utf-8"))
    td_part = _b64(json.dumps(td, separators=(",", ":")).encode("utf-8")) if td else ""
    return f"{_PAIRING_TOKEN_PREFIX}.{address_part}.{td_part}.{secret}"


def _decode_pairing_token(token: str) -> tuple[str, str, dict]:
    """(base_url, secret, td) out of a pairing token. A token minted before
    the overlay (prefix bsp1, or any bsp2 without a TD part) decodes fine
    and simply carries no TD identity: that machine pairs directly, exactly
    as it did before."""
    parts = (token or "").strip().split(".")
    if len(parts) == 3 and parts[0] == _LEGACY_PAIRING_TOKEN_PREFIX:
        parts = [_PAIRING_TOKEN_PREFIX, parts[1], "", parts[2]]
    if len(parts) != 4 or parts[0] != _PAIRING_TOKEN_PREFIX or not parts[1] or not parts[3]:
        raise PeerError("that doesn't look like a valid pairing token")
    try:
        base_url = _unb64(parts[1]).decode("utf-8")
        td = json.loads(_unb64(parts[2]).decode("utf-8")) if parts[2] else {}
    except Exception as exc:
        raise PeerError("that doesn't look like a valid pairing token") from exc
    if not isinstance(td, dict):
        td = {}
    return base_url, parts[3], td


async def _post_with_retry(client: httpx.AsyncClient, url: str, **kwargs) -> httpx.Response:
    last_exc: Optional[Exception] = None
    for _attempt, delay in enumerate((0.0, *_RETRY_DELAYS_S)):
        if delay:
            logger.info("retrying %s in %.0fs after a connection error: %s", url, delay, last_exc)
            await asyncio.sleep(delay)
        try:
            return await client.post(url, **kwargs)
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout) as exc:
            last_exc = exc
            continue
    raise last_exc  # type: ignore[misc]


def td_overlay() -> Any:
    """The overlay module, imported lazily so a machine with no TransferDaemon never pays for it."""
    from bot.transferdaemon import overlay

    return overlay


def td_state(identity: Optional[dict] = None) -> dict:
    """What to tell the UI about this machine's TransferDaemon: is the overlay up, is it paired with anybody,
    and — the actionable part — should ABP offer to install it. Without it linking still works, but only
    directly, which means it stops working the moment the two machines are on different networks."""
    overlay = td_overlay()
    out: dict[str, Any] = {"service": overlay.SERVICE, "available": False, "node_id": "", "peers": 0}
    try:
        out.update(overlay.install_hint())
    except Exception:  # noqa: BLE001  (a broken TD install must never break the peers page)
        out.update(installed=False, built=False, offer_install=True)
    try:
        if not overlay.available():
            return out
        st = overlay.status() or {}
        out.update(available=True, node_id=st.get("node_id", ""), name=st.get("name", ""), state=st.get("state", ""),
                   peers=len(overlay.routes()))
    except Exception:  # noqa: BLE001
        return out
    if identity:
        out["invited"] = bool(identity.get("invite"))
    return out


def td_routes() -> dict:
    """Every linked peer's route, as the UI and API show it: kind, RTT, state, and the address actually used."""
    return {r["name"]: route_info(r) for r in db.list_peer_servers()}


def route_info(peer_row) -> dict:
    """How we currently reach one peer: over TransferDaemon's overlay (kind, RTT, state) when there is a
    route, or directly at the address it advertised. `local_addr` is what the next call will actually use."""
    r = _routes.get(int(peer_row["id"])) or {}
    direct = str(peer_row["base_url"] or "")
    td_node_id = _td_node_id(peer_row)
    return {"kind": r.get("kind") or ("overlay" if r.get("local_addr") else "direct"), "rtt_ms": r.get("rtt_ms"),
            "loss": r.get("loss"), "mbps": r.get("mbps"), "state": r.get("state") or ("up" if direct else "down"),
            "local_addr": r.get("local_addr") or direct, "direct_url": direct, "td_node_id": td_node_id,
            "over_td": bool(td_node_id)}


def peer_base_url(peer_row) -> str:
    """The address to actually call a peer on right now: the overlay's stable local endpoint when there is a
    route, else the address it advertised. A direct URL stays one route among others — it is what the link
    falls back to, and what it still uses when TransferDaemon is not installed anywhere."""
    return str((_routes.get(int(peer_row["id"])) or {}).get("local_addr") or peer_row["base_url"] or "").rstrip("/")


def _td_node_id(peer_row) -> str:
    try:
        return str(peer_row["td_node_id"] or "")
    except (IndexError, KeyError):
        return ""


def _remember_route(peer_row, conn: Optional[dict]) -> None:
    """Cache a peer's overlay endpoint and its metrics. Refreshing it is cheap and idempotent, which is what
    makes a route change (relay, direct path, a node coming back) pick itself up on the next pass."""
    pid = int(peer_row["id"])
    if not conn:
        _routes.pop(pid, None)
        return
    route = conn.get("route") or {}
    _routes[pid] = {"local_addr": str(conn.get("local_addr") or ""), "kind": route.get("kind") or "overlay",
                    "rtt_ms": route.get("rtt_ms"), "loss": route.get("loss"), "mbps": route.get("mbps"),
                    "state": route.get("state") or "up", "node_id": conn.get("node_id") or "",
                    "at": time.monotonic()}


def refresh_route(peer_row) -> bool:
    """(Re)connect to one peer over the overlay and cache the endpoint it hands back. False when this peer
    has no TD identity or the overlay is not there, in which case its direct address keeps being used."""
    td_node_id = _td_node_id(peer_row)
    if not td_node_id:
        _remember_route(peer_row, None)
        return False
    overlay = td_overlay()
    conn = overlay.connect(node_id=td_node_id, service=overlay.SERVICE)
    _remember_route(peer_row, conn)
    if not conn:
        db.mark_peer_server_error(peer_row["id"], "no TransferDaemon route to this peer (using its direct address)")
    return bool(conn)


async def refresh_routes() -> dict:
    """Re-resolve every linked peer's overlay endpoint, in a thread (the overlay calls are synchronous).
    Run by the heartbeat, so a route that moved is picked up on its own and nothing has to be re-linked."""
    def refresh_all() -> dict:
        return {r["name"]: refresh_route(r) for r in db.list_peer_servers()}
    try:
        return await asyncio.to_thread(refresh_all)
    except Exception:  # noqa: BLE001
        logger.exception("could not refresh the TransferDaemon routes")
        return {}


def td_identity(name: str = "", base_url: Optional[str] = None) -> dict:
    """This machine's TransferDaemon identity for a handshake: its overlay node id, plus a fresh invite
    for the other side to pair with. Empty when TransferDaemon is not running here — pairing then works
    exactly as it did before, directly.

    Publishing our own dashboard on the overlay (expose, paired nodes only) is part of it: the point is
    that a linked node can reach us without a forwarded port or a firewall rule."""
    overlay = td_overlay()
    try:
        if not overlay.available():
            return {}
        addr = base_url or detect_own_base_url() or ""
        if addr:
            overlay.expose(addr)
        invite = overlay.invite_create(overlay.INVITE_TTL_S) or {}
    except Exception:  # noqa: BLE001  (a TD that is there but unwell must never block pairing)
        logger.info("could not prepare this machine's TransferDaemon identity", exc_info=True)
        return {}
    return {"node_id": invite.get("node_id") or overlay.node_id(), "invite": invite.get("invite") or "",
            "name": name, "ttl_s": invite.get("ttl_s", overlay.INVITE_TTL_S)}


def generate_pairing_token(base_url: Optional[str] = None, name: str = "") -> dict:
    """Runs on the receiving side, one DASHBOARD_TOKEN-gated local action,
    one click (see the module docstring's step 0). `base_url` is THIS
    server's own reachable address, baked into the returned token so the
    other side never needs it separately — auto-detected via
    detect_own_base_url() when not given, so nobody has to look up or type
    an address at all in the normal case. An explicit `base_url` still
    works, for the one real edge case auto-detection can't cover (a
    reverse proxy, port forwarding, an address that differs from this
    machine's own outbound-facing IP).

    The token also carries this machine's TransferDaemon overlay invite, when it has one, so linking in
    pairs the two TransferDaemon daemons in the same single step and the handshake travels over the
    overlay — no firewall rule, no forwarded port, and the same link from two different networks. The
    result is what the admin shares with whoever is linking in — never the real dashboard token."""
    if base_url:
        base_url = normalize_base_url(base_url)
    else:
        base_url = detect_own_base_url()
        if not base_url:
            raise PeerError("couldn't auto-detect this server's address (no network route) — provide one manually")
    secret, expires_at = db.create_server_pairing_token()
    td = td_identity(name, base_url)
    return {"pairing_token": _encode_pairing_token(base_url, secret, td), "expires_at": expires_at, "base_url": base_url,
            "transferdaemon": td_state(td)}


def _safe_ssh_name(name: str) -> str:
    """A free-text peer name -> a filesystem/`Host` block-safe SSH Toolkit
    connection/keypair name."""
    slug = re.sub(r"[^A-Za-z0-9_-]+", "-", (name or "").strip()).strip("-").lower()
    return f"abp-peer-{slug}" if slug else "abp-peer-unnamed"


async def _setup_own_ssh(peer_name: str) -> dict:
    """Generates (or reuses) our own keypair for this peer relationship and
    returns what we send in our half of the handshake — never a failure
    the caller has to handle specially: SSH Toolkit being unavailable
    (no PowerShell, submodule not checked out) just means an empty result,
    same as it does everywhere else in bot/ssh_toolkit.py."""
    from bot import ssh_toolkit

    connection_name = _safe_ssh_name(peer_name)
    try:
        keypair = await ssh_toolkit.generate_keypair(connection_name)
    except ssh_toolkit.SshToolkitError as exc:
        logger.info("peer SSH auto-setup skipped (no SSH Toolkit): %s", exc)
        return {"connection_name": connection_name, "public_key": None, "identity_file": None}
    return {
        "connection_name": connection_name,
        "public_key": keypair["public_key"],
        "identity_file": keypair["identity_file"],
    }


async def _finish_ssh_setup(
    connection_name: str, identity_file: Optional[str], host: Optional[str],
    remote_public_key: Optional[str], remote_username: Optional[str],
) -> dict:
    """Trusts the other side's public key (so they can reach us) and
    registers a connection back to them (so we can reach them) — the two
    halves that only make sense once both sides of the handshake are known.
    Every step is independently best-effort: a partial result (e.g. we
    trust their key but don't know their username yet) is still reported,
    never silently discarded."""
    from bot import ssh_toolkit

    status: dict = {"trusted_remote_key": False, "connection_registered": False, "connection_name": connection_name}
    if remote_public_key:
        try:
            await ssh_toolkit.install_trusted_key(remote_public_key)
            status["trusted_remote_key"] = True
        except ssh_toolkit.SshToolkitError as exc:
            status["trust_error"] = str(exc)
    if identity_file and host and remote_username:
        try:
            await ssh_toolkit.add_connection(
                connection_name, host, user=remote_username, identity_file=identity_file, force=True,
            )
            status["connection_registered"] = True
        except ssh_toolkit.SshToolkitError as exc:
            status["connection_error"] = str(exc)
    return status


async def link_peer(
    name: str, pairing_token: str, my_name: str, my_base_url: Optional[str] = None, *, setup_ssh: bool = True,
) -> dict:
    """Runs on the initiating side. See module docstring for the full
    handshake, including the SSH auto-pairing and the TransferDaemon pairing
    riding along on it. Raises PeerError on any failure — the fresh credential
    minted for the (possibly unreachable) other side is revoked again so a
    failed link attempt never leaves a dangling, never-used api_keys row.

    TransferDaemon first: pairing the two daemons gives us an overlay endpoint
    for the other machine, and the handshake travels over that instead of the
    address baked into the token. That is what makes an unreachable advertised
    address — a 10.x.x.x behind NAT, a VPN, a different city — a non-event."""
    base_url, remote_pairing_token, remote_td = _decode_pairing_token(pairing_token)
    base_url = normalize_base_url(base_url)
    if my_base_url:
        my_base_url = normalize_base_url(my_base_url)

    route: dict[str, Any] = {}
    if remote_td.get("invite") or remote_td.get("node_id"):
        # Sync (blocking overlay calls) off the event loop, and best-effort throughout: a machine without
        # TransferDaemon, or with a daemon that is unwell, still links directly below.
        try:
            route = await asyncio.to_thread(_pair_over_td, name, my_name, my_base_url, remote_td)
        except Exception:  # noqa: BLE001
            logger.info("could not pair TransferDaemon with %s; linking directly", name, exc_info=True)
    handshake_url = route.get("local_addr") or base_url

    own_ssh = await _setup_own_ssh(name) if setup_ssh else {"connection_name": None, "public_key": None, "identity_file": None}

    inbound_key_id, inbound_plaintext = db.create_api_key(f"peer: {name}", kind="peer_server")
    payload = {
        "name": my_name, "base_url": my_base_url, "api_key": inbound_plaintext,
        "pairing_token": remote_pairing_token,
        "ssh_public_key": own_ssh["public_key"], "ssh_username": getpass.getuser() if setup_ssh else None,
        "transferdaemon": {"node_id": route.get("our_node_id", ""), "invite": route.get("our_invite", ""),
                           "name": my_name},
    }
    try:
        async with httpx.AsyncClient(timeout=HANDSHAKE_TIMEOUT_S) as client:
            resp = await _post_with_retry(client, f"{handshake_url}/api/peers/handshake", json=payload)
        if resp.status_code == 401:
            raise PeerError("that server rejected the pairing token — it may be wrong, expired (10 minutes), or already used")
        resp.raise_for_status()
        data = resp.json()
    except httpx.HTTPError as exc:
        db.revoke_api_key(inbound_key_id)
        raise PeerError(
            f"could not reach {handshake_url}: {exc}"
            + ("" if handshake_url != base_url else
               " — nothing was opened for you, and nothing needs to be: TransferDaemon is what carries a link "
               "across networks. With it running on both machines this goes over the overlay, so no firewall "
               "rule and no forwarded port are involved; a direct address only reaches machines that can already "
               "see each other's port.")
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
    remote_td_back = data.get("transferdaemon") or {}
    td_node_id = str(remote_td_back.get("node_id") or remote_td.get("node_id") or "")
    if td_node_id:
        db.set_peer_transport(peer_id, td_node_id=td_node_id)
    result = dict(db.get_peer_server(peer_id))
    # Whatever route we actually used is what the next call uses, so the heartbeat does not start over
    # from the advertised address (which may be the one that never worked).
    _remember_route(result, route or None)
    result["route"] = route_info(result)

    if setup_ssh:
        result["ssh_setup"] = await _finish_ssh_setup(
            own_ssh["connection_name"], own_ssh["identity_file"],
            urlparse(handshake_url).hostname, data.get("ssh_public_key"), data.get("ssh_username"),
        )
    return result


def _pair_over_td(name: str, my_name: str, my_base_url: Optional[str], remote_td: dict) -> dict:
    """Pair this machine's TransferDaemon with the one in the pairing token and get an endpoint for its
    published dashboard. Returns {} when there is no overlay to pair over — the caller then links directly,
    exactly as it always did."""
    overlay = td_overlay()
    if not overlay.available():
        return {}
    paired = None
    if remote_td.get("invite"):
        paired = overlay.invite_accept(str(remote_td["invite"]), name=name)
    node_id = str((paired or {}).get("node_id") or remote_td.get("node_id") or "")
    if not node_id:
        return {}
    ours = td_identity(my_name, my_base_url)
    conn = overlay.connect(node_id=node_id, service=overlay.SERVICE)
    if not conn:
        return {}
    return {"local_addr": conn["local_addr"], "route": conn.get("route") or {}, "node_id": node_id,
            "our_node_id": ours.get("node_id", ""), "our_invite": ours.get("invite", ""),
            "kind": (conn.get("route") or {}).get("kind") or "overlay"}


async def accept_handshake(
    name: str, api_key: str, base_url: Optional[str], my_name: str, pairing_token: str,
    *, ssh_public_key: Optional[str] = None, ssh_username: Optional[str] = None,
    transferdaemon: Optional[dict] = None,
) -> dict:
    """Runs on the receiving side, inside the /api/peers/handshake route —
    which deliberately does NOT require DASHBOARD_TOKEN. Auth here is the
    pairing_token instead: it's checked and atomically consumed first,
    before anything else in the payload is trusted, so a wrong/expired/
    reused token rejects the whole request regardless of what else was
    sent. Mints our own credential for the caller to store and records
    them as a peer using the credential they sent us — and, the same
    best-effort way link_peer's own side does it, trusts the initiator's
    SSH public key and registers a connection back to them.

    TransferDaemon pairs in the same single step: the initiator's overlay invite arrives in the payload and
    is accepted here, our dashboard is published on the overlay to paired nodes only, and we hand back our
    own overlay node id so the initiator can address us by it. Every step is best-effort — an initiator
    without TransferDaemon still links, exactly as before."""
    if not db.consume_server_pairing_token(pairing_token):
        raise PeerError("invalid, expired, or already-used pairing token")
    name = (name or "unnamed server").strip() or "unnamed server"
    if not api_key:
        raise PeerError("handshake payload missing api_key")
    normalized_base_url = normalize_base_url(base_url) if base_url else ""
    inbound_key_id, inbound_plaintext = db.create_api_key(f"peer: {name}", kind="peer_server")
    peer_id = db.create_peer_server(name, normalized_base_url, api_key, inbound_key_id)
    db.mark_peer_server_ok(peer_id)

    td = await asyncio.to_thread(_accept_td, name, my_name, transferdaemon or {}) if transferdaemon else {}
    if td.get("node_id"):
        db.set_peer_transport(peer_id, td_node_id=str(td["node_id"]))

    own_ssh = await _setup_own_ssh(name)
    if own_ssh["public_key"]:
        await _finish_ssh_setup(
            own_ssh["connection_name"], own_ssh["identity_file"],
            urlparse(normalized_base_url).hostname if normalized_base_url else None,
            ssh_public_key, ssh_username,
        )
    result = {
        "api_key": inbound_plaintext, "name": my_name,
        "ssh_public_key": own_ssh["public_key"], "ssh_username": getpass.getuser(),
    }
    if td:
        result["transferdaemon"] = {"node_id": td.get("our_node_id", ""), "invite": td.get("our_invite", "")}
    return result


def _accept_td(peer_name: str, my_name: str, payload: dict) -> dict:
    """Pair with the initiator's TransferDaemon, publish our own dashboard on the overlay, and connect back
    to theirs. {} when either side has no overlay."""
    overlay = td_overlay()
    if not overlay.available():
        return {}
    invite = str(payload.get("invite") or "")
    if invite:
        overlay.invite_accept(invite, name=peer_name)
    ours = td_identity(my_name)
    conn = overlay.connect(node_id=str(payload.get("node_id") or ""), service=overlay.SERVICE) if payload.get("node_id") else None
    return {"node_id": str(payload.get("node_id") or ""), "our_node_id": ours.get("node_id", ""),
            "our_invite": ours.get("invite", ""), "local_addr": (conn or {}).get("local_addr", "")}


def unlink_peer(peer_id: int) -> Optional[dict]:
    row = db.delete_peer_server(peer_id)
    return dict(row) if row is not None else None


async def _proxy_get(peer_row, path: str) -> dict:
    # GET is safe to retry (idempotent) — a flaky LAN or a peer mid-restart
    # shouldn't turn into a hard failure the admin has to notice and retry
    # by hand. POST (run_bot_action) deliberately does NOT retry: a dropped
    # response after a "restart" actually landed would otherwise risk
    # restarting the target bot twice.
    url = peer_base_url(peer_row)
    try:
        async with httpx.AsyncClient(timeout=PROXY_TIMEOUT_S) as client:
            last_exc: Optional[Exception] = None
            for _attempt, delay in enumerate((0.0, *_RETRY_DELAYS_S)):
                if delay:
                    await asyncio.sleep(delay)
                try:
                    resp = await client.get(
                        f"{url}{path}",
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
                f"{peer_base_url(peer_row)}{path}",
                headers={"X-Dashboard-Token": peer_row["outbound_api_key"]},
                json=json_body,
            )
        resp.raise_for_status()
        db.mark_peer_server_ok(peer_row["id"])
        return resp.json()
    except httpx.HTTPError as exc:
        db.mark_peer_server_error(peer_row["id"], str(exc))
        raise PeerError(str(exc)) from exc


_NO_ADDRESS = ("this peer never shared a reachable address — it can call us, but we can't call it back. "
               "With TransferDaemon on both machines the overlay route is used instead, which needs no address "
               "at all.")


async def fetch_overview(peer_row) -> dict:
    if not peer_base_url(peer_row):
        raise PeerError(_NO_ADDRESS)
    return await _proxy_get(peer_row, "/api/overview")


async def fetch_bots(peer_row) -> list:
    if not peer_base_url(peer_row):
        raise PeerError(_NO_ADDRESS)
    return await _proxy_get(peer_row, "/api/bots")


_BOT_ACTIONS = {"start", "stop", "restart", "enable", "disable"}


async def run_bot_action(peer_row, instance_id: int, action: str) -> dict:
    if action not in _BOT_ACTIONS:
        raise PeerError(f"unknown action {action!r}")
    if not peer_base_url(peer_row):
        raise PeerError(_NO_ADDRESS)
    return await _proxy_post(peer_row, f"/api/bots/{instance_id}/{action}")


async def send_file(peer_row, path, timeout: float = 3600.0) -> dict:
    """Hand a file to a peer with TransferDaemon's own transfer instead of an HTTP body: a big cluster result
    or a job input moves at the platform's speed and is checked as it goes. Returns the transfer; raises
    PeerError when this peer has no overlay identity, so the caller can fall back to its own transport."""
    td_node_id = _td_node_id(peer_row)
    if not td_node_id:
        raise PeerError("this peer has no TransferDaemon identity, so there is no file transfer channel to it")
    overlay = td_overlay()
    try:
        return await asyncio.to_thread(overlay.send_file, path, td_node_id)
    except DaemonError as exc:
        raise PeerError(f"TransferDaemon could not take {path} for {peer_row['name']}: {exc}") from exc


HEALTH_CHECK_INTERVAL_S = 60


async def health_check_all() -> None:
    """Re-resolves every peer's TransferDaemon route, then pings every linked peer it has an address for and
    records whether it answered — the same db.mark_peer_server_ok/_error a real proxied call already triggers,
    just run proactively instead of only ever updating when an admin happens to click into a peer. Without
    this, the dashboard's Online/Unreachable status only reflects the last time someone opened "Manage bots"
    for that server, which could be stale for hours after a link actually broke.

    Refreshing the route first is what makes a route change (a relay took over, a direct path came back, the
    other machine moved) take effect on its own, with nothing to re-link."""
    await refresh_routes()
    for row in db.list_peer_servers():
        if not peer_base_url(row):
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
    await ensure_transport()
    while not stop_event.is_set():
        try:
            await health_check_all()
        except Exception:
            logger.exception("peer health check pass failed")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=HEALTH_CHECK_INTERVAL_S)
        except asyncio.TimeoutError:
            pass


def ensure_transport() -> dict:
    """Starting ABP starts TransferDaemon's daemon when it is installed, so the overlay is already up by the
    time anything needs it. Installs nothing and starts nothing that isn't there: a machine without
    TransferDaemon keeps linking directly, exactly as before."""
    try:
        return td_overlay().ensure_running()
    except Exception:  # noqa: BLE001
        logger.info("could not start TransferDaemon", exc_info=True)
        return {"running": False}


# ---- remote control of a linked server's modules ----------------------------------------------------------------------
# A linked peer's key reaches only monitoring and bot lifecycle by default. The owner of a machine can let linked peers
# control more of it, area by area, with `peers.remote_control` in its config/backends.yaml:
#
#     peers:
#       remote_control: [vm-harness, hermes-manager, transferdaemon, power, modules]
#
# (modules: every module on the Modules page, /api/modules/: install, update, build, hubs, operations.)
#
# Each area is a prefix of this machine's own API that a peer may then call (reads and changes). The machine doing the
# controlling needs nothing extra: proxy() below sends the request with the key the handshake gave it.
CONTROL_AREAS: dict[str, str] = {
    "vm-harness": "/api/vm-harness/",
    "hermes-manager": "/api/hermes-manager/",
    "transferdaemon": "/api/transferdaemon/",
    "power": "/api/power/",
    "modules": "/api/modules/",
}
CONTROL_TIMEOUT_S = 900
# The cluster's routes (bot/cluster/): any linked peer may call them; what it may *do* is decided by the target
# machine's own cluster offer (off until its owner turns sharing on), not by remote_control areas.
CLUSTER_PREFIX = "/api/cluster/"


def allowed_control_areas() -> set[str]:
    """The areas this machine lets linked peers control (its own setting, off by default)."""
    from bot.config import config
    raw = ((config.current or {}).get("peers") or {}).get("remote_control") or []
    return {str(a) for a in raw if str(a) in CONTROL_AREAS}


def control_area_of(path: str) -> Optional[str]:
    """The area a path belongs to: under its prefix, or the prefix's own root (/api/modules, with a query or not)."""
    bare = path.split("?", 1)[0]
    return next((a for a, prefix in CONTROL_AREAS.items() if path.startswith(prefix) or bare == prefix.rstrip("/")),
                None)


def find_peer(ref) -> Any:
    """A linked peer by id, by name (case-insensitive), or by its TransferDaemon node id — peers are
    addressed by overlay node id once the overlay is the route, so that has to resolve too."""
    rows = db.list_peer_servers()
    for r in rows:
        if str(r["id"]) == str(ref) or str(r["name"]).lower() == str(ref).lower():
            return r
    hits = [r for r in rows if _td_node_id(r) and _td_node_id(r) == str(ref)]
    if len(hits) == 1:
        return hits[0]
    names = ", ".join(r["name"] for r in rows) or "none"
    raise PeerError(f"no linked server {ref!r} (linked: {names})")


async def proxy(peer_row, method: str, path: str, body: Any = None, timeout: float = CONTROL_TIMEOUT_S,
                wake: bool = True) -> Any:
    """Call one of a linked peer's module APIs (only the CONTROL_AREAS prefixes, and the cluster's own routes) and
    return its JSON answer. The peer decides whether it allows it (its own peers.remote_control, or for the cluster
    its offer); a refusal comes back as a PeerError saying so. wake=False never wakes a sleeping peer (the cluster's
    heartbeat must not keep machines awake)."""
    method = method.upper()
    if method not in ("GET", "POST", "PUT", "DELETE"):
        raise PeerError(f"method {method} is not allowed")
    if (control_area_of(path) is None and not path.startswith(CLUSTER_PREFIX)) or ".." in path:
        raise PeerError("only " + ", ".join([*CONTROL_AREAS.values(), CLUSTER_PREFIX]) + " can be reached on a linked server")
    if not peer_base_url(peer_row):
        raise PeerError(_NO_ADDRESS)
    async def send() -> httpx.Response:
        async with httpx.AsyncClient(timeout=timeout) as client:
            return await client.request(method, f"{peer_base_url(peer_row)}{path}", json=body,
                                        headers={"X-Dashboard-Token": peer_row["outbound_api_key"]})

    try:
        resp = await send()
    except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
        # Asleep? Wake it (if we know its card and auto_wake is on) and try once more.
        from bot import power
        if wake and power.settings()["auto_wake"] and await power.wake_and_wait(peer_row):
            try:
                resp = await send()
            except httpx.HTTPError as exc2:
                db.mark_peer_server_error(peer_row["id"], str(exc2))
                raise PeerError(f"{peer_row['name']} woke up but did not answer: {exc2}") from exc2
        else:
            db.mark_peer_server_error(peer_row["id"], str(exc))
            raise PeerError(f"{peer_row['name']} is not reachable: {exc}") from exc
    except httpx.HTTPError as exc:
        db.mark_peer_server_error(peer_row["id"], str(exc))
        raise PeerError(f"{peer_row['name']} is not reachable: {exc}") from exc
    db.mark_peer_server_ok(peer_row["id"])
    try:
        data = resp.json()
    except ValueError:
        data = {"text": resp.text}
    if resp.status_code >= 400:
        detail = data.get("detail") if isinstance(data, dict) else data
        if isinstance(detail, dict):
            detail = detail.get("error") or detail
        raise PeerError(f"{peer_row['name']}: {detail} (HTTP {resp.status_code})")
    return data


async def on_machine(machine: str, method: str, path: str, body: Any = None, timeout: float = CONTROL_TIMEOUT_S) -> Any:
    """proxy() by the linked server's name or id: what agent tools use for their `machine` argument."""
    return await proxy(find_peer(machine), method, path, body, timeout)


def any_linked() -> bool:
    try:
        return bool(db.list_peer_servers())
    except Exception:  # noqa: BLE001
        return False
