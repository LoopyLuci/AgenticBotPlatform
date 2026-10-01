"""Ask the home router to forward a port to this machine (UPnP Internet Gateway Device), the standard library only.

    gw = upnp.discover()                     # the router's WAN service, or HostingError if none answers
    upnp.external_ip()                       # what the router says its internet address is
    upnp.add(443, 443, "TCP", "ABP web")     # forward the router's 443 to this machine's 443
    upnp.mappings()                          # every forward on the router (ABP's are described "ABP ...")
    upnp.remove(443, "TCP")

Many routers ship with UPnP off, and some ISPs' routers ignore it; then the forward has to be made in the router's
own page (Hosting shows the exact values) or a tunnel used instead. ABP only ever removes forwards it described as its
own, so a person's own forwards are left alone.
"""
from __future__ import annotations

import re
import socket
import time
import xml.etree.ElementTree as ET
from typing import Optional
from urllib.parse import urljoin

import httpx

from bot.hosting.store import HostingError

_SSDP = ("239.255.255.250", 1900)
_TARGETS = ("urn:schemas-upnp-org:device:InternetGatewayDevice:2", "urn:schemas-upnp-org:device:InternetGatewayDevice:1",
            "urn:schemas-upnp-org:service:WANIPConnection:1")
_SERVICES = ("urn:schemas-upnp-org:service:WANIPConnection:2", "urn:schemas-upnp-org:service:WANIPConnection:1",
             "urn:schemas-upnp-org:service:WANPPPConnection:1")
PREFIX = "ABP "
_found: dict = {}


def _search(timeout: float) -> list[str]:
    locations: list[str] = []
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP) as s:
        s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
        s.settimeout(0.5)
        for st in _TARGETS:
            msg = (f"M-SEARCH * HTTP/1.1\r\nHOST: 239.255.255.250:1900\r\nMAN: \"ssdp:discover\"\r\nMX: 2\r\nST: {st}\r\n\r\n").encode()
            s.sendto(msg, _SSDP)
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            try:
                data, _ = s.recvfrom(4096)
            except socket.timeout:
                continue
            except OSError:
                break
            m = re.search(rb"(?im)^location:\s*(\S+)", data)
            if m:
                loc = m.group(1).decode(errors="replace")
                if loc not in locations:
                    locations.append(loc)
    return locations


def discover(timeout: float = 3.0, refresh: bool = False) -> dict:
    """The router's WAN connection service: {"control_url", "service", "location", "lan_ip"} (cached for 10 minutes)."""
    if _found and not refresh and time.monotonic() - _found.get("at", 0) < 600:
        return _found
    for loc in _search(timeout):
        try:
            xml = httpx.get(loc, timeout=5.0).text
            root = ET.fromstring(xml)
        except (httpx.HTTPError, ET.ParseError):
            continue
        ns = {"d": "urn:schemas-upnp-org:device-1-0"}
        base = root.findtext("d:URLBase", default="", namespaces=ns) or loc
        for svc in root.iter("{urn:schemas-upnp-org:device-1-0}service"):
            stype = svc.findtext("d:serviceType", default="", namespaces=ns)
            if stype in _SERVICES:
                ctl = svc.findtext("d:controlURL", default="", namespaces=ns)
                from bot.hosting.netinfo import lan_ip
                _found.clear()
                _found.update(control_url=urljoin(base, ctl), service=stype, location=loc, lan_ip=lan_ip(), at=time.monotonic(),
                              model=root.findtext(".//d:modelName", default="", namespaces=ns),
                              maker=root.findtext(".//d:manufacturer", default="", namespaces=ns))
                return _found
    raise HostingError("no router answered UPnP on this network (it may be switched off in the router's settings)")


def _soap(action: str, args: dict, gw: Optional[dict] = None) -> dict:
    gw = gw or discover()
    body = "".join(f"<{k}>{v}</{k}>" for k, v in args.items())
    env = (f'<?xml version="1.0"?><s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
           f's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"><s:Body><u:{action} xmlns:u="{gw["service"]}">{body}'
           f"</u:{action}></s:Body></s:Envelope>")
    try:
        r = httpx.post(gw["control_url"], content=env.encode(), timeout=8.0,
                       headers={"Content-Type": 'text/xml; charset="utf-8"', "SOAPAction": f'"{gw["service"]}#{action}"'})
    except httpx.HTTPError as e:
        raise HostingError(f"the router did not answer {action}: {e}") from e
    if r.status_code >= 400:
        code = re.search(r"<errorCode>(\d+)</errorCode>", r.text)
        desc = re.search(r"<errorDescription>(.*?)</errorDescription>", r.text)
        raise HostingError(f"the router refused {action}: {desc.group(1) if desc else r.status_code}"
                           + (f" (UPnP error {code.group(1)})" if code else ""), ) from None
    out = {}
    try:
        for el in ET.fromstring(r.content).iter():
            tag = el.tag.split("}")[-1]
            if el.text is not None and not list(el):
                out[tag] = el.text
    except ET.ParseError:
        pass
    return out


def external_ip() -> str:
    return _soap("GetExternalIPAddress", {}).get("NewExternalIPAddress", "")


def external_ip_safe() -> Optional[str]:
    try:
        return external_ip() or None
    except HostingError:
        return None


def mappings(limit: int = 256) -> list[dict]:
    out = []
    gw = discover()
    for i in range(limit):
        try:
            m = _soap("GetGenericPortMappingEntry", {"NewPortMappingIndex": i}, gw)
        except HostingError:
            break
        out.append({"external_port": int(m.get("NewExternalPort", 0)), "protocol": m.get("NewProtocol"),
                    "internal_ip": m.get("NewInternalClient"), "internal_port": int(m.get("NewInternalPort", 0)),
                    "description": m.get("NewPortMappingDescription", ""), "enabled": m.get("NewEnabled") == "1",
                    "lease_s": int(m.get("NewLeaseDuration") or 0), "abp": (m.get("NewPortMappingDescription") or "").startswith(PREFIX)})
    return out


def add(external_port: int, internal_port: int, protocol: str = "TCP", description: str = "web",
        internal_ip: Optional[str] = None, lease_s: int = 0) -> dict:
    gw = discover()
    ip = internal_ip or gw.get("lan_ip")
    if not ip:
        raise HostingError("could not tell this machine's LAN address")
    protocol = protocol.upper()
    if protocol not in ("TCP", "UDP"):
        raise HostingError("the protocol is TCP or UDP")
    _soap("AddPortMapping", {"NewRemoteHost": "", "NewExternalPort": int(external_port), "NewProtocol": protocol,
                             "NewInternalPort": int(internal_port), "NewInternalClient": ip, "NewEnabled": 1,
                             "NewPortMappingDescription": (PREFIX + description)[:60], "NewLeaseDuration": int(lease_s)}, gw)
    return {"external_port": external_port, "internal_port": internal_port, "protocol": protocol, "internal_ip": ip}


def remove(external_port: int, protocol: str = "TCP", force: bool = False) -> bool:
    if not force:
        mine = [m for m in mappings() if m["external_port"] == external_port and m["protocol"] == protocol.upper()]
        if mine and not mine[0]["abp"]:
            raise HostingError(f"port {external_port}/{protocol} was forwarded by something else ({mine[0]['description']!r}); "
                               "ABP leaves it alone")
    _soap("DeletePortMapping", {"NewRemoteHost": "", "NewExternalPort": int(external_port), "NewProtocol": protocol.upper()})
    return True
