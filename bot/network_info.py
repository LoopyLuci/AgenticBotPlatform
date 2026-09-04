"""Detects this machine's own LAN and Tailscale IPv4 addresses — used to
auto-fill the "host"/"host2" fields when a mobile pairing key is generated
(see bot/dashboard/server.py's api_mobile_keys_create()) instead of making
the operator hand-type both. Mirrors the same two-address concept the
desktop Tauri app's own network.rs (detect_lan_host/detect_tailscale_host)
already implements for its own Mobile tab, and the same private-network
classification android-app's PrivateNetworkGuard.kt uses on the client
side:

- Tailscale's CGNAT range: 100.64.0.0/10
- Private LAN ranges: 10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16

Deliberately conservative: never raises, never returns a loopback/link-
local/public address — good enough for "give the operator a real,
reachable starting point," not a general-purpose network topology tool.
"""

from __future__ import annotations

import ipaddress
import logging
import socket
from typing import Optional

logger = logging.getLogger(__name__)

_TAILSCALE_NET = ipaddress.ip_network("100.64.0.0/10")
_PRIVATE_LAN_NETS = (
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
)


def _classify(addr: str) -> Optional[str]:
    """Returns "tailscale", "lan", or None (loopback/link-local/public/
    unparseable) for one IPv4 address string."""
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return None
    if ip.version != 4 or ip.is_loopback or ip.is_link_local:
        return None
    if ip in _TAILSCALE_NET:
        return "tailscale"
    if any(ip in net for net in _PRIVATE_LAN_NETS):
        return "lan"
    return None


def _default_route_lan_ip() -> Optional[str]:
    """The LAN IP this machine would use to originate a connection — found
    via the same zero-packets-sent trick the desktop app's Rust counterpart
    (network.rs's detect_lan_host) already uses: "connecting" a UDP socket
    just asks the OS to pick a real outbound route/local address; nothing
    is actually transmitted to 8.8.8.8. Far more reliable than enumerating
    every interface and guessing by name which one is "real" (a machine can
    have several virtual adapters — Hyper-V, WSL, Docker, VPN — handing out
    addresses in the exact same private ranges) — the OS routing table
    already knows which one it would actually use."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return None
    finally:
        s.close()


def detect_addresses() -> dict[str, Optional[str]]:
    """{"lan": "192.168.x.x"|None, "tailscale": "100.x.x.x"|None} — never
    raises; a value is None when nothing usable was found (e.g. no network
    connection, or no Tailscale interface up)."""
    result: dict[str, Optional[str]] = {"lan": None, "tailscale": None}

    try:
        default_ip = _default_route_lan_ip()
        if default_ip and _classify(default_ip) == "lan":
            result["lan"] = default_ip
    except Exception as exc:
        logger.warning("network_info.detect_addresses: default-route lookup failed: %s", exc)

    try:
        import psutil

        for addrs in psutil.net_if_addrs().values():
            for addr in addrs:
                if addr.family != socket.AF_INET:
                    continue
                kind = _classify(addr.address)
                if kind and result[kind] is None:
                    result[kind] = addr.address
    except Exception as exc:
        logger.warning("network_info.detect_addresses: failed to enumerate interfaces: %s", exc)

    return result
