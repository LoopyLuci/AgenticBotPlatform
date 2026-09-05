"""Self-contained mobile pairing codes.

A pairing code is one string — "botserver://pair?host=...&host2=...
&host3=...&key=..." — that carries every host this machine is reachable
at (LAN, Tailscale-direct, a public Tailscale Funnel URL) alongside the
device's secret key, so pairing the Android app never requires typing a
host in separately: paste the one code, done. The Android app's
PairingRepository.parse() already understands this exact shape (it's
what the dashboard's QR always encoded) — this module just makes it the
thing a person can also copy/paste as plain text, from the dashboard's
"Key" field or a Support Bot reply, not only scan as a QR.

Shared by bot/dashboard/server.py's POST /api/mobile-keys route and
bot/support_bot/actions.py's mobile-key-creation intent, so both
produce byte-identical codes rather than two independently-maintained
URI builders drifting apart.
"""

from __future__ import annotations

import asyncio
import os
import urllib.parse


async def detect_hosts(host: str = "", host2: str = "", host3: str = "") -> tuple[str, str, str]:
    """Fills in whichever of host/host2/host3 the caller left blank with
    this machine's own detected LAN/Tailscale/Funnel addresses — see
    bot/network_info.py. Funnel is always the last candidate: it costs a
    relay hop, so it's only reached for once LAN/Tailscale-direct fail."""
    if host and host2 and host3:
        return host, host2, host3

    from bot import network_info

    loop = asyncio.get_running_loop()
    addrs, funnel_url = await asyncio.gather(
        loop.run_in_executor(None, network_info.detect_addresses),
        loop.run_in_executor(None, network_info.detect_funnel_url),
    )
    port = int(os.environ.get("DASHBOARD_PORT", "8787"))
    candidates = [f"{addrs[kind]}:{port}" for kind in ("lan", "tailscale") if addrs.get(kind)]
    if funnel_url:
        candidates.append(funnel_url)
    for candidate in candidates:
        if not host:
            host = candidate
        elif not host2 and candidate != host:
            host2 = candidate
        elif not host3 and candidate not in (host, host2):
            host3 = candidate
    return host, host2, host3


def build_pairing_code(key: str, host: str = "", host2: str = "", host3: str = "") -> str:
    """The single self-contained string the Android app's
    PairingRepository.parse() already understands — pasting this alone
    (no separate host entry, no QR needed) is enough to pair, since
    every reachable address is already baked in. host3 (Funnel) is a
    full https:// URL, unlike host/host2's bare host:port, so it's
    always URL-encoded rather than only when it happens to contain
    characters that would otherwise break the query string."""
    params = [f"key={urllib.parse.quote(key, safe='')}"]
    if host:
        params.insert(0, f"host={urllib.parse.quote(host, safe='')}")
    if host2:
        params.append(f"host2={urllib.parse.quote(host2, safe='')}")
    if host3:
        params.append(f"host3={urllib.parse.quote(host3, safe='')}")
    return "botserver://pair?" + "&".join(params)
