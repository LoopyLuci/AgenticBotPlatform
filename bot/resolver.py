"""ABP's DNS: answers that can be trusted, from a validating resolver on this machine (Ironroot) or Quad9.

    resolve(name, rtype)  -> Answer(values, validated, via, rcode)
        1. Ironroot on this machine (settings "ironroot", default 127.0.0.1:5350): a recursive resolver that checks
           DNSSEC from the root down and fails closed. Plain DNS over UDP (TCP when the answer is truncated).
        2. Quad9 over DNS-over-HTTPS (RFC 8484, https://dns.quad9.net/dns-query): validates DNSSEC and blocks known
           malicious domains.
        3. Only when require_validated is off: the system's own resolver (never marked validated).
    validated is the AD flag of the answer (DNSSEC checked by the resolver that answered). With require_validated on,
    an answer without it is refused (an unsigned zone then cannot be resolved at all: that is the point of the switch).

The query asks for DNSSEC (EDNS0 DO bit). Wire format is built and parsed here (A, AAAA, CNAME, TXT, MX, NS, SRV,
PTR, CAA; names with compression); no third-party DNS library. Settings: <data>/resolver.json, or the dashboard
(/api/resolver).
"""
from __future__ import annotations

import ipaddress
import json
import os
import random
import socket
import struct
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import httpx

TYPES = {"A": 1, "NS": 2, "CNAME": 5, "SOA": 6, "PTR": 12, "MX": 15, "TXT": 16, "AAAA": 28, "SRV": 33, "CAA": 257}
NAMES = {v: k for k, v in TYPES.items()}
QUAD9 = {"quad9": "https://dns.quad9.net/dns-query", "quad9-ecs": "https://dns11.quad9.net/dns-query",
         "quad9-unfiltered": "https://dns10.quad9.net/dns-query"}
DEFAULTS = {"ironroot": "127.0.0.1:5350", "upstreams": ["quad9"], "require_validated": False, "timeout_s": 4.0}


class ResolveError(Exception):
    pass


@dataclass
class Answer:
    name: str
    rtype: str
    values: list[str] = field(default_factory=list)
    validated: bool = False
    via: str = ""
    rcode: int = 0
    ttl: int = 0


def _path() -> Path:
    env = os.environ.get("ABP_RESOLVER_FILE", "").strip()
    if env:
        return Path(env)
    from bot.envfile import PROJECT_ROOT
    return PROJECT_ROOT / "data" / "resolver.json"


def settings() -> dict:
    try:
        return {**DEFAULTS, **json.loads(_path().read_text(encoding="utf-8"))}
    except (OSError, ValueError):
        return dict(DEFAULTS)


def set_settings(changes: dict) -> dict:
    bad = set(changes) - set(DEFAULTS)
    if bad:
        raise ValueError(f"unknown resolver setting(s): {', '.join(sorted(bad))}")
    for u in changes.get("upstreams") or []:
        if u not in QUAD9 and not str(u).startswith("https://"):
            raise ValueError(f"an upstream is one of {', '.join(QUAD9)} or a DoH https:// address")
    st = {**settings(), **changes}
    p = _path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(st, indent=1), encoding="utf-8")
    return st


# ---- wire format --------------------------------------------------------------------------------------------------- #

def _encode_name(name: str) -> bytes:
    out = b""
    for label in name.rstrip(".").split("."):
        if label:
            b = label.encode("idna") if not label.isascii() else label.encode()
            if len(b) > 63:
                raise ResolveError(f"a label of {name!r} is longer than 63 bytes")
            out += bytes([len(b)]) + b
    return out + b"\0"


def build_query(name: str, rtype: str, qid: Optional[int] = None) -> bytes:
    t = TYPES.get(rtype.upper())
    if t is None:
        raise ResolveError(f"cannot look up {rtype} records")
    qid = random.randrange(65536) if qid is None else qid
    header = struct.pack(">HHHHHH", qid, 0x0120, 1, 0, 0, 1)            # RD + AD (ask for the AD bit), one OPT record
    opt = b"\0" + struct.pack(">HHIH", 41, 1232, 0x00008000, 0)        # EDNS0: 1232-byte payload, DO bit
    return header + _encode_name(name) + struct.pack(">HH", t, 1) + opt


def _read_name(buf: bytes, i: int, depth: int = 0) -> tuple[str, int]:
    labels, jumped, end = [], False, i
    while True:
        if i >= len(buf) or depth > 30:
            raise ResolveError("a malformed name in the answer")
        n = buf[i]
        if n == 0:
            i += 1
            break
        if n & 0xC0 == 0xC0:
            ptr = ((n & 0x3F) << 8) | buf[i + 1]
            if not jumped:
                end = i + 2
            jumped = True
            i, depth = ptr, depth + 1
            continue
        labels.append(buf[i + 1: i + 1 + n].decode("ascii", "replace"))
        i += 1 + n
    return ".".join(labels), (end if jumped else i)


def _rdata(buf: bytes, t: int, start: int, length: int) -> str:
    d = buf[start: start + length]
    if t == 1:
        return str(ipaddress.IPv4Address(d))
    if t == 28:
        return str(ipaddress.IPv6Address(d))
    if t in (2, 5, 12):
        return _read_name(buf, start)[0]
    if t == 15:
        return f"{struct.unpack('>H', d[:2])[0]} {_read_name(buf, start + 2)[0]}"
    if t == 33:
        pri, weight, port = struct.unpack(">HHH", d[:6])
        return f"{pri} {weight} {port} {_read_name(buf, start + 6)[0]}"
    if t == 16:
        parts, j = [], 0
        while j < len(d):
            n = d[j]
            parts.append(d[j + 1: j + 1 + n].decode("utf-8", "replace"))
            j += 1 + n
        return "".join(parts)
    if t == 257:
        tag_len = d[1]
        return f"{d[0]} {d[2:2 + tag_len].decode()} \"{d[2 + tag_len:].decode('utf-8', 'replace')}\""
    return d.hex()


def parse_response(buf: bytes, name: str, rtype: str, qid: Optional[int] = None) -> Answer:
    if len(buf) < 12:
        raise ResolveError("a truncated DNS answer")
    rid, flags, qd, an, _ns, _ar = struct.unpack(">HHHHHH", buf[:12])
    if qid is not None and rid != qid:
        raise ResolveError("an answer to a different question")
    i = 12
    for _ in range(qd):
        _, i = _read_name(buf, i)
        i += 4
    want = TYPES[rtype.upper()]
    out = Answer(name=name.rstrip("."), rtype=rtype.upper(), validated=bool(flags & 0x0020), rcode=flags & 0x000F)
    ttls = []
    for _ in range(an):
        _, i = _read_name(buf, i)
        t, _cls, ttl, ln = struct.unpack(">HHIH", buf[i: i + 10])
        i += 10
        if t == want:
            out.values.append(_rdata(buf, t, i, ln).rstrip(".") if t != 16 else _rdata(buf, t, i, ln))
            ttls.append(ttl)
        i += ln
    out.ttl = min(ttls) if ttls else 0
    return out


def truncated(buf: bytes) -> bool:
    return len(buf) >= 4 and bool(struct.unpack(">H", buf[2:4])[0] & 0x0200)


# ---- transports ---------------------------------------------------------------------------------------------------- #

def _hostport(addr: str) -> tuple[str, int]:
    if addr.startswith("["):
        host, _, port = addr[1:].partition("]:")
    else:
        host, _, port = addr.rpartition(":")
    return host, int(port or 53)


def ask_dns(server: str, name: str, rtype: str, timeout: float = 4.0) -> Answer:
    """One question to a DNS server over UDP, again over TCP when the answer was truncated."""
    host, port = _hostport(server)
    qid = random.randrange(65536)
    q = build_query(name, rtype, qid)
    fam = socket.AF_INET6 if ":" in host else socket.AF_INET
    with socket.socket(fam, socket.SOCK_DGRAM) as s:
        s.settimeout(timeout)
        s.sendto(q, (host, port))
        data, _ = s.recvfrom(65535)
    if truncated(data):
        with socket.create_connection((host, port), timeout=timeout) as t:
            t.sendall(struct.pack(">H", len(q)) + q)
            n = struct.unpack(">H", _recv_exact(t, 2))[0]
            data = _recv_exact(t, n)
    a = parse_response(data, name, rtype, qid)
    a.via = f"ironroot@{server}"
    return a


def _recv_exact(s: socket.socket, n: int) -> bytes:
    out = b""
    while len(out) < n:
        chunk = s.recv(n - len(out))
        if not chunk:
            raise ResolveError("the DNS connection closed early")
        out += chunk
    return out


_clients: dict[str, httpx.Client] = {}


def _doh_client(timeout: float) -> httpx.Client:
    c = _clients.get("doh")
    if c is None:
        try:
            import h2  # noqa: F401  Quad9 answers DoH over HTTP/2 only (HTTP/1.1 gets 505)
            http2 = True
        except ImportError:
            http2 = False
        c = _clients["doh"] = httpx.Client(timeout=timeout, http2=http2)
    return c


def ask_doh(url: str, name: str, rtype: str, timeout: float = 4.0, transport: Optional[httpx.BaseTransport] = None) -> Answer:
    """One question over DNS-over-HTTPS (RFC 8484, POST application/dns-message)."""
    q = build_query(name, rtype, 0)                                     # RFC 8484: id 0 for cache-friendliness
    hdr = {"content-type": "application/dns-message", "accept": "application/dns-message"}
    if transport is not None:
        with httpx.Client(timeout=timeout, transport=transport) as c:
            r = c.post(url, content=q, headers=hdr)
    else:
        # One HTTP/2 connection, reused. Some networks reset a share of TLS handshakes (seen here: about 1 in 6 to both
        # Quad9 addresses), so a failed connection is redialled a few times before the next resolver is tried.
        for attempt in range(4):
            try:
                r = _doh_client(timeout).post(url, content=q, headers=hdr)
                break
            except (httpx.ConnectError, httpx.RemoteProtocolError, httpx.ReadError):
                _clients.pop("doh", None)
                if attempt == 3:
                    raise
                time.sleep(0.1 * (attempt + 1))
    if r.status_code != 200:
        raise ResolveError(f"{url}: HTTP {r.status_code}")
    a = parse_response(r.content, name, rtype)
    a.via = next((k for k, v in QUAD9.items() if v == url), url)
    return a


def resolve(name: str, rtype: str = "A", *, require_validated: Optional[bool] = None,
            transport: Optional[httpx.BaseTransport] = None) -> Answer:
    """The answer for name/rtype from the most trustworthy resolver that answers (see the module docstring)."""
    st = settings()
    strict = st["require_validated"] if require_validated is None else require_validated
    timeout = float(st["timeout_s"])
    errors = []
    tries: list = []
    if st.get("ironroot"):
        tries.append(("dns", st["ironroot"]))
    tries += [("doh", QUAD9.get(u, u)) for u in st.get("upstreams") or []]
    for kind, where in tries:
        try:
            a = ask_dns(where, name, rtype, timeout) if kind == "dns" else ask_doh(where, name, rtype, timeout, transport)
        except (OSError, ResolveError, httpx.HTTPError, struct.error, ValueError) as e:
            errors.append(f"{where}: {e}")
            continue
        if a.rcode == 2:                                    # SERVFAIL: a validating resolver refusing bad signatures
            errors.append(f"{a.via}: SERVFAIL (DNSSEC validation failed, or the zone is unreachable)")
            if strict:
                break
            continue
        if strict and not a.validated and a.rcode == 0:
            raise ResolveError(f"{name} {rtype}: {a.via} answered without DNSSEC validation (the zone is unsigned), "
                               "and validated answers are required")
        return a
    if strict:
        raise ResolveError(f"{name} {rtype}: no validated answer ({'; '.join(errors) or 'no resolver configured'})")
    try:                                                   # last resort: this machine's own resolver, never "validated"
        fam = {"A": socket.AF_INET, "AAAA": socket.AF_INET6}.get(rtype.upper())
        if fam is None:
            raise ResolveError(f"the system resolver only answers A and AAAA ({'; '.join(errors)})")
        vals = sorted({i[4][0] for i in socket.getaddrinfo(name, None, fam)})
        return Answer(name=name, rtype=rtype.upper(), values=vals, validated=False, via="system")
    except OSError as e:
        raise ResolveError(f"{name} {rtype}: {'; '.join(errors + [str(e)])}") from e


def status() -> dict:
    """Which resolvers answer right now, and whether they validate (a lookup of a signed name: isc.org)."""
    st = settings()
    out = {"settings": st, "resolvers": []}
    targets = ([("ironroot", "dns", st["ironroot"])] if st.get("ironroot") else []) + \
        [(u, "doh", QUAD9.get(u, u)) for u in st.get("upstreams") or []]
    for label, kind, where in targets:
        try:
            a = ask_dns(where, "isc.org", "A", 3.0) if kind == "dns" else ask_doh(where, "isc.org", "A", 3.0)
            out["resolvers"].append({"name": label, "address": where, "answers": True, "validates": a.validated})
        except (OSError, ResolveError, httpx.HTTPError, struct.error, ValueError) as e:
            out["resolvers"].append({"name": label, "address": where, "answers": False, "error": str(e)[:200]})
    return out
