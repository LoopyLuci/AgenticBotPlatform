"""Detects this machine's own LAN, Tailscale, and public-Funnel addresses —
used to auto-fill the "host"/"host2"/"host3" fields when a mobile pairing
key is generated (see bot/dashboard/server.py's api_mobile_keys_create())
instead of making the operator hand-type any of them. Mirrors the same
address-detection concept the desktop Tauri app's own network.rs
(detect_lan_host/detect_tailscale_host) already implements for its own
Mobile tab, and the same private-network classification android-app's
PrivateNetworkGuard.kt uses on the client side:

- Tailscale's CGNAT range: 100.64.0.0/10
- Private LAN ranges: 10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16
- Funnel: Tailscale's own public-internet relay — a stable
  `https://<host>.<tailnet>.ts.net` URL that works from any network
  (cellular, a stranger's Wi-Fi, behind a proxy) with no port-forwarding
  or VPN client required on the far end, since it's plain HTTPS on 443.
  Detected via the `tailscale` CLI, which is already installed and
  running on this machine (same binary "tailscale status" already
  confirmed reachable) — never assumed just because Tailscale itself is
  up, since Funnel must be separately enabled per-node/per-port.

Deliberately conservative: never raises, never returns a loopback/link-
local/public LAN address — good enough for "give the operator/app a real,
reachable starting point," not a general-purpose network topology tool.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import shutil
import socket
import subprocess
from typing import Optional

logger = logging.getLogger(__name__)

_TAILSCALE_BIN = shutil.which("tailscale") or r"C:\Program Files\Tailscale\tailscale.exe"

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


def _run_tailscale_json(*args: str) -> Optional[dict]:
    """Runs `tailscale <args> --json` and parses the result — None on any
    failure (binary missing, daemon not running, timeout, bad JSON). A
    short timeout matters here: this is called from an HTTP request
    handler's executor thread, and a hung subprocess must never turn into
    a hung pairing-key request."""
    try:
        proc = subprocess.run(
            [_TAILSCALE_BIN, *args, "--json"],
            capture_output=True, timeout=3, text=True, check=False,
        )
        if proc.returncode != 0 or not proc.stdout.strip():
            return None
        return json.loads(proc.stdout)
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
        logger.info("network_info: tailscale %s failed: %s", " ".join(args), exc)
        return None


def detect_funnel_url() -> Optional[str]:
    """The public https://<host>.<tailnet>.ts.net URL, if Tailscale Funnel
    is actually turned on right now — not just because Tailscale itself is
    running. Not matched against a specific local port: Funnel's own JSON
    config keys are the *public-facing* host:port (almost always ":443"),
    not the local backend port it proxies to internally, so a strict port
    match here would be brittle against Tailscale's own JSON shape. This
    machine only ever runs one Funnel target, so "AllowFunnel is non-empty"
    is a reliable enough signal in practice; None if Funnel isn't
    configured at all or the CLI isn't reachable."""
    funnel = _run_tailscale_json("funnel", "status")
    if not funnel or not funnel.get("AllowFunnel"):
        return None
    status = _run_tailscale_json("status")
    dns_name = ((status or {}).get("Self") or {}).get("DNSName")
    if not dns_name:
        return None
    return f"https://{dns_name.rstrip('.')}"


def detect_addresses() -> dict[str, Optional[str]]:
    """{"lan": "192.168.x.x"|None, "tailscale": "100.x.x.x"|None} — never
    raises; a value is None when nothing usable was found (e.g. no network
    connection, or no Tailscale interface up). Funnel's URL is looked up
    separately (detect_funnel_url) since it needs the port and returns a
    full URL rather than a bare host."""
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
