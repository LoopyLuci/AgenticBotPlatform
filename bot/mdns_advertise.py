"""Advertises this BotServer install on the local network via mDNS/DNS-SD
(`_botserver._tcp.local.`) so the Android app's NsdDiscoveryClient can find
a live server without any stored IP — the server-side half of hardening
mobile connectivity for "any network, any condition": when a phone's
configured host(s) stop answering (a DHCP lease changed the LAN IP, a
Tailscale hostname stopped resolving), it can re-discover the server fresh
as long as both are on the same local network right now.

Best-effort only, matching bot/hotreload.py's own failure stance: mDNS
needs a working multicast-capable network stack, which isn't guaranteed in
every environment (some containers/CI runners, some VPN configurations).
Any failure here is logged and swallowed — this is observability/discovery
sugar layered on top of the dashboard's own HTTP server, never a
dependency the app's actual startup relies on.
"""

from __future__ import annotations

import logging
import os
import socket
from typing import Optional

logger = logging.getLogger(__name__)

SERVICE_TYPE = "_botserver._tcp.local."

_zeroconf = None
_service_info = None


def start(port: Optional[int] = None) -> None:
    """Registers the mDNS advertisement. Safe to call more than once (a
    no-op if already running) and safe to call in an environment with no
    usable LAN address or a broken multicast stack — logs and returns
    rather than raising."""
    global _zeroconf, _service_info
    if _zeroconf is not None:
        return
    try:
        from zeroconf import ServiceInfo, Zeroconf

        from bot import network_info

        resolved_port = port or int(os.environ.get("DASHBOARD_PORT", "8787"))
        lan_ip = network_info.detect_addresses().get("lan")
        if not lan_ip:
            logger.info("mdns_advertise: no LAN address detected — skipping mDNS advertisement")
            return

        hostname = socket.gethostname().split(".")[0] or "botserver"
        service_name = f"BotServer on {hostname}.{SERVICE_TYPE}"
        info = ServiceInfo(
            SERVICE_TYPE,
            service_name,
            addresses=[socket.inet_aton(lan_ip)],
            port=resolved_port,
        )
        zc = Zeroconf()
        zc.register_service(info)
        _zeroconf = zc
        _service_info = info
        logger.info("mdns_advertise: advertising %r at %s:%s", service_name, lan_ip, resolved_port)
    except Exception as exc:
        logger.warning("mdns_advertise: failed to start — continuing without it: %s", exc)
        _zeroconf = None
        _service_info = None


def stop() -> None:
    """Unregisters and closes the mDNS advertisement, if running. Safe to
    call even if start() was never called or already failed."""
    global _zeroconf, _service_info
    if _zeroconf is None:
        return
    zc = _zeroconf
    info = _service_info
    _zeroconf = None
    _service_info = None
    try:
        if info is not None:
            zc.unregister_service(info)
        zc.close()
    except Exception as exc:
        logger.warning("mdns_advertise: error during shutdown — ignored: %s", exc)
