"""A small filtering forward proxy: commands may reach only the hosts on a list (roadmap P2).

    sandbox:
      network: allowlist
      allow_hosts: ["pypi.org", "*.pythonhosted.org", "api.example.com:443"]
      allow_private_hosts: []     # names / addresses that may resolve to this computer or a private network

The docker backend runs a command on an `--internal` network (no route out at all) next to this
proxy, which is the only thing with a way out, and points HTTP(S)_PROXY at it. So the command
cannot get around the list: a program that ignores the proxy settings simply has no network.

**Rules.** The proxy speaks HTTP CONNECT (https and anything tunnelled) and plain HTTP in
absolute form (`GET http://host/path`). Everything else is refused.
* A host is allowed when it matches an entry: the exact name, or `*.domain` for any name below
  `domain` (not `domain` itself). An entry may carry a port (`host:443`, `[::1]:8080`); without
  one, any port matches. Names are compared case-insensitively.
* The proxy does its own DNS and connects to the address it checked, so a name cannot be
  re-pointed between the check and the connection.
* An IP address as the host is refused unless that exact address is listed.
* A destination that is loopback, private, link-local, reserved or otherwise not a public
  address is refused, however the host got there (a public name pointing at 127.0.0.1 included),
  unless the host matches an `allow_private_hosts` entry. Those entries also count as allowed
  hosts. This is what keeps a command from using the proxy to reach this computer's own services.
* Every decision is reported to a log callback (one JSON line on stdout when run as a program).
* Limits: header size and time, simultaneous connections, idle time, total time and bytes per
  connection.

This module imports only the standard library on purpose: the docker backend runs this very file
inside a small container (`python net_allowlist.py`), and it must work without the rest of ABP.
"""

from __future__ import annotations

import argparse
import asyncio
import ipaddress
import json
import os
import socket
import sys
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Iterable, Optional
from urllib.parse import urlsplit

MAX_HEADER_BYTES = 16 * 1024
HEADER_TIMEOUT = 10.0
CONNECT_TIMEOUT = 10.0
IDLE_TIMEOUT = 60.0
MAX_SECONDS = 600.0
MAX_BYTES = 512 * 1024 * 1024
MAX_CONNECTIONS = 64
DEFAULT_PORT = 3128


# ---- host patterns ---------------------------------------------------------------------
@dataclass(frozen=True)
class Pattern:
    host: str                 # lower-case; for a wildcard, the suffix after "*."
    wildcard: bool = False
    port: Optional[int] = None

    def matches(self, host: str, port: int) -> bool:
        if self.port is not None and self.port != port:
            return False
        if self.wildcard:
            return host.endswith("." + self.host)
        return host == self.host


def _norm_host(host: str) -> str:
    host = host.strip().lower().rstrip(".")
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    return host


def parse_pattern(text: str) -> Pattern:
    raw = str(text).strip().lower()
    if not raw:
        raise ValueError("an empty host pattern")
    port: Optional[int] = None
    host = raw
    if raw.startswith("["):                       # [v6] or [v6]:port
        end = raw.find("]")
        if end < 0:
            raise ValueError(f"bad host pattern {text!r}")
        host, rest = raw[:end + 1], raw[end + 1:]
        if rest:
            if not rest.startswith(":"):
                raise ValueError(f"bad host pattern {text!r}")
            port_text = rest[1:]
            port = _port(port_text, text)
    elif raw.count(":") == 1:
        host, port_text = raw.split(":")
        port = _port(port_text, text)
    host = _norm_host(host)
    wildcard = host.startswith("*.")
    if wildcard:
        host = host[2:]
    if not host or "*" in host or "/" in host or " " in host:
        raise ValueError(f"bad host pattern {text!r} (use an exact name, or *.domain)")
    if wildcard and _is_ip(host):
        raise ValueError(f"bad host pattern {text!r} (a wildcard needs a domain name)")
    return Pattern(host, wildcard, port)


def _port(port_text: str, whole: str) -> int:
    if not port_text.isdigit() or not 0 < int(port_text) < 65536:
        raise ValueError(f"bad port in host pattern {whole!r}")
    return int(port_text)


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def parse_patterns(items: Optional[Iterable]) -> list[Pattern]:
    return [parse_pattern(i) for i in (items or []) if str(i).strip()]


def is_public(addr: str) -> bool:
    """True only for an address that is out on the public internet."""
    ip = ipaddress.ip_address(addr)
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip.is_global and not ip.is_multicast


# ---- the decision ----------------------------------------------------------------------
@dataclass
class Decision:
    ok: bool
    reason: str
    host: str = ""
    port: int = 0
    addrs: list[str] = field(default_factory=list)


Resolver = Callable[[str, int], Awaitable[list[str]]]


async def system_resolver(host: str, port: int) -> list[str]:
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    seen: list[str] = []
    for info in infos:
        addr = info[4][0].split("%")[0]
        if addr not in seen:
            seen.append(addr)
    return seen


class Policy:
    def __init__(self, allow_hosts: Iterable = (), allow_private_hosts: Iterable = (),
                 resolver: Optional[Resolver] = None):
        self.allow = parse_patterns(allow_hosts)
        self.private = parse_patterns(allow_private_hosts)
        self.resolver = resolver or system_resolver

    def listed(self, host: str, port: int) -> bool:
        return any(p.matches(host, port) for p in (*self.allow, *self.private))

    def private_ok(self, host: str, port: int) -> bool:
        return any(p.matches(host, port) for p in self.private)

    async def decide(self, host: str, port: int) -> Decision:
        host = _norm_host(host)
        if not host or not 0 < port < 65536:
            return Decision(False, "bad destination", host, port)
        if not self.listed(host, port):
            return Decision(False, "not on the allow list", host, port)
        literal = _is_ip(host)
        try:
            addrs = [host] if literal else await asyncio.wait_for(self.resolver(host, port), CONNECT_TIMEOUT)
        except (OSError, asyncio.TimeoutError):
            return Decision(False, "the name did not resolve", host, port)
        if not addrs:
            return Decision(False, "the name did not resolve", host, port)
        if not self.private_ok(host, port):
            bad = [a for a in addrs if not is_public(a)]
            if bad:
                what = "an IP address" if literal else f"{host} resolves to"
                return Decision(False, f"{what} {bad[0]}, which is not a public address", host, port, addrs)
        return Decision(True, "allowed", host, port, addrs)


# ---- the proxy -------------------------------------------------------------------------
LogFn = Callable[[dict], None]


class Proxy:
    def __init__(self, policy: Policy, log: Optional[LogFn] = None, *, idle_timeout: float = IDLE_TIMEOUT,
                 max_seconds: float = MAX_SECONDS, max_bytes: int = MAX_BYTES, max_connections: int = MAX_CONNECTIONS):
        self.policy = policy
        self._log = log or (lambda row: None)
        self.idle_timeout = idle_timeout
        self.max_seconds = max_seconds
        self.max_bytes = max_bytes
        self._slots = asyncio.Semaphore(max_connections)
        self._server: Optional[asyncio.AbstractServer] = None

    @property
    def port(self) -> int:
        return self._server.sockets[0].getsockname()[1]

    async def start(self, host: str = "127.0.0.1", port: int = 0) -> None:
        self._server = await asyncio.start_server(self._client, host, port, limit=MAX_HEADER_BYTES)

    async def close(self) -> None:
        if self._server:
            self._server.close()
            await self._server.wait_closed()

    def _record(self, decision: Decision, method: str) -> None:
        try:
            self._log({"ts": round(time.time(), 3), "decision": "allow" if decision.ok else "deny", "method": method,
                       "host": decision.host, "port": decision.port, "reason": decision.reason})
        except Exception:  # noqa: BLE001 - a broken logger must not break the proxy
            pass

    async def _client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            async with self._slots:
                await self._serve(reader, writer)
        except (OSError, asyncio.TimeoutError, asyncio.IncompleteReadError, asyncio.LimitOverrunError, ValueError):
            pass
        finally:
            try:
                writer.close()
            except Exception:  # noqa: BLE001
                pass

    async def _serve(self, reader, writer) -> None:
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), HEADER_TIMEOUT)
        except (asyncio.LimitOverrunError, asyncio.IncompleteReadError, asyncio.TimeoutError):
            await self._reply(writer, 400, "bad or oversized request")
            return
        lines = head.decode("latin-1").split("\r\n")
        parts = lines[0].split(" ")
        if len(parts) != 3 or not parts[2].startswith("HTTP/"):
            await self._reply(writer, 400, "bad request line")
            return
        method, target, version = parts
        headers = [ln for ln in lines[1:] if ln]
        if method.upper() == "CONNECT":
            host, _, port_text = target.rpartition(":")
            if not host or not port_text.isdigit():
                await self._reply(writer, 400, "CONNECT needs host:port")
                return
            await self._connect(reader, writer, host, int(port_text), method="CONNECT", head_out=None)
            return
        url = urlsplit(target)
        if url.scheme.lower() != "http" or not url.hostname:
            self._record(Decision(False, "only CONNECT and http:// absolute-form requests are served", target[:80], 0), method)
            await self._reply(writer, 403, "only CONNECT and http:// absolute-form requests are served")
            return
        try:
            port = url.port or 80
        except ValueError:
            await self._reply(writer, 400, "bad port")
            return
        path = (url.path or "/") + (f"?{url.query}" if url.query else "")
        kept = [h for h in headers if h.split(":", 1)[0].strip().lower() not in
                ("proxy-connection", "proxy-authorization", "connection", "keep-alive")]
        out = "\r\n".join([f"{method} {path} {version}", *kept, "Connection: close"]) + "\r\n\r\n"
        await self._connect(reader, writer, url.hostname, port, method=method, head_out=out.encode("latin-1"))

    async def _reply(self, writer, status: int, why: str) -> None:
        reason = {200: "Connection Established", 400: "Bad Request", 403: "Forbidden", 502: "Bad Gateway"}.get(status, "Error")
        body = f"{why}\n".encode()
        writer.write(f"HTTP/1.1 {status} {reason}\r\nContent-Type: text/plain\r\nContent-Length: {len(body)}\r\n"
                     f"Connection: close\r\n\r\n".encode() + body)
        try:
            await writer.drain()
        except OSError:
            pass

    async def _connect(self, reader, writer, host: str, port: int, *, method: str, head_out: Optional[bytes]) -> None:
        decision = await self.policy.decide(host, port)
        self._record(decision, method)
        if not decision.ok:
            await self._reply(writer, 403, f"{decision.host}:{decision.port} refused: {decision.reason}")
            return
        up_r = up_w = None
        for addr in decision.addrs:
            try:
                up_r, up_w = await asyncio.wait_for(asyncio.open_connection(addr, port), CONNECT_TIMEOUT)
                break
            except (OSError, asyncio.TimeoutError):
                continue
        if up_w is None:
            await self._reply(writer, 502, f"could not connect to {decision.host}:{decision.port}")
            return
        try:
            if head_out is None:
                writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                await writer.drain()
            else:
                up_w.write(head_out)
                await up_w.drain()
            await self._pipe(reader, writer, up_r, up_w)
        finally:
            up_w.close()

    async def _pipe(self, c_r, c_w, u_r, u_w) -> None:
        moved = [0]

        async def copy(src, dst):
            try:
                while True:
                    chunk = await asyncio.wait_for(src.read(65536), self.idle_timeout)
                    if not chunk:
                        break
                    moved[0] += len(chunk)
                    if moved[0] > self.max_bytes:
                        break
                    dst.write(chunk)
                    await dst.drain()
            except (OSError, asyncio.TimeoutError):
                pass
            finally:
                try:
                    dst.close()
                except Exception:  # noqa: BLE001
                    pass

        tasks = [asyncio.ensure_future(copy(c_r, u_w)), asyncio.ensure_future(copy(u_r, c_w))]
        try:
            await asyncio.wait(tasks, timeout=self.max_seconds, return_when=asyncio.ALL_COMPLETED)
        finally:
            for t in tasks:
                t.cancel()


# ---- running as a program (inside the docker proxy container) -----------------------------
def _stdout_log(row: dict) -> None:
    sys.stdout.write(json.dumps(row) + "\n")
    sys.stdout.flush()


async def _main(listen: str, port: int, allow: list, private: list) -> None:
    proxy = Proxy(Policy(allow, private), _stdout_log)
    await proxy.start(listen, port)
    _stdout_log({"event": "listening", "port": proxy.port, "allow": len(proxy.policy.allow),
                 "allow_private": len(proxy.policy.private)})
    await asyncio.Event().wait()


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="ABP allow-list forward proxy")
    ap.add_argument("--listen", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = ap.parse_args(argv)
    try:
        cfg = json.loads(os.environ.get("ABP_NET_ALLOW") or "{}")
        asyncio.run(_main(args.listen, args.port, cfg.get("allow") or [], cfg.get("private") or []))
    except (ValueError, KeyboardInterrupt) as exc:
        if isinstance(exc, ValueError):
            sys.stderr.write(f"net_allowlist: {exc}\n")
            return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
