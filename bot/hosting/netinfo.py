"""This machine on the network, as hosting needs it: its public addresses, whether it sits behind NAT or carrier-grade
NAT (where forwarding a port cannot work and a tunnel is the way), DNS as the world sees it (DNS over HTTPS, not this
machine's cache or hosts file), and whether a port answers.
"""
from __future__ import annotations

import ipaddress
import socket
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

import httpx

from bot.hosting.store import HostingError

_V4_SOURCES = ("https://api.ipify.org", "https://ipv4.icanhazip.com", "https://v4.ident.me")
_V6_SOURCES = ("https://api6.ipify.org", "https://ipv6.icanhazip.com", "https://v6.ident.me")
_DOH = ("https://cloudflare-dns.com/dns-query", "https://dns.google/resolve")
_TYPES = {"A": 1, "NS": 2, "CNAME": 5, "SOA": 6, "MX": 15, "TXT": 16, "AAAA": 28, "SRV": 33, "CAA": 257}
_cache: dict[str, tuple[float, Optional[str]]] = {}


def _ask(urls: tuple[str, ...], family: int) -> Optional[str]:
    for url in urls:
        try:
            r = httpx.get(url, timeout=6.0)
            ip = r.text.strip()
            addr = ipaddress.ip_address(ip)
            if addr.version == family:
                return ip
        except (httpx.HTTPError, ValueError):
            continue
    return None


def public_ip(version: int = 4, max_age_s: float = 120.0) -> Optional[str]:
    """This machine's address as the internet sees it (None if there is no route of that IP version)."""
    key = f"v{version}"
    hit = _cache.get(key)
    if hit and time.monotonic() - hit[0] < max_age_s:
        return hit[1]
    ip = _ask(_V4_SOURCES if version == 4 else _V6_SOURCES, version)
    _cache[key] = (time.monotonic(), ip)
    return ip


def lan_ip() -> Optional[str]:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 9))     # TEST-NET: no packet is sent, the OS just picks the outgoing interface
            return s.getsockname()[0]
    except OSError:
        return None


def classify(ip: Optional[str]) -> str:
    if not ip:
        return "none"
    a = ipaddress.ip_address(ip)
    if a in ipaddress.ip_network("100.64.0.0/10"):
        return "cgnat"
    if a.is_private:
        return "private"
    if a.is_loopback or a.is_link_local:
        return "local"
    return "public"


def overview(router: bool = True) -> dict:
    """Public v4/v6, LAN address, the router's own WAN address (UPnP), and what that means for hosting from here."""
    with ThreadPoolExecutor(3) as ex:
        f4, f6 = ex.submit(public_ip, 4), ex.submit(public_ip, 6)
        fr = None
        if router:
            from bot.hosting import upnp
            fr = ex.submit(upnp.external_ip_safe)
        v4, v6 = f4.result(), f6.result()
        wan = fr.result() if fr else None
    lan = lan_ip()
    out = {"public_ipv4": v4, "public_ipv6": v6, "lan_ip": lan, "router_wan_ip": wan, "router_upnp": wan is not None}
    if not v4 and not v6:
        verdict = ("offline", "No internet connection was found.")
    elif lan and v4 and lan == v4:
        verdict = ("direct", "This machine has a public IPv4 address itself: open the port in its firewall and point DNS at it.")
    elif wan and classify(wan) in ("cgnat", "private"):
        verdict = ("cgnat", f"The router's own internet address ({wan}) is not public: the provider uses carrier-grade NAT, "
                   "so forwarding a port cannot reach this machine. Use a tunnel (Cloudflare Tunnel or Tailscale Funnel)"
                   + (", or IPv6, which this machine has" if v6 else "") + ".")
    elif wan and v4 and wan != v4:
        verdict = ("double-nat", f"The router reports {wan} but the internet sees {v4}: there is another router or NAT in "
                   "between. A forward on this router alone will not be enough; a tunnel always works.")
    else:
        verdict = ("nat", "Behind a home router: forward ports 80 and 443 to this machine (ABP can ask the router through "
                   "UPnP" + ("" if wan else ", which it does not answer here — forward them in the router's own page") +
                   "), or use a tunnel and open nothing.")
    out["situation"], out["advice"] = verdict
    return out


def resolve(name: str, rtype: str = "A", timeout: float = 8.0) -> list[str]:
    """The record values the world sees for name/rtype: ABP's resolver first (Ironroot, then Quad9; bot/resolver.py),
    then DNS over HTTPS through Cloudflare and Google."""
    rtype = rtype.upper()
    want = _TYPES.get(rtype)
    if not want:
        raise HostingError(f"cannot look up {rtype} records")
    last: Optional[Exception] = None
    from bot import resolver                       # Ironroot on this machine, then Quad9 (both validate DNSSEC)
    try:
        return resolver.resolve(name, rtype).values
    except resolver.ResolveError as e:
        if resolver.settings()["require_validated"]:
            raise HostingError(str(e)) from e
        last = e
    for url in _DOH:
        try:
            r = httpx.get(url, params={"name": name, "type": rtype}, headers={"accept": "application/dns-json"}, timeout=timeout)
            r.raise_for_status()
            j = r.json()
            vals = []
            for a in j.get("Answer") or []:
                if a.get("type") == want:
                    v = str(a.get("data", "")).rstrip(".")
                    vals.append(v[1:-1] if rtype == "TXT" and v.startswith('"') and v.endswith('"') else v)
            return vals
        except (httpx.HTTPError, ValueError) as e:
            last = e
    raise HostingError(f"DNS over HTTPS lookups failed: {last}")


def propagation(name: str, rtype: str, expect: list[str]) -> dict:
    """Whether both public resolvers already answer `expect` for name/rtype (a fresh record can take minutes)."""
    seen = {}
    for url in _DOH:
        try:
            r = httpx.get(url, params={"name": name, "type": rtype}, headers={"accept": "application/dns-json"}, timeout=8.0)
            want = _TYPES[rtype.upper()]
            seen[url.split("/")[2]] = sorted(str(a.get("data", "")).rstrip(".").strip('"') for a in (r.json().get("Answer") or []) if a.get("type") == want)
        except (httpx.HTTPError, ValueError) as e:
            seen[url.split("/")[2]] = [f"error: {e}"]
    ok = all(sorted(v.rstrip(".") for v in expect) == vals for vals in seen.values())
    return {"name": name, "type": rtype, "expect": expect, "seen": seen, "propagated": ok}


def port_open(host: str, port: int, timeout: float = 4.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def http_probe(url: str, timeout: float = 10.0, verify: bool = True) -> dict:
    """Fetch a URL the way a visitor would: status, final URL after redirects, TLS validity, time taken."""
    t0 = time.monotonic()
    try:
        r = httpx.get(url, timeout=timeout, follow_redirects=True, verify=verify)
        return {"url": url, "ok": r.status_code < 400, "status": r.status_code, "final_url": str(r.url),
                "ms": int((time.monotonic() - t0) * 1000), "server": r.headers.get("server", ""), "tls": url.startswith("https") and verify}
    except httpx.HTTPError as e:
        msg = str(e) or type(e).__name__
        if verify and url.startswith("https") and ("CERTIFICATE" in msg.upper() or "SSL" in msg.upper()):
            alt = http_probe(url, timeout, verify=False)
            alt.update(ok=False, tls=False, error=f"the certificate is not valid: {msg}")
            return alt
        return {"url": url, "ok": False, "error": msg, "ms": int((time.monotonic() - t0) * 1000)}
