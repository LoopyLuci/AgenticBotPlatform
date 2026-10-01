"""ABP's edge: the web server that answers for every site served from this machine.

    python -m bot.hosting.edge [--http 80] [--https 443] [--bind 0.0.0.0]

One process, two listeners. Requests are routed by host name to a site (bot/hosting/service.py writes them to
`<hosting>/sites.json`, which the edge re-reads when it changes, so adding a site or a domain needs no restart):

  static     files from a folder: index.html for folders, an SPA fallback to /index.html if the site asks for it,
             ETag / Last-Modified / Range (FileResponse), gzip, and a long cache for fingerprinted assets
  proxy      to an app on this machine or the LAN (http://127.0.0.1:3000), WebSockets included, with
             X-Forwarded-For/-Proto/-Host and the original Host if the site wants it
  redirect   every path to another address (301, the path kept)

HTTPS picks each site's certificate by SNI from `<hosting>/certs/<host>/` (written by bot/hosting/acme.py or put there
by a person); a host without one gets a self-signed certificate, so a browser warns rather than the handshake failing.
Port 80 answers ACME HTTP-01 challenges for every host, and redirects to HTTPS for sites with a certificate and
force_https. `/.well-known/abp-edge` on 127.0.0.1 tells ABP the edge is up and which sites it has loaded.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import datetime as _dt
import hashlib
import hmac
import json
import logging
import mimetypes
import os
import re
import ssl
import sys
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger("abp.edge")

_FINGERPRINTED = re.compile(r"[.-][0-9a-f]{8,}\.(js|css|woff2?|png|jpg|jpeg|svg|webp|avif)$", re.I)
_HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailers", "transfer-encoding",
        "upgrade", "host", "content-length"}


def hosting_dir() -> Path:
    from bot.hosting.store import root
    return root()


def challenge_dir() -> Path:
    p = hosting_dir() / "acme-challenges"
    p.mkdir(parents=True, exist_ok=True)
    return p


def cert_dir(host: str) -> Path:
    return hosting_dir() / "certs" / host.lower()


def hash_password(password: str, salt: Optional[bytes] = None) -> str:
    salt = salt or os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 200_000)
    return f"pbkdf2${base64.b64encode(salt).decode()}${base64.b64encode(dk).decode()}"


def check_password(password: str, stored: str) -> bool:
    try:
        _, salt, want = stored.split("$")
        got = hashlib.pbkdf2_hmac("sha256", password.encode(), base64.b64decode(salt), 200_000)
        return hmac.compare_digest(got, base64.b64decode(want))
    except (ValueError, TypeError):
        return False


# ---- routing table -------------------------------------------------------------------------------------------- #

class Table:
    """sites.json, re-read when its modification time changes; host -> site."""

    def __init__(self, path: Path):
        self.path = path
        self.mtime = -1.0
        self.by_host: dict[str, dict] = {}
        self.wild: list[tuple[str, dict]] = []
        self.loaded_at = 0.0

    def refresh(self) -> None:
        try:
            m = self.path.stat().st_mtime
        except FileNotFoundError:
            m = 0.0
        if m == self.mtime:
            return
        try:
            sites = json.loads(self.path.read_text(encoding="utf-8")) if m else {}
        except (OSError, ValueError) as e:
            logger.warning("edge: cannot read %s (%s); keeping the previous routes", self.path, e)
            return
        by_host, wild = {}, []
        for sid, s in sites.items():
            if not s.get("enabled", True) or s.get("serve", "edge") != "edge":
                continue
            s = {**s, "id": sid}
            for d in s.get("domains") or []:
                d = d.lower().strip().rstrip(".")
                if d.startswith("*."):
                    wild.append((d[1:], s))
                else:
                    by_host[d] = s
        self.by_host, self.wild, self.mtime, self.loaded_at = by_host, sorted(wild, key=lambda w: -len(w[0])), m, time.time()
        logger.info("edge: %d host names loaded", len(by_host) + len(wild))

    def site_for(self, host: str) -> Optional[dict]:
        host = host.split(":")[0].lower().rstrip(".")
        s = self.by_host.get(host)
        if s:
            return s
        for suffix, site in self.wild:
            if host.endswith(suffix):
                return site
        return None


# ---- certificates --------------------------------------------------------------------------------------------- #

def self_signed(host: str) -> tuple[bytes, bytes]:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, host)])
    now = _dt.datetime.now(_dt.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - _dt.timedelta(minutes=5))
            .not_valid_after(now + _dt.timedelta(days=365))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName(host)]), critical=False)
            .sign(key, hashes.SHA256()))
    return (cert.public_bytes(serialization.Encoding.PEM),
            key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))


class Certs:
    """SNI -> an SSLContext per host, from <hosting>/certs/<host>/{cert,key}.pem (reloaded when they change)."""

    def __init__(self):
        self.cache: dict[str, tuple[float, ssl.SSLContext]] = {}

    def _ctx(self, cert: Path, key: Path) -> ssl.SSLContext:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ctx.set_alpn_protocols(["http/1.1"])
        ctx.load_cert_chain(str(cert), str(key))
        return ctx

    def for_host(self, host: str) -> ssl.SSLContext:
        host = (host or "localhost").lower()
        d = cert_dir(host)
        cert, key = d / "cert.pem", d / "key.pem"
        if not cert.exists():
            parts = host.split(".", 1)          # a wildcard certificate one level up
            if len(parts) == 2:
                wd = cert_dir("_wildcard." + parts[1])
                if (wd / "cert.pem").exists():
                    d, cert, key = wd, wd / "cert.pem", wd / "key.pem"
        if not cert.exists():
            d = hosting_dir() / "certs" / "_self-signed" / host
            cert, key = d / "cert.pem", d / "key.pem"
            if not cert.exists():
                d.mkdir(parents=True, exist_ok=True)
                c, k = self_signed(host)
                key.write_bytes(k)
                cert.write_bytes(c)
        m = cert.stat().st_mtime
        hit = self.cache.get(str(cert))
        if hit and hit[0] == m:
            return hit[1]
        ctx = self._ctx(cert, key)
        self.cache[str(cert)] = (m, ctx)
        return ctx

    def has_real(self, host: str) -> bool:
        if (cert_dir(host) / "cert.pem").exists():
            return True
        parts = host.split(".", 1)
        return len(parts) == 2 and (cert_dir("_wildcard." + parts[1]) / "cert.pem").exists()

    def server_context(self) -> ssl.SSLContext:
        base = self.for_host("localhost")

        def pick(sock: ssl.SSLObject, name: Optional[str], _ctx: ssl.SSLContext):
            try:
                sock.context = self.for_host(name or "localhost")
            except Exception as e:  # noqa: BLE001 - a broken cert must not take the listener down
                logger.warning("edge: certificate for %s: %s", name, e)
            return None
        base.sni_callback = pick
        return base


# ---- the app -------------------------------------------------------------------------------------------------- #

def build_app(table: Table, certs: Certs, scheme: str):
    import httpx
    from starlette.applications import Starlette
    from starlette.requests import Request
    from starlette.responses import FileResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response, StreamingResponse
    from starlette.routing import Route, WebSocketRoute
    from starlette.websockets import WebSocket, WebSocketDisconnect

    client = httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=10.0), follow_redirects=False, verify=False)
    stats = {"requests": 0, "started": time.time()}

    def _auth_ok(site: dict, request) -> bool:
        auth = site.get("auth")
        if not auth:
            return True
        h = request.headers.get("authorization", "")
        if not h.lower().startswith("basic "):
            return False
        try:
            user, _, pw = base64.b64decode(h[6:]).decode().partition(":")
        except (ValueError, UnicodeDecodeError):
            return False
        return user == auth.get("user") and check_password(pw, auth.get("password_hash", ""))

    def _static(site: dict, request) -> Response:
        root = Path(site["root"]).resolve()
        rel = request.url.path.lstrip("/")
        try:
            target = (root / rel).resolve()
            target.relative_to(root)
        except (ValueError, OSError):
            return PlainTextResponse("Not found", 404)
        if target.is_dir():
            if not request.url.path.endswith("/"):
                return RedirectResponse(request.url.path + "/" + (f"?{request.url.query}" if request.url.query else ""), 301)
            target = target / "index.html"
        if not target.is_file():
            for alt in (target.with_suffix(".html"),):          # /about -> about.html (clean URLs)
                if alt.is_file() and alt.parent.resolve().is_relative_to(root):
                    target = alt
                    break
            else:
                if site.get("spa") and "." not in Path(rel).name:
                    target = root / "index.html"
                elif (root / "404.html").is_file():
                    return FileResponse(root / "404.html", status_code=404)
                else:
                    return PlainTextResponse("Not found", 404)
        headers = {"X-Content-Type-Options": "nosniff"}
        if _FINGERPRINTED.search(target.name):
            headers["Cache-Control"] = "public, max-age=31536000, immutable"
        elif target.suffix == ".html":
            headers["Cache-Control"] = "no-cache"
        else:
            headers["Cache-Control"] = "public, max-age=3600"
        headers.update(site.get("headers") or {})
        mt = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        return FileResponse(target, media_type=mt, headers=headers)

    async def _proxy(site: dict, request) -> Response:
        up = site["upstream"].rstrip("/")
        url = up + request.url.path + (f"?{request.url.query}" if request.url.query else "")
        headers = {k: v for k, v in request.headers.items() if k.lower() not in _HOP}
        client_ip = request.client.host if request.client else ""
        prior = request.headers.get("x-forwarded-for")
        headers["x-forwarded-for"] = f"{prior}, {client_ip}" if prior else client_ip
        headers["x-forwarded-proto"] = scheme
        headers["x-forwarded-host"] = request.headers.get("host", "")
        if site.get("preserve_host"):
            headers["host"] = request.headers.get("host", "")
        body = request.stream() if request.method not in ("GET", "HEAD") else None
        try:
            req = client.build_request(request.method, url, headers=headers, content=body)
            resp = await client.send(req, stream=True)
        except httpx.HTTPError as e:
            return PlainTextResponse(f"The site's app at {up} is not answering ({type(e).__name__}).", 502)
        out_headers = [(k, v) for k, v in resp.headers.multi_items() if k.lower() not in _HOP | {"content-encoding"}]
        if "content-encoding" in resp.headers:
            out_headers.append(("content-encoding", resp.headers["content-encoding"]))

        async def body_iter():
            try:
                async for chunk in resp.aiter_raw():
                    yield chunk
            finally:
                await resp.aclose()
        r = StreamingResponse(body_iter(), status_code=resp.status_code)
        r.raw_headers = [(k.lower().encode("latin-1"), v.encode("latin-1")) for k, v in out_headers]
        return r

    async def handle(request: Request) -> Response:
        stats["requests"] += 1
        table.refresh()
        path = request.url.path
        host = request.headers.get("host", "").split(":")[0].lower()
        if path.startswith("/.well-known/acme-challenge/"):
            token = path.rsplit("/", 1)[-1]
            f = challenge_dir() / token
            if re.fullmatch(r"[A-Za-z0-9_-]+", token) and f.is_file():
                return PlainTextResponse(f.read_text(encoding="utf-8"))
            return PlainTextResponse("Not found", 404)
        if path == "/.well-known/abp-edge" and request.client and request.client.host in ("127.0.0.1", "::1"):
            return JSONResponse({"edge": True, "pid": os.getpid(), "scheme": scheme, "hosts": sorted(table.by_host),
                                 "wildcards": [w[0] for w in table.wild], "requests": stats["requests"],
                                 "started": stats["started"], "loaded_at": table.loaded_at})
        site = table.site_for(host)
        if not site:
            return PlainTextResponse(f"No site here answers to {host or 'this address'}.", 421 if host else 400)
        if scheme == "http" and site.get("force_https", True) and certs.has_real(host):
            return RedirectResponse(f"https://{request.headers.get('host', host)}{path}" + (f"?{request.url.query}" if request.url.query else ""), 301)
        if not _auth_ok(site, request):
            return Response("Sign in to see this site.", 401, {"WWW-Authenticate": f'Basic realm="{site.get("name", "site")}"'})
        kind = site.get("kind")
        if kind == "redirect":
            return RedirectResponse(site["redirect_to"].rstrip("/") + path + (f"?{request.url.query}" if request.url.query else ""), 301)
        if kind == "static":
            return _static(site, request)
        if kind in ("proxy", "container", "app"):
            return await _proxy(site, request)
        return PlainTextResponse(f"site {site.get('id')} has an unknown kind {kind!r}", 500)

    async def ws(websocket: WebSocket):
        import websockets
        table.refresh()
        host = websocket.headers.get("host", "").split(":")[0].lower()
        site = table.site_for(host)
        if not site or site.get("kind") not in ("proxy", "container", "app") or not _auth_ok(site, websocket):
            await websocket.close(code=1008)
            return
        up = site["upstream"].rstrip("/").replace("http://", "ws://", 1).replace("https://", "wss://", 1)
        url = up + websocket.url.path + (f"?{websocket.url.query}" if websocket.url.query else "")
        protos = [p.strip() for p in websocket.headers.get("sec-websocket-protocol", "").split(",") if p.strip()]
        try:
            async with websockets.connect(url, subprotocols=protos or None, max_size=None,
                                          additional_headers={"x-forwarded-for": websocket.client.host if websocket.client else "",
                                                              "x-forwarded-proto": "wss" if scheme == "https" else "ws"}) as upstream:
                await websocket.accept(subprotocol=upstream.subprotocol)

                async def down():
                    async for msg in upstream:
                        await (websocket.send_bytes(msg) if isinstance(msg, bytes) else websocket.send_text(msg))

                async def upward():
                    while True:
                        m = await websocket.receive()
                        if m["type"] == "websocket.disconnect":
                            return
                        if m.get("bytes") is not None:
                            await upstream.send(m["bytes"])
                        elif m.get("text") is not None:
                            await upstream.send(m["text"])
                tasks = [asyncio.create_task(down()), asyncio.create_task(upward())]
                _, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for t in pending:
                    t.cancel()
        except (OSError, websockets.exceptions.WebSocketException, WebSocketDisconnect) as e:
            logger.info("edge: websocket to %s ended: %s", url, e)
        try:
            await websocket.close()
        except RuntimeError:
            pass

    methods = ["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]
    app = Starlette(routes=[WebSocketRoute("/{path:path}", ws), Route("/{path:path}", handle, methods=methods)])
    from starlette.middleware.gzip import GZipMiddleware
    app.add_middleware(GZipMiddleware, minimum_size=1024)
    return app


async def serve(http_port: int, https_port: int, bind: str) -> None:
    import uvicorn

    table = Table(hosting_dir() / "sites.json")
    table.refresh()
    certs = Certs()
    servers = []
    if http_port:
        cfg = uvicorn.Config(build_app(table, certs, "http"), host=bind, port=http_port, log_level="warning",
                             proxy_headers=False, server_header=False, ws="websockets", lifespan="off")
        servers.append(uvicorn.Server(cfg))
    if https_port:
        cfg = uvicorn.Config(build_app(table, certs, "https"), host=bind, port=https_port, log_level="warning",
                             proxy_headers=False, server_header=False, ws="websockets", lifespan="off")
        cfg.load()
        cfg.ssl = certs.server_context()    # uvicorn takes one context; the SNI callback swaps in each host's
        cfg.loaded = True
        servers.append(uvicorn.Server(cfg))
    (hosting_dir() / "edge.json").write_text(json.dumps({"pid": os.getpid(), "http": http_port, "https": https_port,
                                                         "bind": bind, "started": time.time()}), encoding="utf-8")
    await asyncio.gather(*(s.serve() for s in servers))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m bot.hosting.edge", description="ABP's web server for hosted sites")
    ap.add_argument("--http", type=int, default=80)
    ap.add_argument("--https", type=int, default=443)
    ap.add_argument("--bind", default="0.0.0.0")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    try:
        asyncio.run(serve(a.http, a.https, a.bind))
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
