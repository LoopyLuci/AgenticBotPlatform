"""The file server process:  python -m bot.fileserver.server [--port 8790] [--bind 0.0.0.0]

    /                     the web file manager (sign in, browse, upload with resume, preview, share links, search)
    /api/...              the REST file API (below)
    /dav/<share>/...      WebDAV (bot/fileserver/dav.py): map it as a drive on Windows, macOS, Linux, iOS, Android
    /s/<token>[/...]      share links: a file to download, or a folder to browse (and upload to, if allowed)

Who is asking: the dashboard token (X-Dashboard-Token: an administrator), a file-server user (HTTP Basic, or the
session cookie the sign-in page sets), or nobody (public shares only). Each share's access decides the rest.

REST
    GET    /api/whoami                                 GET /api/shares (the ones you may read)
    GET    /api/list?share=&path=                      a folder (files of a lost disk show "emulated")
    GET    /api/file?share=&path=[&download=1]         a file, with Range; a lost disk's file streams from parity
    PUT    /api/file?share=&path=[&overwrite=1]        the body is the file
    DELETE /api/file?share=&path=                      to the recycle bin unless the share turns it off
    POST   /api/mkdir {share, path}                    POST /api/move {share, from, to, to_share?, overwrite?}
    POST   /api/copy {share, from, to, to_share?}
    POST   /api/uploads {share, path, size, overwrite?}   -> {id, offset}: a resumable upload
    PATCH  /api/uploads/{id}   (Upload-Offset: n)      append the body at n -> {offset, done}
    GET    /api/uploads/{id}                           where it stands (resume from offset)
    GET    /api/thumb?share=&path=&size=256            an image's thumbnail (JPEG)
    GET    /api/search?q=&share=&mode=words|meaning    the content index (bot/fileserver/index.py)
    GET    /api/links  POST /api/links {share, path, password?, expires_days?, max_downloads?, allow_upload?}
    DELETE /api/links/{token}
    POST   /login {user, password}   POST /logout
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import hmac
import json
import mimetypes
import os
import secrets
import shutil
import sys
import time
import urllib.parse
from pathlib import Path
from typing import Optional

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response, StreamingResponse
from starlette.routing import Route

from bot.fileserver import shares
from bot.fileserver.store import FsError, load, root, update

STATIC = Path(__file__).parent / "static"
COOKIE = "abpfs"
SESSION_DAYS = 14
UPLOAD_CHUNK_MAX = 256 << 20


# ---- who is asking -------------------------------------------------------------------------------------------------- #

def _key() -> bytes:
    f = root() / "session.key"
    if not f.exists():
        f.write_bytes(secrets.token_bytes(32))
    return f.read_bytes()


def make_session(user: str) -> str:
    exp = int(time.time() + SESSION_DAYS * 86400)
    msg = f"{user}|{exp}"
    sig = hmac.new(_key(), msg.encode(), hashlib.sha256).hexdigest()
    return base64.urlsafe_b64encode(f"{msg}|{sig}".encode()).decode()


def read_session(value: str) -> Optional[str]:
    try:
        user, exp, sig = base64.urlsafe_b64decode(value.encode()).decode().split("|")
    except (ValueError, UnicodeDecodeError):
        return None
    want = hmac.new(_key(), f"{user}|{exp}".encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, want) or time.time() > int(exp):
        return None
    return user


def who(request: Request) -> Optional[dict]:
    tok = os.environ.get("DASHBOARD_TOKEN", "")
    given = request.headers.get("x-dashboard-token", "")
    if tok and given and hmac.compare_digest(given, tok):
        return {"name": "admin", "admin": True, "via": "token"}
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("basic "):
        try:
            u, _, pw = base64.b64decode(auth[6:]).decode().partition(":")
        except (ValueError, UnicodeDecodeError):
            return None
        hit = shares.check_user(u, pw)
        return {**hit, "via": "basic"} if hit else None
    c = request.cookies.get(COOKIE)
    if c:
        u = read_session(c)
        if u:
            us = load("users", {}).get(u)
            if us:
                return {"name": u, "admin": bool(us.get("admin")), "via": "cookie"}
    return None


def _deny(user: Optional[dict]) -> Response:
    if user:
        return JSONResponse({"detail": "you do not have access to that"}, 403)
    return JSONResponse({"detail": "sign in"}, 401, headers={"WWW-Authenticate": 'Basic realm="ABP File Server", charset="UTF-8"'})


def _share(request: Request, mode: str, name: Optional[str] = None) -> tuple[Optional[dict], Optional[dict], Optional[Response]]:
    user = who(request)
    name = name if name is not None else request.query_params.get("share", "")
    try:
        s = shares.get(name)
    except FsError as e:
        return user, None, JSONResponse({"detail": str(e)}, 404)
    if not shares.can(user, s, mode):
        return user, s, _deny(user)
    return user, s, None


def _err(e: Exception, status: int = 400) -> JSONResponse:
    return JSONResponse({"detail": str(e)}, status)


# ---- files ---------------------------------------------------------------------------------------------------------- #

def send_file(s: dict, rel: str, request: Request, download: bool = False) -> Response:
    rel = shares.clean(rel)
    hit = shares.locate(s, rel)
    name = rel.rsplit("/", 1)[-1] or s["name"]
    disp = f"attachment; filename*=UTF-8''{urllib.parse.quote(name)}" if download else f"inline; filename*=UTF-8''{urllib.parse.quote(name)}"
    mt = mimetypes.guess_type(name)[0] or "application/octet-stream"
    if hit and hit[1].is_file():
        try:
            from bot.fileserver import mover
            mover.record_access(s["name"], rel)
        except Exception:  # noqa: BLE001 - the access log must never block a download
            pass
        return FileResponse(hit[1], media_type=mt, headers={"Content-Disposition": disp, "Cache-Control": "private, max-age=0"})
    disk = shares.emulated(s, rel)
    if disk:
        from bot.fileserver import array
        return StreamingResponse(array.emulate(disk, f"{s['name']}/{rel}"), media_type=mt,
                                 headers={"Content-Disposition": disp, "X-ABP-Emulated": disk})
    return PlainTextResponse("Not found", 404)


async def write_stream(dest: Path, stream, overwrite: bool) -> int:
    if dest.exists() and not overwrite:
        raise FsError(f"{dest.name} exists")
    tmp = dest.with_name(dest.name + f".{secrets.token_hex(4)}.abp-upload")
    n = 0
    try:
        with open(tmp, "wb") as f:
            async for chunk in stream:
                f.write(chunk)
                n += len(chunk)
        os.replace(tmp, dest)
    finally:
        if tmp.exists():
            tmp.unlink()
    return n


def _copy(src_share: dict, src: str, dst_share: dict, dst: str) -> int:
    n = 0
    for rel, real in list(shares.walk(src_share, src)):
        sub = rel[len(shares.clean(src)):].lstrip("/")
        target_rel = f"{shares.clean(dst)}/{sub}" if sub else shares.clean(dst)
        dest = shares.place(dst_share, target_rel, real.stat().st_size)
        shutil.copy2(real, dest)
        n += 1
    if not n:
        raise FsError(f"{src} does not exist")
    return n


# ---- resumable uploads ---------------------------------------------------------------------------------------------- #

def _upload(uid: str) -> dict:
    u = load("uploads", {}).get(uid)
    if not u:
        raise FsError("no such upload (it finished, expired or never started)")
    return u


def build_app() -> Starlette:
    async def whoami(request: Request):
        u = who(request)
        return JSONResponse({"user": u and {k: u[k] for k in ("name", "admin")}, "server": "ABP File Server"})

    async def list_shares(request: Request):
        u = who(request)
        out = []
        for name, s in shares.shares().items():
            s = {**s, "name": name}
            if shares.can(u, s, "r"):
                out.append({"name": name, "comment": s["comment"], "write": shares.can(u, s, "w"), "access": s["access"],
                            "kind": "folder" if s.get("path") else "user share"})
        if not out and not u:
            return _deny(None)
        return JSONResponse(out)

    async def list_dir(request: Request):
        user, s, bad = _share(request, "r")
        if bad:
            return bad
        try:
            return JSONResponse({"share": s["name"], "path": shares.clean(request.query_params.get("path", "")),
                                 "write": shares.can(user, s, "w"),
                                 "entries": await asyncio.to_thread(shares.listing, s, request.query_params.get("path", ""))})
        except FsError as e:
            return _err(e)

    async def file_ep(request: Request):
        mode = "r" if request.method in ("GET", "HEAD") else "w"
        user, s, bad = _share(request, mode)
        if bad:
            return bad
        rel = request.query_params.get("path", "")
        try:
            if request.method in ("GET", "HEAD"):
                return send_file(s, rel, request, download=request.query_params.get("download") == "1")
            if request.method == "PUT":
                size = int(request.headers.get("content-length") or 0)
                dest = await asyncio.to_thread(shares.place, s, rel, size)
                n = await write_stream(dest, request.stream(), request.query_params.get("overwrite") == "1")
                return JSONResponse({"written": n, "path": shares.clean(rel)}, 201)
            if request.method == "DELETE":
                return JSONResponse({"removed": await asyncio.to_thread(shares.remove_path, s, rel)})
        except FsError as e:
            return _err(e, 409 if "exists" in str(e) else 400)
        return PlainTextResponse("method not allowed", 405)

    async def mkdir(request: Request):
        b = await request.json()
        user, s, bad = _share(request, "w", b.get("share", ""))
        if bad:
            return bad
        try:
            rel = shares.clean(b.get("path", ""))
            await asyncio.to_thread(shares.make_dir, s, rel)
            return JSONResponse({"created": rel}, 201)
        except FsError as e:
            return _err(e, 409 if "exists" in str(e) else 400)

    async def move_or_copy(request: Request):
        b = await request.json()
        user, s, bad = _share(request, "w" if request.url.path.endswith("move") else "r", b.get("share", ""))
        if bad:
            return bad
        user, d, bad = _share(request, "w", b.get("to_share") or b.get("share", ""))
        if bad:
            return bad
        try:
            if request.url.path.endswith("move"):
                await asyncio.to_thread(shares.move, s, b["from"], d, b["to"], bool(b.get("overwrite")))
                return JSONResponse({"moved": True})
            return JSONResponse({"copied": await asyncio.to_thread(_copy, s, b["from"], d, b["to"])})
        except (FsError, KeyError) as e:
            return _err(e)

    async def upload_start(request: Request):
        b = await request.json()
        user, s, bad = _share(request, "w", b.get("share", ""))
        if bad:
            return bad
        try:
            size = int(b.get("size", 0))
            dest = await asyncio.to_thread(shares.place, s, b.get("path", ""), size)
            if dest.exists() and not b.get("overwrite"):
                return _err(FsError(f"{dest.name} exists"), 409)
            uid = secrets.token_urlsafe(12)
            tmp = dest.with_name(dest.name + f".{uid[:8]}.abp-upload")
            tmp.touch()
            rec = {"share": s["name"], "path": shares.clean(b.get("path", "")), "dest": str(dest), "tmp": str(tmp), "size": size,
                   "offset": 0, "user": user["name"] if user else None, "started": int(time.time()), "sha256": b.get("sha256", "")}
            update("uploads", {}, lambda us: us.__setitem__(uid, rec))
            return JSONResponse({"id": uid, "offset": 0}, 201)
        except FsError as e:
            return _err(e)

    async def upload_ep(request: Request):
        uid = request.path_params["uid"]
        try:
            u = _upload(uid)
        except FsError as e:
            return _err(e, 404)
        user, s, bad = _share(request, "w", u["share"])
        if bad:
            return bad
        tmp = Path(u["tmp"])
        have = tmp.stat().st_size if tmp.exists() else 0
        if request.method in ("GET", "HEAD"):
            return JSONResponse({"offset": have, "size": u["size"]}, headers={"Upload-Offset": str(have)})
        if request.method == "DELETE":
            tmp.unlink(missing_ok=True)
            update("uploads", {}, lambda us: us.pop(uid, None))
            return JSONResponse({"cancelled": True})
        off = int(request.headers.get("upload-offset", "-1"))
        if off != have:
            return JSONResponse({"detail": f"the upload is at {have}, not {off}", "offset": have}, 409, headers={"Upload-Offset": str(have)})
        n = 0
        with open(tmp, "ab") as f:
            async for chunk in request.stream():
                n += len(chunk)
                if have + n > u["size"] or n > UPLOAD_CHUNK_MAX:
                    f.truncate(have)
                    return _err(FsError("more data than the upload's size"))
                f.write(chunk)
        have += n
        done = have == u["size"]
        if done:
            if u.get("sha256"):
                h = hashlib.sha256()
                with open(tmp, "rb") as f:
                    for block in iter(lambda: f.read(1 << 20), b""):
                        h.update(block)
                if h.hexdigest() != u["sha256"].lower():
                    tmp.unlink(missing_ok=True)
                    update("uploads", {}, lambda us: us.pop(uid, None))
                    return _err(FsError("the file arrived damaged (its SHA-256 does not match); upload it again"), 422)
            os.replace(tmp, u["dest"])
            update("uploads", {}, lambda us: us.pop(uid, None))
        return JSONResponse({"offset": have, "done": done}, headers={"Upload-Offset": str(have)})

    async def thumb(request: Request):
        user, s, bad = _share(request, "r")
        if bad:
            return bad
        size = max(32, min(1024, int(request.query_params.get("size", 256))))
        hit = shares.locate(s, request.query_params.get("path", ""))
        if not hit or not hit[1].is_file():
            return PlainTextResponse("Not found", 404)
        p = hit[1]
        st = p.stat()
        key = hashlib.sha1(f"{p}|{st.st_mtime_ns}|{size}".encode()).hexdigest()
        cache = root() / "thumbs" / key[:2] / f"{key}.jpg"
        if not cache.exists():
            def make():
                from PIL import Image, ImageOps
                cache.parent.mkdir(parents=True, exist_ok=True)
                with Image.open(p) as im:
                    im = ImageOps.exif_transpose(im)
                    im.thumbnail((size, size))
                    im.convert("RGB").save(cache, "JPEG", quality=82)
            try:
                await asyncio.to_thread(make)
            except Exception:  # noqa: BLE001 - not an image Pillow can read
                return PlainTextResponse("no thumbnail", 415)
        return FileResponse(cache, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=86400"})

    async def search(request: Request):
        user = who(request)
        q = request.query_params.get("q", "").strip()
        if not q:
            return JSONResponse([])
        from bot.fileserver import index
        visible = [n for n, s in shares.shares().items() if shares.can(user, {**s, "name": n}, "r")]
        only = request.query_params.get("share")
        res = await asyncio.to_thread(index.search, q, shares=[only] if only in visible else visible,
                                      mode=request.query_params.get("mode", "auto"), limit=int(request.query_params.get("limit", 50)))
        return JSONResponse(res)

    async def links_ep(request: Request):
        user = who(request)
        if not user:
            return _deny(None)
        if request.method == "GET":
            mine = [l for l in shares.links() if user.get("admin") or l["by"] == user["name"]]
            return JSONResponse(mine)
        if request.method == "DELETE":
            tok = request.path_params.get("token", "")
            l = next((x for x in shares.links() if x["token"] == tok), None)
            if not l or not (user.get("admin") or l["by"] == user["name"]):
                return _err(FsError("no such link"), 404)
            return JSONResponse({"removed": shares.remove_link(tok)})
        b = await request.json()
        _, s, bad = _share(request, "r", b.get("share", ""))
        if bad:
            return bad
        if b.get("allow_upload") and not shares.can(user, s, "w"):
            return _deny(user)
        try:
            return JSONResponse(shares.create_link(s["name"], b.get("path", ""), password=b.get("password", ""),
                                                   expires_days=float(b.get("expires_days", 7)), max_downloads=int(b.get("max_downloads", 0)),
                                                   allow_upload=bool(b.get("allow_upload")), created_by=user["name"]), 201)
        except FsError as e:
            return _err(e)

    async def link_ep(request: Request):
        tok = request.path_params["token"]
        sub = shares.clean(request.path_params.get("rest", ""))
        pw = request.query_params.get("pw", "") or request.cookies.get(f"abpl_{tok[:8]}", "")
        try:
            l = shares.open_link(tok, pw)
        except PermissionError:
            return HTMLResponse(_link_password_page(tok), 401)
        except FsError as e:
            return HTMLResponse(f"<p>{e}</p>", 410)
        s = shares.get(l["share"])
        rel = "/".join(x for x in (l["path"], sub) if x)
        if request.method == "PUT":
            if not l["allow_upload"] or not sub:
                return PlainTextResponse("uploads are not allowed on this link", 403)
            dest = await asyncio.to_thread(shares.place, s, rel, int(request.headers.get("content-length") or 0))
            try:
                n = await write_stream(dest, request.stream(), False)
            except FsError as e:
                return _err(e, 409)
            return JSONResponse({"written": n}, 201)
        hit = shares.locate(s, rel)
        if hit and hit[1].is_dir():
            entries = await asyncio.to_thread(shares.listing, s, rel)
            if request.query_params.get("json"):
                return JSONResponse({"entries": entries, "upload": l["allow_upload"]})
            resp = HTMLResponse(_link_folder_page(tok, sub, entries, l["allow_upload"]))
        else:
            shares.count_download(tok)
            resp = send_file(s, rel, request, download=request.query_params.get("download") == "1")
        if pw:
            resp.set_cookie(f"abpl_{tok[:8]}", pw, httponly=True, samesite="strict", max_age=86400)
        return resp

    async def login(request: Request):
        if request.headers.get("content-type", "").startswith("application/json"):
            b = await request.json()
        else:
            b = dict(await request.form())
        u = shares.check_user(str(b.get("user", "")), str(b.get("password", "")))
        if not u:
            return _err(FsError("wrong user name or password (or too many tries: wait a minute)"), 401)
        r = JSONResponse({"user": u})
        r.set_cookie(COOKIE, make_session(u["name"]), httponly=True, samesite="strict", max_age=SESSION_DAYS * 86400,
                     secure=request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https")
        return r

    async def logout(request: Request):
        r = JSONResponse({"signed_out": True})
        r.delete_cookie(COOKIE)
        return r

    async def ui(request: Request):
        return FileResponse(STATIC / "index.html", media_type="text/html", headers={"Cache-Control": "no-cache"})

    async def health(request: Request):
        return JSONResponse({"ok": True, "pid": os.getpid(), "shares": len(shares.shares())})

    from bot.fileserver import dav
    routes = [
        Route("/", ui), Route("/api/health", health), Route("/api/whoami", whoami), Route("/api/shares", list_shares),
        Route("/api/list", list_dir), Route("/api/file", file_ep, methods=["GET", "HEAD", "PUT", "DELETE"]),
        Route("/api/mkdir", mkdir, methods=["POST"]), Route("/api/move", move_or_copy, methods=["POST"]),
        Route("/api/copy", move_or_copy, methods=["POST"]), Route("/api/uploads", upload_start, methods=["POST"]),
        Route("/api/uploads/{uid}", upload_ep, methods=["GET", "HEAD", "PATCH", "DELETE"]), Route("/api/thumb", thumb),
        Route("/api/search", search), Route("/api/links", links_ep, methods=["GET", "POST"]),
        Route("/api/links/{token}", links_ep, methods=["DELETE"]),
        Route("/s/{token}", link_ep, methods=["GET", "HEAD", "PUT"]), Route("/s/{token}/{rest:path}", link_ep, methods=["GET", "HEAD", "PUT"]),
        Route("/login", login, methods=["POST"]), Route("/logout", logout, methods=["POST"]),
        *dav.routes(),
    ]
    return Starlette(routes=routes)


def _esc(s: str) -> str:
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


_PAGE = """<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title><style>body{{font:15px system-ui,sans-serif;max-width:860px;margin:24px auto;padding:0 16px;color:#1d2330;background:#fafbfc}}
@media (prefers-color-scheme:dark){{body{{background:#14171c;color:#e6e8ec}}a{{color:#8ab4ff}}}}
table{{width:100%;border-collapse:collapse}}td{{padding:6px 4px;border-bottom:1px solid #8883}}input,button{{font:inherit;padding:6px 10px}}</style></head><body>{body}</body></html>"""


def _link_password_page(tok: str) -> str:
    return _PAGE.format(title="Shared with you", body=f"<h2>This link needs a password</h2><form method=get action='/s/{_esc(tok)}'>"
                        "<input type=password name=pw autofocus> <button>Open</button></form>")


def _link_folder_page(tok: str, sub: str, entries: list[dict], upload: bool) -> str:
    base = f"/s/{tok}/" + (sub + "/" if sub else "")
    rows = "".join(f"<tr><td><a href='{_esc(base + urllib.parse.quote(e['name']))}{'/' if e['dir'] else ''}'>{_esc(e['name'])}{'/' if e['dir'] else ''}</a></td>"
                   f"<td>{'' if e['dir'] else _human(e['size'])}</td><td>{'' if e['dir'] else f'''<a href='{_esc(base + urllib.parse.quote(e['name']))}?download=1'>download</a>'''}</td></tr>"
                   for e in entries)
    up = (f"<p><input type=file id=f multiple> <button onclick=\"for(const f of document.getElementById('f').files)"
          f"fetch('{_esc(base)}'+encodeURIComponent(f.name),{{method:'PUT',body:f}}).then(r=>r.ok?location.reload():r.text().then(alert))\">Upload</button></p>") if upload else ""
    return _PAGE.format(title="Shared with you", body=f"<h2>Shared with you{(' — ' + _esc(sub)) if sub else ''}</h2>{up}<table>{rows}</table>")


def _human(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


def main(argv=None) -> int:
    import uvicorn
    ap = argparse.ArgumentParser(prog="python -m bot.fileserver.server", description="ABP File Server")
    ap.add_argument("--port", type=int, default=8790)
    ap.add_argument("--bind", default="0.0.0.0")
    a = ap.parse_args(argv)
    (root() / "server.json").write_text(json.dumps({"pid": os.getpid(), "port": a.port, "bind": a.bind, "started": time.time()}))
    uvicorn.run(build_app(), host=a.bind, port=a.port, log_level="warning", server_header=False, proxy_headers=True,
                forwarded_allow_ips="127.0.0.1")
    return 0


if __name__ == "__main__":
    sys.exit(main())
