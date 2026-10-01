"""Moving files between places, and keeping places in step.

Remotes (remotes.json; secrets sealed with the vault key):
    local    a folder on this machine
    share    a share of this file server (the union view: writes follow the share's allocation rules)
    abp      another ABP file server (its REST API: resumable uploads with SHA-256 checks, Range downloads)
    webdav   any WebDAV server (Nextcloud, ownCloud, another NAS, ...)
    s3       S3-compatible storage (AWS, Backblaze B2, Wasabi, MinIO, Cloudflare R2, ... ; SigV4)

A job copies a source to a destination:
    copy      new and changed files go across
    mirror    the destination becomes an exact copy (files that are not in the source are deleted there)
    two-way   changes on either side go to the other; deletions too; a file changed on both sides keeps both
              (the other side's copy is renamed "name (conflict <date>).ext")
Files are compared by size and modification time (and SHA-256 where both sides can give it). Transfers run in
parallel (`parallel`), are retried with backoff, resume where they stopped (local, share and abp destinations keep the
partial file), can be limited in speed (`limit_kbps`), and are verified (SHA-256 on arrival, when the source can say).
"""
from __future__ import annotations

import base64
import datetime as _dt
import hashlib
import hmac
import json
import os
import re
import shutil
import threading
import time
import urllib.parse
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable, Iterator, Optional
from xml.etree import ElementTree as ET

import httpx

from bot.fileserver import shares as shares_mod
from bot.fileserver.store import FsError, db, load, update

Log = Callable[[str], None]
CHUNK = 8 << 20
KINDS = {"local": ["path"], "share": ["share"], "abp": ["url", "user", "password"], "webdav": ["url", "user", "password"],
         "s3": ["endpoint", "bucket", "region", "access_key", "secret_key"]}
SECRET_FIELDS = {"password", "secret_key"}


# ---- remotes ---------------------------------------------------------------------------------------------------------- #

def remotes() -> list[dict]:
    return [{"name": k, "kind": v["kind"], **{f: v.get(f, "") for f in KINDS[v["kind"]] if f not in SECRET_FIELDS},
             "secrets_set": [f for f in KINDS[v["kind"]] if f in SECRET_FIELDS and v.get(f)]} for k, v in sorted(load("remotes", {}).items())]


def set_remote(name: str, kind: str, fields: dict) -> dict:
    if kind not in KINDS:
        raise FsError(f"a remote is one of {', '.join(KINDS)}")
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,40}", name):
        raise FsError("a remote name is letters, digits, dots, dashes")
    from bot.vault import seal
    cur = load("remotes", {}).get(name, {})
    rec = {"kind": kind}
    for f in KINDS[kind]:
        v = str(fields.get(f, "") or "")
        if f in SECRET_FIELDS:
            rec[f] = seal(v) if v else cur.get(f, "")
        else:
            rec[f] = v or cur.get(f, "")
    if kind == "local" and not Path(rec["path"]).is_dir():
        raise FsError(f"{rec['path']} is not a folder")
    if kind == "share":
        shares_mod.get(rec["share"])
    update("remotes", {}, lambda rs: rs.__setitem__(name, rec))
    return next(r for r in remotes() if r["name"] == name)


def remove_remote(name: str) -> bool:
    return update("remotes", {}, lambda rs: rs.pop(name, None) is not None)


def _secret(rec: dict, f: str) -> str:
    from bot.vault import unseal
    return unseal(rec[f]) if rec.get(f) else ""


class Endpoint:
    """One side of a job: a remote and a folder in it."""
    can_resume = False
    gives_sha = False

    def tree(self) -> dict[str, tuple[int, float]]: ...
    def read(self, rel: str, offset: int = 0) -> Iterator[bytes]: ...
    def write(self, rel: str, chunks: Iterator[bytes], size: int, mtime: float, sha256: str = "") -> None: ...
    def delete(self, rel: str) -> None: ...
    def rename(self, rel: str, new: str) -> None: ...
    def sha256(self, rel: str) -> str:
        h = hashlib.sha256()
        for c in self.read(rel):
            h.update(c)
        return h.hexdigest()


class LocalEndpoint(Endpoint):
    can_resume = True
    gives_sha = True

    def __init__(self, base: Path):
        self.base = base
        self.chunk = CHUNK            # the block size reads use; set per job from the transfer model (bot/neurallab/systune.py)

    def _p(self, rel: str) -> Path:
        return self.base / shares_mod.clean(rel)

    def tree(self):
        out = {}
        if not self.base.is_dir():
            return out
        for dirpath, dirnames, files in os.walk(self.base):
            dirnames[:] = [d for d in dirnames if not d.startswith(".abp-")]
            for f in files:
                if f.endswith((".abp-part", ".abp-upload")):
                    continue
                p = Path(dirpath) / f
                try:
                    st = p.stat()
                except OSError:
                    continue
                out[p.relative_to(self.base).as_posix()] = (st.st_size, st.st_mtime)
        return out

    def read(self, rel, offset=0):
        with open(self._p(rel), "rb") as f:
            f.seek(offset)
            for c in iter(lambda: f.read(self.chunk), b""):
                yield c

    def partial(self, rel: str) -> int:
        p = self._p(rel).with_name(self._p(rel).name + ".abp-part")
        return p.stat().st_size if p.exists() else 0

    def write(self, rel, chunks, size, mtime, sha256="", resume_from=0):
        dest = self._p(rel)
        dest.parent.mkdir(parents=True, exist_ok=True)
        part = dest.with_name(dest.name + ".abp-part")
        with open(part, "ab" if resume_from else "wb") as f:
            if resume_from:
                f.truncate(resume_from)
            for c in chunks:
                f.write(c)
        if part.stat().st_size != size:
            raise OSError(f"{rel}: {part.stat().st_size} of {size} bytes arrived")
        if sha256:
            h = hashlib.sha256()
            with open(part, "rb") as f:
                for c in iter(lambda: f.read(CHUNK), b""):
                    h.update(c)
            if h.hexdigest() != sha256:
                part.unlink()
                raise OSError(f"{rel}: arrived damaged (SHA-256 differs); it will be sent again")
        os.replace(part, dest)
        os.utime(dest, (mtime, mtime))

    def delete(self, rel):
        p = self._p(rel)
        if p.is_dir():
            shutil.rmtree(p)
        elif p.exists():
            p.unlink()

    def rename(self, rel, new):
        os.replace(self._p(rel), self._p(new))


class ShareEndpoint(LocalEndpoint):
    def __init__(self, share: str, sub: str):
        self.share = shares_mod.get(share)
        self.sub = shares_mod.clean(sub)
        super().__init__(Path("."))

    def _rel(self, rel):
        return "/".join(x for x in (self.sub, shares_mod.clean(rel)) if x)

    def tree(self):
        out = {}
        n = len(self.sub) + 1 if self.sub else 0
        for rel, real in shares_mod.walk(self.share, self.sub):
            if rel.endswith((".abp-part", ".abp-upload")):
                continue
            st = real.stat()
            out[rel[n:]] = (st.st_size, st.st_mtime)
        return out

    def _p(self, rel):
        hit = shares_mod.locate(self.share, self._rel(rel))
        return hit[1] if hit else shares_mod.place(self.share, self._rel(rel))

    def delete(self, rel):
        try:
            shares_mod.remove_path(self.share, self._rel(rel))
        except FsError:
            pass

    def rename(self, rel, new):
        shares_mod.move(self.share, self._rel(rel), self.share, self._rel(new))


class AbpEndpoint(Endpoint):
    can_resume = True

    def __init__(self, rec: dict, sub: str):
        self.url = rec["url"].rstrip("/")
        share, _, path = shares_mod.clean(sub).partition("/")
        if not share:
            raise FsError("an abp remote's path starts with the share: <share>/<folder>")
        self.share, self.sub = share, path
        self.c = httpx.Client(auth=(rec["user"], _secret(rec, "password")), timeout=httpx.Timeout(120, connect=15))

    def _rel(self, rel):
        return "/".join(x for x in (self.sub, rel) if x)

    def _ok(self, r: httpx.Response) -> httpx.Response:
        if r.status_code >= 400:
            raise OSError(f"{self.url}: {r.status_code} {r.text[:200]}")
        return r

    def tree(self):
        out = {}
        stack = [""]
        while stack:
            d = stack.pop()
            j = self._ok(self.c.get(f"{self.url}/api/list", params={"share": self.share, "path": self._rel(d)})).json()
            for e in j["entries"]:
                rel = f"{d}/{e['name']}" if d else e["name"]
                if e["dir"]:
                    stack.append(rel)
                else:
                    out[rel] = (e["size"], e["mtime"])
        return out

    def read(self, rel, offset=0):
        hdr = {"Range": f"bytes={offset}-"} if offset else {}
        with self.c.stream("GET", f"{self.url}/api/file", params={"share": self.share, "path": self._rel(rel)}, headers=hdr) as r:
            self._ok(r)
            yield from r.iter_bytes(CHUNK)

    def write(self, rel, chunks, size, mtime, sha256="", resume_from=0):
        up = self._ok(self.c.post(f"{self.url}/api/uploads", json={"share": self.share, "path": self._rel(rel), "size": size,
                                                                     "overwrite": True, "sha256": sha256})).json()
        off = 0
        buf = b""
        for c in chunks:
            buf += c
            if len(buf) >= CHUNK:
                off = self._patch(up["id"], off, buf)
                buf = b""
        if buf or size == 0:
            off = self._patch(up["id"], off, buf)

    def _patch(self, uid, off, data):
        r = self._ok(self.c.patch(f"{self.url}/api/uploads/{uid}", content=data, headers={"Upload-Offset": str(off)}))
        return r.json()["offset"]

    def delete(self, rel):
        self.c.delete(f"{self.url}/api/file", params={"share": self.share, "path": self._rel(rel)})

    def rename(self, rel, new):
        self._ok(self.c.post(f"{self.url}/api/move", json={"share": self.share, "from": self._rel(rel), "to": self._rel(new), "overwrite": True}))


class DavEndpoint(Endpoint):
    def __init__(self, rec: dict, sub: str):
        self.base = rec["url"].rstrip("/") + "/" + "/".join(urllib.parse.quote(p) for p in shares_mod.clean(sub).split("/") if p)
        self.base = self.base.rstrip("/")
        self.c = httpx.Client(auth=(rec["user"], _secret(rec, "password")), timeout=httpx.Timeout(120, connect=15))

    def _u(self, rel):
        return self.base + "/" + "/".join(urllib.parse.quote(p) for p in rel.split("/") if p)

    def tree(self):
        out = {}
        stack = [""]
        root_path = urllib.parse.urlparse(self.base).path.rstrip("/")
        while stack:
            d = stack.pop()
            r = self.c.request("PROPFIND", self._u(d) + "/", headers={"Depth": "1"})
            if r.status_code == 404:
                continue
            if r.status_code >= 400:
                raise OSError(f"PROPFIND {d}: {r.status_code}")
            for resp in ET.fromstring(r.content).iter("{DAV:}response"):
                href = urllib.parse.unquote(urllib.parse.urlparse(resp.findtext("{DAV:}href")).path).rstrip("/")
                rel = href[len(root_path):].strip("/")
                if rel == d.strip("/"):
                    continue
                if resp.find(".//{DAV:}collection") is not None:
                    stack.append(rel)
                else:
                    size = int(resp.findtext(".//{DAV:}getcontentlength") or 0)
                    lm = resp.findtext(".//{DAV:}getlastmodified")
                    mt = _dt.datetime.strptime(lm, "%a, %d %b %Y %H:%M:%S %Z").replace(tzinfo=_dt.timezone.utc).timestamp() if lm else 0
                    out[rel] = (size, mt)
        return out

    def read(self, rel, offset=0):
        hdr = {"Range": f"bytes={offset}-"} if offset else {}
        with self.c.stream("GET", self._u(rel), headers=hdr) as r:
            if r.status_code >= 400:
                raise OSError(f"GET {rel}: {r.status_code}")
            yield from r.iter_bytes(CHUNK)

    def write(self, rel, chunks, size, mtime, sha256="", resume_from=0):
        parts = rel.split("/")[:-1]
        for i in range(1, len(parts) + 1):
            self.c.request("MKCOL", self._u("/".join(parts[:i])))
        r = self.c.put(self._u(rel), content=chunks)
        if r.status_code >= 400:
            raise OSError(f"PUT {rel}: {r.status_code} {r.text[:200]}")

    def delete(self, rel):
        self.c.delete(self._u(rel))

    def rename(self, rel, new):
        self.c.request("MOVE", self._u(rel), headers={"Destination": self._u(new), "Overwrite": "T"})


class S3Endpoint(Endpoint):
    """S3 (path-style requests, SigV4)."""

    def __init__(self, rec: dict, sub: str):
        self.ep = rec["endpoint"].rstrip("/") or f"https://s3.{rec['region']}.amazonaws.com"
        self.bucket, self.region = rec["bucket"], rec["region"] or "us-east-1"
        self.key_id, self.secret = rec["access_key"], _secret(rec, "secret_key")
        self.prefix = shares_mod.clean(sub)
        self.c = httpx.Client(timeout=httpx.Timeout(300, connect=15))

    def _sign(self, method: str, key: str, query: dict, payload_hash: str, extra: Optional[dict] = None) -> tuple[str, dict]:
        host = urllib.parse.urlparse(self.ep).netloc
        path = "/" + self.bucket + ("/" + urllib.parse.quote(key, safe="/~") if key else "")
        now = _dt.datetime.now(_dt.timezone.utc)
        amz, day = now.strftime("%Y%m%dT%H%M%SZ"), now.strftime("%Y%m%d")
        q = "&".join(f"{urllib.parse.quote(k, safe='~')}={urllib.parse.quote(str(v), safe='~')}" for k, v in sorted(query.items()))
        headers = {"host": host, "x-amz-date": amz, "x-amz-content-sha256": payload_hash, **{k.lower(): v for k, v in (extra or {}).items()}}
        signed = ";".join(sorted(headers))
        canon = "\n".join([method, path, q, "".join(f"{k}:{headers[k]}\n" for k in sorted(headers)), signed, payload_hash])
        scope = f"{day}/{self.region}/s3/aws4_request"
        sts = "\n".join(["AWS4-HMAC-SHA256", amz, scope, hashlib.sha256(canon.encode()).hexdigest()])

        def h(k, m):
            return hmac.new(k, m.encode(), hashlib.sha256).digest()
        k = h(h(h(h(("AWS4" + self.secret).encode(), day), self.region), "s3"), "aws4_request")
        headers["authorization"] = (f"AWS4-HMAC-SHA256 Credential={self.key_id}/{scope}, SignedHeaders={signed}, "
                                    f"Signature={hmac.new(k, sts.encode(), hashlib.sha256).hexdigest()}")
        headers.pop("host")
        return f"{self.ep}{path}" + (f"?{q}" if q else ""), headers

    def _k(self, rel):
        return "/".join(x for x in (self.prefix, rel) if x)

    def tree(self):
        out, token = {}, None
        ns = "{http://s3.amazonaws.com/doc/2006-03-01/}"
        while True:
            q = {"list-type": "2", "prefix": (self.prefix + "/") if self.prefix else ""}
            if token:
                q["continuation-token"] = token
            url, hd = self._sign("GET", "", q, hashlib.sha256(b"").hexdigest())
            r = self.c.get(url, headers=hd)
            if r.status_code >= 400:
                raise OSError(f"S3 list: {r.status_code} {r.text[:200]}")
            x = ET.fromstring(r.content)
            for c in x.iter(f"{ns}Contents"):
                key = c.findtext(f"{ns}Key")
                rel = key[len(self.prefix) + 1:] if self.prefix else key
                if rel and not rel.endswith("/"):
                    mt = _dt.datetime.fromisoformat(c.findtext(f"{ns}LastModified").replace("Z", "+00:00")).timestamp()
                    out[rel] = (int(c.findtext(f"{ns}Size")), mt)
            if x.findtext(f"{ns}IsTruncated") == "true":
                token = x.findtext(f"{ns}NextContinuationToken")
            else:
                return out

    def read(self, rel, offset=0):
        extra = {"Range": f"bytes={offset}-"} if offset else {}
        url, hd = self._sign("GET", self._k(rel), {}, "UNSIGNED-PAYLOAD", extra)
        with self.c.stream("GET", url, headers=hd) as r:
            if r.status_code >= 400:
                raise OSError(f"S3 GET {rel}: {r.status_code}")
            yield from r.iter_bytes(CHUNK)

    def write(self, rel, chunks, size, mtime, sha256="", resume_from=0):
        data = b"".join(chunks)          # single PUT (S3 allows 5 GB); large files are sent as one request
        url, hd = self._sign("PUT", self._k(rel), {}, hashlib.sha256(data).hexdigest())
        r = self.c.put(url, content=data, headers=hd)
        if r.status_code >= 400:
            raise OSError(f"S3 PUT {rel}: {r.status_code} {r.text[:200]}")

    def delete(self, rel):
        url, hd = self._sign("DELETE", self._k(rel), {}, hashlib.sha256(b"").hexdigest())
        self.c.delete(url, headers=hd)

    def rename(self, rel, new):
        self.write(new, self.read(rel), 0, 0)
        self.delete(rel)


def endpoint(spec: dict) -> Endpoint:
    """spec: {"remote": <name> | "local" path, "path": sub-path} or {"path": "C:/x"} for a plain folder."""
    name = spec.get("remote", "")
    if not name:
        return LocalEndpoint(Path(spec["path"]))
    rec = load("remotes", {}).get(name)
    if not rec:
        raise FsError(f"no remote {name!r}")
    sub = spec.get("path", "")
    k = rec["kind"]
    if k == "local":
        return LocalEndpoint(Path(rec["path"]) / shares_mod.clean(sub))
    if k == "share":
        return ShareEndpoint(rec["share"], sub)
    if k == "abp":
        return AbpEndpoint(rec, sub)
    if k == "webdav":
        return DavEndpoint(rec, sub)
    return S3Endpoint(rec, sub)


# ---- jobs ------------------------------------------------------------------------------------------------------------- #

def jobs() -> dict:
    return load("transfer_jobs", {})


def set_job(name: str, job: dict) -> dict:
    if job.get("mode", "copy") not in ("copy", "mirror", "two-way"):
        raise FsError("mode is copy, mirror or two-way")
    for side in ("source", "dest"):
        if not isinstance(job.get(side), dict):
            raise FsError(f"{side} is {{remote, path}} or {{path}}")
    j = {"mode": "copy", "every_minutes": 0, "parallel": 0, "limit_kbps": 0, "verify": True, "exclude": [], **job}
    update("transfer_jobs", {}, lambda js: js.__setitem__(name, j))
    return j


def remove_job(name: str) -> bool:
    return update("transfer_jobs", {}, lambda js: js.pop(name, None) is not None)


class _Bucket:
    def __init__(self, kbps: int):
        self.rate = kbps * 1024
        self.allow = float(self.rate)
        self.t = time.monotonic()
        self.lock = threading.Lock()

    def take(self, n: int) -> None:
        if not self.rate:
            return
        with self.lock:
            now = time.monotonic()
            self.allow = min(self.rate, self.allow + (now - self.t) * self.rate)
            self.t = now
            self.allow -= n
            wait = -self.allow / self.rate if self.allow < 0 else 0
        if wait:
            time.sleep(wait)


def _state_con():
    con = db("transfer")
    con.executescript("""CREATE TABLE IF NOT EXISTS last(job TEXT, path TEXT, size INT, mtime REAL, PRIMARY KEY(job, path));
                         CREATE TABLE IF NOT EXISTS runs(id TEXT PRIMARY KEY, job TEXT, started INT, finished INT, result TEXT);""")
    return con


def _same(a: tuple[int, float], b: tuple[int, float]) -> bool:
    return a[0] == b[0] and abs(a[1] - b[1]) < 2.0        # FAT/SMB/S3 keep times to a second or two


def plan(job: dict, src: Endpoint, dst: Endpoint, last: dict) -> list[tuple[str, str, str]]:
    """[(action, path, detail)]: put (src -> dst), get (dst -> src), del_dst, del_src, conflict."""
    import fnmatch
    ex = job.get("exclude") or []
    keep = lambda p: not any(fnmatch.fnmatch(p, e) or fnmatch.fnmatch(p.rsplit("/", 1)[-1], e) for e in ex)  # noqa: E731
    s = {k: v for k, v in src.tree().items() if keep(k)}
    d = {k: v for k, v in dst.tree().items() if keep(k)}
    out = []
    if job["mode"] in ("copy", "mirror"):
        for p, v in s.items():
            if p not in d or not _same(v, d[p]):
                out.append(("put", p, ""))
        if job["mode"] == "mirror":
            out += [("del_dst", p, "") for p in d if p not in s]
        return out
    for p in set(s) | set(d):                            # two-way, against the state after the last run
        a, b, l = s.get(p), d.get(p), last.get(p)
        if a and b:
            if _same(a, b):
                continue
            a_changed = not l or not _same(a, l)
            b_changed = not l or not _same(b, l)
            if a_changed and b_changed:
                out.append(("conflict", p, ""))
            elif a_changed:
                out.append(("put", p, ""))
            else:
                out.append(("get", p, ""))
        elif a and not b:
            out.append(("del_src", p, "") if l and _same(a, l) else ("put", p, ""))
        elif b and not a:
            out.append(("del_dst", p, "") if l and _same(b, l) else ("get", p, ""))
    return out


def _send(a: Endpoint, b: Endpoint, rel: str, rel_to: str, size: int, mtime: float, verify: bool, bucket: _Bucket) -> int:
    sha = a.sha256(rel) if verify and a.gives_sha else ""
    start = 0
    if b.can_resume and isinstance(b, LocalEndpoint):
        start = b.partial(rel_to)
        if start > size:
            start = 0

    def stream():
        for c in a.read(rel, start):
            bucket.take(len(c))
            yield c
    if isinstance(b, LocalEndpoint):
        b.write(rel_to, stream(), size, mtime, sha, resume_from=start)
    else:
        b.write(rel_to, stream(), size, mtime, sha)
    return size - start


def run(name: str, log: Log = lambda m: None, dry_run: bool = False) -> dict:
    job = jobs().get(name)
    if not job:
        raise FsError(f"no transfer job {name!r}")
    src, dst = endpoint(job["source"]), endpoint(job["dest"])
    con = _state_con()
    last = {r[0]: (r[1], r[2]) for r in con.execute("SELECT path, size, mtime FROM last WHERE job=?", (name,))}
    actions = plan(job, src, dst, last)
    out = {"job": name, "planned": len(actions), "done": 0, "bytes": 0, "failed": [], "conflicts": 0}
    if dry_run:
        out["actions"] = actions[:500]
        return out
    rid = uuid.uuid4().hex[:10]
    con.execute("INSERT INTO runs(id, job, started) VALUES(?,?,?)", (rid, name, int(time.time())))
    con.commit()
    bucket = _Bucket(int(job.get("limit_kbps") or 0))
    s_tree, d_tree = None, None
    lock = threading.Lock()
    parallel, tuned = _tuning(job, src, dst, actions, log)
    out["parallel"], out["chunk"] = parallel, getattr(src, "chunk", CHUNK)
    t_start = time.time()

    def do(act):
        nonlocal s_tree, d_tree
        kind, p, _ = act
        for attempt in range(4):
            try:
                if kind == "put":
                    with lock:
                        s_tree = s_tree or src.tree()
                    size, mt = s_tree[p]
                    n = _send(src, dst, p, p, size, mt, job.get("verify", True), bucket)
                elif kind == "get":
                    with lock:
                        d_tree = d_tree or dst.tree()
                    size, mt = d_tree[p]
                    n = _send(dst, src, p, p, size, mt, job.get("verify", True), bucket)
                elif kind == "del_dst":
                    dst.delete(p)
                    n = 0
                elif kind == "del_src":
                    src.delete(p)
                    n = 0
                else:                                     # conflict: keep both, the destination's copy renamed
                    stem, dot, ext = p.rpartition(".")
                    tag = f" (conflict {time.strftime('%Y-%m-%d %H%M')})"
                    other = f"{stem}{tag}.{ext}" if dot and "/" not in ext else f"{p}{tag}"
                    with lock:
                        d_tree = d_tree or dst.tree()
                        s_tree = s_tree or src.tree()
                    dst.rename(p, other)
                    size, mt = d_tree[p]
                    _send(dst, src, other, other, size, mt, False, bucket)
                    n = _send(src, dst, p, p, *s_tree[p], job.get("verify", True), bucket)
                    with lock:
                        out["conflicts"] += 1
                with lock:
                    out["done"] += 1
                    out["bytes"] += n
                return
            except (OSError, httpx.HTTPError, KeyError, FsError) as e:
                if attempt == 3:
                    with lock:
                        out["failed"].append({"action": kind, "path": p, "error": str(e)[:300]})
                    log(f"{kind} {p} failed: {e}")
                    return
                time.sleep(1.5 * 2 ** attempt)

    with ThreadPoolExecutor(parallel) as ex:
        list(ex.map(do, actions))
    if tuned and out["bytes"]:
        try:                                              # a real copy: more data for the transfer model
            from bot.neurallab import systune
            systune.record_copy(str(src.base), str(dst.base), out["bytes"], time.time() - t_start, out["chunk"], parallel)
        except Exception:                                 # noqa: BLE001 - measuring must never fail a transfer
            pass
    if job["mode"] == "two-way" and not out["failed"]:
        now_s = src.tree()
        with con:
            con.execute("DELETE FROM last WHERE job=?", (name,))
            con.executemany("INSERT INTO last(job, path, size, mtime) VALUES(?,?,?,?)", [(name, p, v[0], v[1]) for p, v in now_s.items()])
    con.execute("UPDATE runs SET finished=?, result=? WHERE id=?", (int(time.time()), json.dumps(out), rid))
    con.commit()
    con.close()
    update("transfer_jobs", {}, lambda js: js[name].__setitem__("last_run", {"at": int(time.time()), **{k: out[k] for k in ("done", "bytes", "conflicts")},
                                                                              "failed": len(out["failed"])}) if name in js else None)
    log(f"transfer {name}: {out['done']}/{out['planned']} done, {out['bytes'] >> 20} MiB, {len(out['failed'])} failed")
    return out


def _tuning(job: dict, src: Endpoint, dst: Endpoint, actions: list, log: Log) -> tuple[int, bool]:
    """Parallel files and block size: the job's own setting, else (between local folders) what the transfer model
    advises for these two drives, else 4 files of 8 MiB blocks."""
    if job.get("parallel"):
        return max(1, min(16, int(job["parallel"]))), False
    if not (isinstance(src, LocalEndpoint) and isinstance(dst, LocalEndpoint)):
        return 4, False
    puts = [a for a in actions if a[0] == "put"]
    if not puts:
        return 4, False
    try:
        from bot.neurallab import systune
        tree = src.tree()
        total = sum(tree.get(a[1], (0, 0))[0] for a in puts)
        adv = systune.advise_copy(str(src.base), str(dst.base), total, len(puts))
    except Exception as e:                                # noqa: BLE001 - no advice: the defaults
        log(f"transfer tuning unavailable ({e}); 4 files at a time")
        return 4, False
    src.chunk = dst.chunk = int(adv["chunk"])
    log(f"{len(puts)} file(s), {total >> 20} MiB: {adv['parallel']} at a time, {adv['chunk'] >> 10} KiB blocks "
        f"({adv['source']}{', ~%s MB/s expected' % adv['predicted_mb_s'] if adv.get('predicted_mb_s') else ''})")
    return int(adv["parallel"]), True


def due() -> list[str]:
    now = time.time()
    return [n for n, j in jobs().items() if j.get("every_minutes") and now - (j.get("last_run") or {}).get("at", 0) >= j["every_minutes"] * 60]


def history(limit: int = 50) -> list[dict]:
    con = _state_con()
    rows = con.execute("SELECT id, job, started, finished, result FROM runs ORDER BY started DESC LIMIT ?", (limit,)).fetchall()
    con.close()
    return [{"id": r[0], "job": r[1], "started": r[2], "finished": r[3], "result": json.loads(r[4]) if r[4] else None} for r in rows]
