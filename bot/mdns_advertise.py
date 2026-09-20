"""Advertises this AgenticBotPlatform install on the local network via mDNS/DNS-SD
(`_agenticbot._tcp.local.`) so the Android app's NsdDiscoveryClient can find
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

# DNS-SD service type labels are conventionally capped at 15 bytes
# (zeroconf enforces this and raises if violated) — the project's full
# "agenticbotplatform" name is 19 bytes, so this uses the shorter
# "agenticbot" label instead. Confirmed live: the rename from "botserver"
# (fit fine) to "agenticbotplatform" broke this outright, silently
# disabling mDNS discovery/mobile pairing on every install ("mdns_advertise:
# failed to start ... Service name (agenticbotplatform) must be <= 15
# bytes") until this was caught. Must match Android's
# NsdDiscoveryClient.SERVICE_TYPE exactly.
SERVICE_TYPE = "_agenticbot._tcp.local."

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
    # A throwaway instance (the release pipeline's bundle smoke test, an
    # isolated test run) must not announce itself to phones on the LAN — a
    # stale advertisement of a dead instance is exactly what confuses them.
    if os.environ.get("ABP_DISABLE_MDNS", "").strip().lower() in ("1", "true", "yes", "on"):
        logger.info("mdns_advertise: disabled by ABP_DISABLE_MDNS")
        return
    try:
        from zeroconf import ServiceInfo, Zeroconf

        from bot import network_info

        resolved_port = port or int(os.environ.get("DASHBOARD_PORT", "8787"))
        lan_ip = network_info.detect_addresses().get("lan")
        if not lan_ip:
            logger.info("mdns_advertise: no LAN address detected — skipping mDNS advertisement")
            return

        hostname = socket.gethostname().split(".")[0] or "agenticbotplatform"
        service_name = f"AgenticBotPlatform on {hostname}.{SERVICE_TYPE}"
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
