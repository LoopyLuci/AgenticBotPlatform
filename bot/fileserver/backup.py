"""Backups: deduplicated, compressed, encrypted and versioned, to any folder (another drive, a share, a mounted
remote disk).

A repository is a folder:
    config.json         id, chunk size, the scrypt parameters and the master key, wrapped by the password (AES-GCM)
    data/xx/<id>        chunks: zlib-compressed, AES-256-GCM encrypted; <id> = HMAC-SHA256(key, plaintext), so equal
                        content is stored once (across files, folders, snapshots and machines sharing the repository),
                        and nobody without the key can tell what a chunk holds
    snapshots/<id>      one per backup run, encrypted: every file's path, size, time and chunk list

A file unchanged since the previous snapshot (size and time) is not read again. Retention (`forget`) keeps the last N
and the newest of each day/week/month as configured; `prune` deletes chunks no snapshot uses; `check` proves every
chunk a snapshot needs is there and (for a sample, or all) that it decrypts and matches its id.

Chunks are fixed-size (4 MiB): a change inside a large file costs only the chunks it touches, an insertion shifts the
rest of that file (content-defined chunking would avoid that; it is a later step).
"""
from __future__ import annotations

import base64
import fnmatch
import hashlib
import hmac
import json
import os
import random
import secrets
import socket
import time
import uuid
import zlib
from pathlib import Path
from typing import Callable, Iterator, Optional

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

from bot.fileserver.store import FsError, load, update

Log = Callable[[str], None]
CHUNK = 4 << 20


class Repo:
    def __init__(self, path: str | Path, password: str):
        self.path = Path(path)
        cfg_f = self.path / "config.json"
        if not cfg_f.exists():
            raise FsError(f"{self.path} is not a backup repository (create it first)")
        self.cfg = json.loads(cfg_f.read_text(encoding="utf-8"))
        k = self.cfg["kdf"]
        kek = Scrypt(salt=base64.b64decode(k["salt"]), length=32, n=k["n"], r=k["r"], p=k["p"]).derive(password.encode())
        try:
            blob = base64.b64decode(self.cfg["key"])
            master = AESGCM(kek).decrypt(blob[:12], blob[12:], b"abp-backup-key")
        except InvalidTag as e:
            raise FsError("wrong repository password") from e
        self.enc_key, self.id_key = master[:32], master[32:]
        self.aes = AESGCM(self.enc_key)

    @staticmethod
    def create(path: str | Path, password: str) -> "Repo":
        p = Path(path)
        if (p / "config.json").exists():
            raise FsError(f"{p} already holds a repository")
        if len(password) < 10:
            raise FsError("use a repository password of at least 10 characters (it cannot be recovered)")
        (p / "data").mkdir(parents=True, exist_ok=True)
        (p / "snapshots").mkdir(exist_ok=True)
        salt = os.urandom(16)
        n, r, par = 2 ** 15, 8, 1
        kek = Scrypt(salt=salt, length=32, n=n, r=r, p=par).derive(password.encode())
        master = os.urandom(64)
        nonce = os.urandom(12)
        cfg = {"version": 1, "id": uuid.uuid4().hex, "chunk": CHUNK, "created": int(time.time()),
               "kdf": {"salt": base64.b64encode(salt).decode(), "n": n, "r": r, "p": par},
               "key": base64.b64encode(nonce + AESGCM(kek).encrypt(nonce, master, b"abp-backup-key")).decode()}
        (p / "config.json").write_text(json.dumps(cfg, indent=1), encoding="utf-8")
        return Repo(p, password)

    # -- objects --
    def chunk_id(self, data: bytes) -> str:
        return hmac.new(self.id_key, data, hashlib.sha256).hexdigest()

    def _chunk_path(self, cid: str) -> Path:
        return self.path / "data" / cid[:2] / cid

    def _seal(self, data: bytes, aad: bytes) -> bytes:
        nonce = os.urandom(12)
        return nonce + self.aes.encrypt(nonce, zlib.compress(data, 6), aad)

    def _open(self, blob: bytes, aad: bytes) -> bytes:
        return zlib.decompress(self.aes.decrypt(blob[:12], blob[12:], aad))

    def has(self, cid: str) -> bool:
        return self._chunk_path(cid).exists()

    def put(self, data: bytes) -> tuple[str, bool]:
        cid = self.chunk_id(data)
        p = self._chunk_path(cid)
        if p.exists():
            return cid, False
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp" + secrets.token_hex(3))
        tmp.write_bytes(self._seal(data, cid.encode()))
        os.replace(tmp, p)
        return cid, True

    def get(self, cid: str) -> bytes:
        try:
            data = self._open(self._chunk_path(cid).read_bytes(), cid.encode())
        except FileNotFoundError as e:
            raise FsError(f"chunk {cid[:12]}… is missing from the repository") from e
        except (InvalidTag, zlib.error) as e:
            raise FsError(f"chunk {cid[:12]}… is damaged") from e
        if not hmac.compare_digest(self.chunk_id(data), cid):
            raise FsError(f"chunk {cid[:12]}… does not match its id")
        return data

    # -- snapshots --
    def snapshots(self) -> list[dict]:
        out = []
        for f in sorted((self.path / "snapshots").glob("*.snap")):
            try:
                s = json.loads(self._open(f.read_bytes(), b"snapshot"))
            except (InvalidTag, zlib.error, ValueError):
                continue
            out.append({k: s[k] for k in ("id", "time", "host", "sources", "stats", "tags") if k in s})
        return sorted(out, key=lambda s: s["time"])

    def snapshot(self, sid: str) -> dict:
        hits = list((self.path / "snapshots").glob(f"*{sid}*.snap"))
        if len(hits) != 1:
            raise FsError(f"no single snapshot matches {sid!r}")
        return json.loads(self._open(hits[0].read_bytes(), b"snapshot"))

    def save_snapshot(self, snap: dict) -> None:
        f = self.path / "snapshots" / f"{time.strftime('%Y%m%d-%H%M%S', time.localtime(snap['time']))}-{snap['id']}.snap"
        f.write_bytes(self._seal(json.dumps(snap).encode(), b"snapshot"))

    def delete_snapshot(self, sid: str) -> None:
        for f in (self.path / "snapshots").glob(f"*{sid}.snap"):
            f.unlink()


def _walk(src: Path, exclude: list[str]) -> Iterator[tuple[str, Path]]:
    for dirpath, dirnames, files in os.walk(src):
        rel_dir = Path(dirpath).relative_to(src).as_posix()
        rel_dir = "" if rel_dir == "." else rel_dir + "/"
        dirnames[:] = [d for d in dirnames if not d.startswith(".abp-") and not any(fnmatch.fnmatch(rel_dir + d, e) for e in exclude)]
        for f in files:
            rel = rel_dir + f
            if any(fnmatch.fnmatch(rel, e) or fnmatch.fnmatch(f, e) for e in exclude):
                continue
            yield rel, Path(dirpath) / f


def backup(repo: Repo, sources: list[str], exclude: Optional[list[str]] = None, tags: Optional[list[str]] = None,
           log: Log = lambda m: None) -> dict:
    prev_files: dict[tuple[str, str], dict] = {}
    snaps = [s for s in repo.snapshots() if sorted(s["sources"]) == sorted(sources)]
    if snaps:
        for f in repo.snapshot(snaps[-1]["id"])["files"]:
            prev_files[(f["source"], f["path"])] = f
    files, stats = [], {"files": 0, "bytes": 0, "new_chunks": 0, "new_bytes_stored": 0, "unchanged_files": 0, "errors": 0}
    t0 = time.time()
    for src in sources:
        base = Path(src)
        if not base.is_dir():
            raise FsError(f"{src} is not a folder")
        for rel, p in _walk(base, exclude or []):
            try:
                st = p.stat()
            except OSError:
                stats["errors"] += 1
                continue
            prev = prev_files.get((src, rel))
            entry = {"source": src, "path": rel, "size": st.st_size, "mtime": st.st_mtime}
            if prev and prev["size"] == st.st_size and abs(prev["mtime"] - st.st_mtime) < 1e-3 and all(repo.has(c) for c in prev["chunks"]):
                entry["chunks"] = prev["chunks"]
                stats["unchanged_files"] += 1
            else:
                chunks = []
                try:
                    with open(p, "rb") as f:
                        for data in iter(lambda: f.read(CHUNK), b""):
                            cid, new = repo.put(data)
                            chunks.append(cid)
                            if new:
                                stats["new_chunks"] += 1
                                stats["new_bytes_stored"] += repo._chunk_path(cid).stat().st_size
                except OSError as e:
                    log(f"cannot read {p}: {e}")
                    stats["errors"] += 1
                    continue
                entry["chunks"] = chunks
            files.append(entry)
            stats["files"] += 1
            stats["bytes"] += st.st_size
    stats["seconds"] = round(time.time() - t0, 1)
    snap = {"id": uuid.uuid4().hex[:12], "time": time.time(), "host": socket.gethostname(), "sources": sources, "tags": tags or [],
            "stats": stats, "files": files}
    repo.save_snapshot(snap)
    log(f"snapshot {snap['id']}: {stats['files']} files, {stats['bytes'] >> 20} MiB; {stats['new_chunks']} new chunks "
        f"({stats['new_bytes_stored'] >> 20} MiB stored)")
    return {"snapshot": snap["id"], **stats}


def restore(repo: Repo, sid: str, target: str, include: str = "", log: Log = lambda m: None) -> dict:
    snap = repo.snapshot(sid)
    tgt = Path(target)
    n = b = 0
    for f in snap["files"]:
        if include and not (f["path"] == include or f["path"].startswith(include.rstrip("/") + "/") or fnmatch.fnmatch(f["path"], include)):
            continue
        sub = Path(f["source"]).name if len(snap["sources"]) > 1 else ""
        dest = tgt / sub / f["path"]
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(dest.name + ".abp-part")
        with open(tmp, "wb") as out:
            for cid in f["chunks"]:
                out.write(repo.get(cid))
        os.replace(tmp, dest)
        os.utime(dest, (f["mtime"], f["mtime"]))
        n += 1
        b += f["size"]
    log(f"restored {n} files ({b >> 20} MiB) from {sid} to {target}")
    return {"files": n, "bytes": b}


def forget(repo: Repo, keep_last: int = 0, keep_daily: int = 0, keep_weekly: int = 0, keep_monthly: int = 0,
           dry_run: bool = False) -> dict:
    snaps = list(reversed(repo.snapshots()))          # newest first
    keep: set[str] = set(s["id"] for s in snaps[:keep_last])
    for count, fmt in ((keep_daily, "%Y-%m-%d"), (keep_weekly, "%G-%V"), (keep_monthly, "%Y-%m")):
        seen = []
        for s in snaps:
            key = time.strftime(fmt, time.localtime(s["time"]))
            if key not in seen and len(seen) < count:
                seen.append(key)
                keep.add(s["id"])
    if not (keep_last or keep_daily or keep_weekly or keep_monthly):
        keep = {s["id"] for s in snaps}
    drop = [s["id"] for s in snaps if s["id"] not in keep]
    if not dry_run:
        for sid in drop:
            repo.delete_snapshot(sid)
    return {"kept": len(keep), "forgotten": drop}


def prune(repo: Repo) -> dict:
    used: set[str] = set()
    for s in repo.snapshots():
        for f in repo.snapshot(s["id"])["files"]:
            used.update(f["chunks"])
    removed = freed = 0
    for d in (repo.path / "data").iterdir():
        for c in d.iterdir():
            if c.name not in used:
                freed += c.stat().st_size
                c.unlink()
                removed += 1
    return {"removed_chunks": removed, "freed_bytes": freed, "chunks_in_use": len(used)}


def check(repo: Repo, read_percent: float = 5.0) -> dict:
    used: set[str] = set()
    for s in repo.snapshots():
        for f in repo.snapshot(s["id"])["files"]:
            used.update(f["chunks"])
    missing = [c for c in used if not repo.has(c)]
    sample = [c for c in used if c not in missing]
    random.shuffle(sample)
    sample = sample[: max(1 if sample else 0, int(len(sample) * read_percent / 100))]
    damaged = []
    for c in sample:
        try:
            repo.get(c)
        except FsError:
            damaged.append(c)
    return {"snapshots": len(repo.snapshots()), "chunks": len(used), "missing": len(missing), "read": len(sample),
            "damaged": len(damaged), "ok": not missing and not damaged}


# ---- configured jobs (scheduled) -------------------------------------------------------------------------------------- #

def jobs() -> dict:
    return {k: {kk: vv for kk, vv in v.items() if kk != "password"} | {"password_set": bool(v.get("password"))}
            for k, v in load("backup_jobs", {}).items()}


def set_job(name: str, repo_path: str, sources: list[str], password: str = "", every_hours: float = 24, exclude=None,
            keep: Optional[dict] = None) -> dict:
    from bot.vault import seal
    cur = load("backup_jobs", {}).get(name, {})
    if not password and not cur.get("password"):
        raise FsError("the repository password is needed (it is kept sealed so scheduled backups can run)")
    pw = password or None
    if pw:
        if not (Path(repo_path) / "config.json").exists():
            Repo.create(repo_path, pw)
        else:
            Repo(repo_path, pw)                     # proves the password
    job = {"repo": repo_path, "sources": sources, "every_hours": every_hours, "exclude": exclude or [],
           "keep": keep or {"keep_last": 3, "keep_daily": 7, "keep_weekly": 4, "keep_monthly": 12},
           "password": seal(pw) if pw else cur["password"], "last_run": cur.get("last_run")}
    update("backup_jobs", {}, lambda js: js.__setitem__(name, job))
    return jobs()[name]


def _repo_for(name: str) -> tuple[dict, Repo]:
    from bot.vault import unseal
    job = load("backup_jobs", {}).get(name)
    if not job:
        raise FsError(f"no backup job {name!r}")
    return job, Repo(job["repo"], unseal(job["password"]))


def run_job(name: str, log: Log = lambda m: None) -> dict:
    job, repo = _repo_for(name)
    res = backup(repo, job["sources"], job.get("exclude"), tags=[name], log=log)
    res["forget"] = forget(repo, **job.get("keep", {}))
    if res["forget"]["forgotten"]:
        res["prune"] = prune(repo)
    update("backup_jobs", {}, lambda js: js[name].__setitem__("last_run", {"at": int(time.time()), "snapshot": res["snapshot"],
                                                                          "files": res["files"], "errors": res["errors"]}))
    return res


def job_repo(name: str) -> Repo:
    return _repo_for(name)[1]


def remove_job(name: str) -> bool:
    return update("backup_jobs", {}, lambda js: js.pop(name, None) is not None)


def due() -> list[str]:
    now = time.time()
    return [n for n, j in load("backup_jobs", {}).items() if j.get("every_hours") and
            now - ((j.get("last_run") or {}).get("at") or 0) >= j["every_hours"] * 3600]
