"""WebDAV (RFC 4918, class 1 and 2) for the file server: /dav/ lists the shares, /dav/<share>/... is the share.

    Windows   Explorer > This PC > Map network drive > https://host/dav/<share>  (Windows sends passwords only over
              HTTPS by default: put the file server behind ABP Hosting's edge with a certificate, or use a client such
              as WinSCP / rclone / Cyberduck over plain http on the LAN)
    macOS     Finder > Go > Connect to Server > http(s)://host:8790/dav/<share>
    Linux     davfs2, GNOME Files / Dolphin (davs://host/dav/<share>), rclone
    phones    any WebDAV-capable file manager (iOS Files through an app, Solid Explorer, ...)

Methods: OPTIONS, PROPFIND (Depth 0/1; infinity is answered as 1), PROPPATCH (accepted, so Windows can set file
times; times are not stored), GET/HEAD (Range), PUT, DELETE, MKCOL, COPY, MOVE (Destination, Overwrite), LOCK/UNLOCK
(exclusive write locks, enforced on PUT/DELETE/MOVE: a locked resource needs its token in the If header).
"""
from __future__ import annotations

import asyncio
import email.utils
import mimetypes
import shutil
import time
import urllib.parse
import uuid
from xml.etree import ElementTree as ET

from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response
from starlette.routing import Route

from bot.fileserver import shares
from bot.fileserver.store import FsError

NS = "DAV:"
_locks: dict[tuple[str, str], dict] = {}        # (share, path) -> {token, owner, expires, depth}


def _href(share: str, rel: str, is_dir: bool) -> str:
    parts = ["dav"] + ([share] if share else []) + ([p for p in rel.split("/") if p])
    h = "/" + "/".join(urllib.parse.quote(p) for p in parts)
    return h + "/" if is_dir else h


def _split(path: str) -> tuple[str, str]:
    path = urllib.parse.unquote(path)
    rest = path.split("/dav", 1)[1].strip("/") if "/dav" in path else path.strip("/")
    share, _, rel = rest.partition("/")
    return share, rel


def _http_date(ts: float) -> str:
    return email.utils.formatdate(ts, usegmt=True)


def _iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def _prop_response(ms: ET.Element, href: str, *, is_dir: bool, size: int = 0, mtime: float = 0, name: str = "",
                   quota: tuple[int, int] | None = None) -> None:
    r = ET.SubElement(ms, f"{{{NS}}}response")
    ET.SubElement(r, f"{{{NS}}}href").text = href
    ps = ET.SubElement(r, f"{{{NS}}}propstat")
    p = ET.SubElement(ps, f"{{{NS}}}prop")
    ET.SubElement(p, f"{{{NS}}}displayname").text = name
    rt = ET.SubElement(p, f"{{{NS}}}resourcetype")
    if is_dir:
        ET.SubElement(rt, f"{{{NS}}}collection")
    else:
        ET.SubElement(p, f"{{{NS}}}getcontentlength").text = str(size)
        ET.SubElement(p, f"{{{NS}}}getcontenttype").text = mimetypes.guess_type(name)[0] or "application/octet-stream"
        ET.SubElement(p, f"{{{NS}}}getetag").text = f'"{int(mtime * 1000):x}-{size:x}"'
    ET.SubElement(p, f"{{{NS}}}getlastmodified").text = _http_date(mtime or time.time())
    ET.SubElement(p, f"{{{NS}}}creationdate").text = _iso(mtime or time.time())
    sl = ET.SubElement(p, f"{{{NS}}}supportedlock")
    le = ET.SubElement(sl, f"{{{NS}}}lockentry")
    ET.SubElement(ET.SubElement(le, f"{{{NS}}}lockscope"), f"{{{NS}}}exclusive")
    ET.SubElement(ET.SubElement(le, f"{{{NS}}}locktype"), f"{{{NS}}}write")
    if quota:
        ET.SubElement(p, f"{{{NS}}}quota-available-bytes").text = str(quota[0])
        ET.SubElement(p, f"{{{NS}}}quota-used-bytes").text = str(quota[1])
    ET.SubElement(ps, f"{{{NS}}}status").text = "HTTP/1.1 200 OK"


def _xml(el: ET.Element, status: int = 207, headers: dict | None = None) -> Response:
    ET.register_namespace("D", NS)
    body = b'<?xml version="1.0" encoding="utf-8"?>\n' + ET.tostring(el)
    return Response(body, status, media_type='application/xml; charset="utf-8"', headers=headers)


def _locked(share: str, rel: str, request: Request) -> bool:
    """Whether share/rel (or a folder above it) holds a live lock this request does not present the token for."""
    now = time.time()
    for k in [k for k, v in _locks.items() if v["expires"] < now]:
        _locks.pop(k, None)
    ifh = request.headers.get("if", "")
    parts = rel.split("/") if rel else []
    for i in range(len(parts), -1, -1):
        lk = _locks.get((share, "/".join(parts[:i])))
        if lk and (i == len(parts) or lk["depth"] == "infinity") and lk["token"] not in ifh:
            return True
    return False


def routes() -> list[Route]:
    from bot.fileserver.server import _deny, send_file, who, write_stream

    async def handle(request: Request) -> Response:
        m = request.method
        share_name, rel = _split(request.url.path)
        user = who(request)
        if m == "OPTIONS":
            return Response(headers={"DAV": "1, 2", "MS-Author-Via": "DAV", "Allow":
                                     "OPTIONS, PROPFIND, PROPPATCH, GET, HEAD, PUT, DELETE, MKCOL, COPY, MOVE, LOCK, UNLOCK"})
        if not share_name:                                  # /dav/: the shares
            if m != "PROPFIND":
                return PlainTextResponse("choose a share", 405)
            visible = [(n, s) for n, s in shares.shares().items() if shares.can(user, {**s, "name": n}, "r")]
            if not visible and not user:
                return _deny(None)
            ms = ET.Element(f"{{{NS}}}multistatus")
            _prop_response(ms, "/dav/", is_dir=True, name="ABP")
            if request.headers.get("depth", "1") != "0":
                for n, _s in visible:
                    _prop_response(ms, _href(n, "", True), is_dir=True, name=n)
            return _xml(ms)
        try:
            s = shares.get(share_name)
            rel = shares.clean(rel)
        except FsError as e:
            return PlainTextResponse(str(e), 404)
        write = m in ("PUT", "DELETE", "MKCOL", "MOVE", "LOCK", "UNLOCK", "PROPPATCH")
        if not shares.can(user, s, "w" if write else "r"):
            return _deny(user)
        try:
            if m == "PROPFIND":
                hit = shares.locate(s, rel)
                emu = None if hit else shares.emulated(s, rel)
                if not hit and not emu and rel:
                    return PlainTextResponse("Not found", 404)
                ms = ET.Element(f"{{{NS}}}multistatus")
                is_dir = (not rel) or bool(hit and hit[1].is_dir())
                if hit:
                    st = hit[1].stat()
                    q = None
                    if is_dir:
                        u = shutil.disk_usage(hit[1])
                        q = (u.free, u.used)
                    _prop_response(ms, _href(s["name"], rel, is_dir), is_dir=is_dir, size=st.st_size, mtime=st.st_mtime,
                                   name=rel.rsplit("/", 1)[-1] or s["name"], quota=q)
                else:
                    _prop_response(ms, _href(s["name"], rel, True), is_dir=True, name=rel.rsplit("/", 1)[-1] or s["name"])
                if is_dir and request.headers.get("depth", "1") != "0":
                    for e in await asyncio.to_thread(shares.listing, s, rel):
                        child = f"{rel}/{e['name']}" if rel else e["name"]
                        _prop_response(ms, _href(s["name"], child, e["dir"]), is_dir=e["dir"], size=e["size"], mtime=e["mtime"], name=e["name"])
                return _xml(ms)
            if m in ("GET", "HEAD"):
                hit = shares.locate(s, rel)
                if hit and hit[1].is_dir():
                    return PlainTextResponse(f"{s['name']}/{rel}: a folder (use a WebDAV client)", 200)
                return send_file(s, rel, request)
            if m == "PROPPATCH":
                ms = ET.Element(f"{{{NS}}}multistatus")
                r = ET.SubElement(ms, f"{{{NS}}}response")
                ET.SubElement(r, f"{{{NS}}}href").text = _href(s["name"], rel, False)
                body = await request.body()
                ps = ET.SubElement(r, f"{{{NS}}}propstat")
                p = ET.SubElement(ps, f"{{{NS}}}prop")
                try:
                    for setel in ET.fromstring(body).iter(f"{{{NS}}}prop"):
                        for child in setel:
                            ET.SubElement(p, child.tag)
                except ET.ParseError:
                    pass
                ET.SubElement(ps, f"{{{NS}}}status").text = "HTTP/1.1 200 OK"
                return _xml(ms)
            if _locked(s["name"], rel, request) and m in ("PUT", "DELETE", "MOVE"):
                return PlainTextResponse("locked", 423)
            if m == "PUT":
                if not rel:
                    return PlainTextResponse("name a file", 409)
                parent = rel.rsplit("/", 1)[0] if "/" in rel else ""
                if parent and not shares.locate(s, parent):
                    return PlainTextResponse("the folder does not exist", 409)
                existed = shares.locate(s, rel) is not None
                dest = await asyncio.to_thread(shares.place, s, rel, int(request.headers.get("content-length") or 0))
                await write_stream(dest, request.stream(), True)
                return Response(status_code=204 if existed else 201)
            if m == "DELETE":
                await asyncio.to_thread(shares.remove_path, s, rel)
                _locks.pop((s["name"], rel), None)
                return Response(status_code=204)
            if m == "MKCOL":
                if shares.locate(s, rel):
                    return PlainTextResponse("exists", 405)
                if await request.body():
                    return PlainTextResponse("MKCOL with a body is not supported", 415)
                parent = rel.rsplit("/", 1)[0] if "/" in rel else ""
                if parent and not shares.locate(s, parent):
                    return PlainTextResponse("the parent folder does not exist", 409)
                await asyncio.to_thread(shares.make_dir, s, rel)
                return Response(status_code=201)
            if m in ("COPY", "MOVE"):
                dst = request.headers.get("destination", "")
                d_share, d_rel = _split(urllib.parse.urlparse(dst).path)
                ds = shares.get(d_share)
                if not shares.can(user, ds, "w"):
                    return _deny(user)
                d_rel = shares.clean(d_rel)
                overwrite = request.headers.get("overwrite", "T").upper() != "F"
                existed = shares.locate(ds, d_rel) is not None
                if existed and not overwrite:
                    return PlainTextResponse("the destination exists", 412)
                if m == "MOVE":
                    await asyncio.to_thread(shares.move, s, rel, ds, d_rel, overwrite)
                else:
                    from bot.fileserver.server import _copy
                    if existed:
                        await asyncio.to_thread(shares.remove_path, ds, d_rel, False)
                    await asyncio.to_thread(_copy, s, rel, ds, d_rel)
                return Response(status_code=204 if existed else 201)
            if m == "LOCK":
                timeout = 3600
                th = request.headers.get("timeout", "")
                if th.lower().startswith("second-"):
                    try:
                        timeout = min(86400, int(th.split("-", 1)[1].split(",")[0]))
                    except ValueError:
                        pass
                key = (s["name"], rel)
                body = await request.body()
                cur = _locks.get(key)
                if not body and cur:                         # a refresh
                    cur["expires"] = time.time() + timeout
                    lk = cur
                else:
                    if cur and cur["expires"] > time.time():
                        return PlainTextResponse("locked", 423)
                    owner = ""
                    try:
                        o = ET.fromstring(body).find(f"{{{NS}}}owner") if body else None
                        owner = "".join(o.itertext()).strip() if o is not None else ""
                    except ET.ParseError:
                        pass
                    lk = {"token": f"opaquelocktoken:{uuid.uuid4()}", "owner": owner, "expires": time.time() + timeout,
                          "depth": request.headers.get("depth", "infinity").lower()}
                    _locks[key] = lk
                    if not shares.locate(s, rel) and rel:      # a lock-null resource: create an empty file, as clients expect
                        dest = await asyncio.to_thread(shares.place, s, rel, 0)
                        dest.touch()
                prop = ET.Element(f"{{{NS}}}prop")
                al = ET.SubElement(ET.SubElement(prop, f"{{{NS}}}lockdiscovery"), f"{{{NS}}}activelock")
                ET.SubElement(ET.SubElement(al, f"{{{NS}}}locktype"), f"{{{NS}}}write")
                ET.SubElement(ET.SubElement(al, f"{{{NS}}}lockscope"), f"{{{NS}}}exclusive")
                ET.SubElement(al, f"{{{NS}}}depth").text = lk["depth"]
                ET.SubElement(al, f"{{{NS}}}owner").text = lk["owner"]
                ET.SubElement(al, f"{{{NS}}}timeout").text = f"Second-{timeout}"
                ET.SubElement(ET.SubElement(al, f"{{{NS}}}locktoken"), f"{{{NS}}}href").text = lk["token"]
                return _xml(prop, 200, {"Lock-Token": f"<{lk['token']}>"})
            if m == "UNLOCK":
                tok = request.headers.get("lock-token", "").strip("<>")
                lk = _locks.get((s["name"], rel))
                if lk and lk["token"] == tok:
                    _locks.pop((s["name"], rel), None)
                    return Response(status_code=204)
                return PlainTextResponse("no such lock", 409)
        except FsError as e:
            return PlainTextResponse(str(e), 409 if "exists" in str(e) else 404 if "does not exist" in str(e) else 400)
        return PlainTextResponse("method not allowed", 405)

    methods = ["OPTIONS", "PROPFIND", "PROPPATCH", "GET", "HEAD", "PUT", "DELETE", "MKCOL", "COPY", "MOVE", "LOCK", "UNLOCK"]
    return [Route("/dav", handle, methods=methods), Route("/dav/", handle, methods=methods),
            Route("/dav/{path:path}", handle, methods=methods)]

