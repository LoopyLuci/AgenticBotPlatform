"""Shares, users and links.

A share is either
  - a *user share* across the array (Unraid's user shares): the folder `<share>` at the top of every allowed data disk
    and of a cache pool, seen as one tree. Reads find a file on whichever disk has it (the cache first); new files go
    where the share's rules say:
        cache        no | yes (new files to the cache, the mover moves them to the array) | only (cache only) |
                     prefer (stay on the cache while it has room; the mover moves them back from the array)
        allocation   highwater (fill disks in turn to half, then a quarter... of the largest: few disks spin) |
                     mostfree | fillup (in order, until min_free is left)
        split_level  how many folder levels below the share may be split across disks (0: any; 1: each top folder
                     stays on one disk; ...)
        disks / exclude_disks
  - or a *folder share*: one folder anywhere (`path`), served as it is.

Access, like Unraid's SMB security: public (everyone reads and writes, no sign-in), secure (everyone reads, the listed
users with "rw" write), private (only the listed users). The dashboard token is always an administrator.

Deleted files go to a recycle bin (`.abp-recycle` on the same disk; kept `recycle_days`) unless the share turns it off.
Links share one file or folder with anyone who has the address: optional password, expiry, download limit, uploads.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import re
import secrets
import shutil
import time
from pathlib import Path, PurePosixPath
from typing import Iterator, Optional

from bot.fileserver.store import FsError, load, save, update

_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._-]{0,63}$")
RECYCLE = ".abp-recycle"
DEFAULTS = {"comment": "", "cache": "no", "cache_pool": "cache", "allocation": "highwater", "split_level": 0,
            "min_free_gb": 2.0, "disks": [], "exclude_disks": [], "access": "private", "users": {},
            "recycle_bin": True, "recycle_days": 30, "exports": {"webdav": True, "smb": False, "nfs": False}}


# ---- pools (cache) ------------------------------------------------------------------------------------------------ #

def pools() -> dict:
    return load("pools", {})


def set_pool(name: str, path: str) -> dict:
    if not _NAME.match(name):
        raise FsError("a pool name is letters, digits, dots, dashes")
    p = Path(path).expanduser()
    p.mkdir(parents=True, exist_ok=True)
    update("pools", {}, lambda ps: ps.__setitem__(name, {"path": str(p)}))
    return pools()[name]


def remove_pool(name: str) -> bool:
    users_of = [s for s, v in shares().items() if v.get("cache") != "no" and v.get("cache_pool") == name]
    if users_of:
        raise FsError(f"shares {', '.join(users_of)} use this pool")
    return update("pools", {}, lambda ps: ps.pop(name, None) is not None)


# ---- shares ------------------------------------------------------------------------------------------------------- #

def shares() -> dict:
    return {k: {**DEFAULTS, **v} for k, v in load("shares", {}).items()}


def get(name: str) -> dict:
    s = shares().get(name)
    if not s:
        hits = [k for k in shares() if k.lower() == name.lower()]
        if not hits:
            raise FsError(f"no share {name!r}")
        name, s = hits[0], shares()[hits[0]]
    return {**s, "name": name}


def _validate(name: str, s: dict) -> None:
    if not _NAME.match(name) or name.startswith(".") or name.lower() in ("api", "dav", "s", "ui", "login", "static"):
        raise FsError(f"{name!r} cannot be a share name")
    if s.get("path"):
        if not Path(s["path"]).is_dir():
            raise FsError(f"{s['path']} is not a folder")
    else:
        from bot.fileserver import array
        if not array.config()["disks"] and s.get("cache") != "only":
            raise FsError("a user share needs the array (set up disks) or cache 'only'; or give the share a folder (path)")
    if s["cache"] not in ("no", "yes", "only", "prefer"):
        raise FsError("cache is no, yes, only or prefer")
    if s["cache"] != "no" and s["cache_pool"] not in pools():
        raise FsError(f"there is no pool {s['cache_pool']!r}: add one (a folder on a fast drive) first")
    if s["allocation"] not in ("highwater", "mostfree", "fillup"):
        raise FsError("allocation is highwater, mostfree or fillup")
    if s["access"] not in ("public", "secure", "private"):
        raise FsError("access is public, secure or private")
    for u, mode in (s.get("users") or {}).items():
        if mode not in ("r", "rw"):
            raise FsError(f"user {u}: access is r or rw")


def create(name: str, settings: dict) -> dict:
    if name in shares():
        raise FsError(f"share {name} exists")
    s = {**DEFAULTS, **settings}
    _validate(name, s)
    update("shares", {}, lambda all_: all_.__setitem__(name, {k: v for k, v in s.items() if k != "name"}))
    return get(name)


def edit(name: str, changes: dict) -> dict:
    cur = get(name)
    name = cur.pop("name")
    s = {**cur, **changes}
    _validate(name, s)
    update("shares", {}, lambda all_: all_.__setitem__(name, {k: v for k, v in s.items() if k != "name"}))
    return get(name)


def remove(name: str) -> bool:
    """Forget the share (its files stay on the disks)."""
    name = get(name)["name"]
    return update("shares", {}, lambda all_: all_.pop(name, None) is not None)


# ---- users and access --------------------------------------------------------------------------------------------- #

def _pw_hash(pw: str, salt: Optional[bytes] = None) -> str:
    salt = salt or os.urandom(16)
    return "pbkdf2$" + base64.b64encode(salt).decode() + "$" + base64.b64encode(
        hashlib.pbkdf2_hmac("sha256", pw.encode(), salt, 200_000)).decode()


def _pw_ok(pw: str, stored: str) -> bool:
    try:
        _, salt, want = stored.split("$")
        return hmac.compare_digest(hashlib.pbkdf2_hmac("sha256", pw.encode(), base64.b64decode(salt), 200_000),
                                   base64.b64decode(want))
    except (ValueError, TypeError):
        return False


def users() -> list[dict]:
    return [{"name": k, "admin": bool(v.get("admin")), "created": v.get("created")} for k, v in sorted(load("users", {}).items())]


def set_user(name: str, password: str, admin: bool = False) -> dict:
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,32}", name):
        raise FsError("a user name is 1-32 letters, digits, dots, dashes, underscores")
    if len(password) < 8:
        raise FsError("a password has at least 8 characters")
    update("users", {}, lambda us: us.__setitem__(name, {"password_hash": _pw_hash(password), "admin": admin,
                                                         "created": us.get(name, {}).get("created") or int(time.time())}))
    return {"name": name, "admin": admin}


def remove_user(name: str) -> bool:
    return update("users", {}, lambda us: us.pop(name, None) is not None)


_fails: dict[str, list[float]] = {}


def check_user(name: str, password: str) -> Optional[dict]:
    """The user if the password is right. Five wrong guesses in a minute lock the name for a minute."""
    now = time.time()
    recent = [t for t in _fails.get(name, []) if now - t < 60]
    if len(recent) >= 5:
        return None
    u = load("users", {}).get(name)
    if u and _pw_ok(password, u["password_hash"]):
        _fails.pop(name, None)
        return {"name": name, "admin": bool(u.get("admin"))}
    _fails[name] = recent + [now]
    return None


def can(user: Optional[dict], share: dict, mode: str) -> bool:
    """mode: 'r' or 'w'. user None = not signed in; {"admin": True} = the dashboard token or an admin."""
    if user and user.get("admin"):
        return True
    if mode == "w":
        from bot.fileserver.guard import frozen
        if share.get("name") in frozen():          # the guard froze it: read-only until a person unfreezes it
            return False
    acc = share.get("access", "private")
    if acc == "public":
        return True
    granted = (share.get("users") or {}).get(user["name"]) if user else None
    if mode == "r":
        return acc == "secure" or granted in ("r", "rw")
    return granted == "rw"


# ---- paths: the union view ---------------------------------------------------------------------------------------- #

def clean(rel: str) -> str:
    """A path inside a share, normalised; refuses anything that climbs out or hides in ABP's own folders."""
    rel = (rel or "").replace("\\", "/").strip("/")
    parts = []
    for p in rel.split("/"):
        if p in ("", "."):
            continue
        if p == ".." or p.startswith(".abp-") or ":" in p or "\0" in p:
            raise FsError(f"not a valid path: {rel!r}")
        parts.append(p)
    return "/".join(parts)


def branches(share: dict) -> list[tuple[str, Path]]:
    """(branch name, the share's folder on it), the cache first; disks in array order."""
    if share.get("path"):
        return [("folder", Path(share["path"]))]
    from bot.fileserver import array
    out = []
    if share["cache"] != "no":
        out.append((f"pool:{share['cache_pool']}", Path(pools()[share["cache_pool"]]["path"]) / share["name"]))
    if share["cache"] != "only":
        allowed = set(share.get("disks") or []) or None
        for d in array.config()["disks"]:
            if (allowed is None or d["name"] in allowed) and d["name"] not in (share.get("exclude_disks") or []):
                out.append((d["name"], Path(d["path"]) / share["name"]))
    return out


def locate(share: dict, rel: str) -> Optional[tuple[str, Path]]:
    """Where an existing file or folder is (the first branch that has it)."""
    rel = clean(rel)
    for name, base in branches(share):
        p = base / rel if rel else base
        if p.exists():
            return name, p
    return None


def emulated(share: dict, rel: str) -> Optional[str]:
    """The lost array disk holding share/rel, if it is only reachable through parity."""
    if share.get("path"):
        return None
    from bot.fileserver import array
    from bot.fileserver.array import _con
    cfg = array.config()
    lost = [d["name"] for d in cfg["disks"] if not Path(d["path"]).is_dir()]
    if not lost:
        return None
    con = _con()
    try:
        for d in lost:
            if con.execute("SELECT 1 FROM files WHERE disk=? AND path=?", (d, f"{share['name']}/{clean(rel)}")).fetchone():
                return d
    finally:
        con.close()
    return None


def listing(share: dict, rel: str = "") -> list[dict]:
    rel = clean(rel)
    seen: dict[str, dict] = {}
    for bname, base in branches(share):
        d = base / rel if rel else base
        if not d.is_dir():
            continue
        try:
            entries = list(os.scandir(d))
        except OSError:
            continue
        for e in entries:
            if e.name.startswith(".abp-"):
                continue
            try:
                st = e.stat()
            except OSError:
                continue
            is_dir = e.is_dir()
            if e.name in seen:
                if not is_dir and not seen[e.name]["dir"]:
                    seen[e.name].setdefault("also_on", []).append(bname)
                continue
            seen[e.name] = {"name": e.name, "dir": is_dir, "size": 0 if is_dir else st.st_size, "mtime": st.st_mtime,
                            "disk": bname}
    if not share.get("path"):     # files of a lost disk, served from parity
        from bot.fileserver import array
        from bot.fileserver.array import _con
        lost = [d["name"] for d in array.config()["disks"] if not Path(d["path"]).is_dir()]
        if lost:
            prefix = f"{share['name']}/{rel + '/' if rel else ''}"
            con = _con()
            for d in lost:
                for path, size, mtime in con.execute("SELECT path, size, mtime_ns FROM files WHERE disk=? AND path LIKE ?",
                                                     (d, prefix.replace("%", "\\%") + "%")):
                    rest = path[len(prefix):]
                    name = rest.split("/", 1)[0]
                    if name in seen:
                        continue
                    seen[name] = {"name": name, "dir": "/" in rest, "size": 0 if "/" in rest else size,
                                  "mtime": mtime / 1e9, "disk": d, "emulated": True}
            con.close()
    return sorted(seen.values(), key=lambda e: (not e["dir"], e["name"].lower()))


def _free(path: Path) -> int:
    p = path
    while not p.exists():
        p = p.parent
    return shutil.disk_usage(p).free


def _total(path: Path) -> int:
    p = path
    while not p.exists():
        p = p.parent
    return shutil.disk_usage(p).total


def place(share: dict, rel: str, size: int = 0) -> Path:
    """Where a new file share/rel goes (its folder is created). An existing file stays where it is."""
    rel = clean(rel)
    if not rel:
        raise FsError("name a file")
    hit = locate(share, rel)
    if hit:
        return hit[1]
    br = branches(share)
    if not br:
        raise FsError(f"share {share['name']} has no disks to write to")
    min_free = int(float(share.get("min_free_gb", 2)) * (1 << 30))
    if share.get("path"):
        target = br[0][1]
    else:
        cache = [b for b in br if b[0].startswith("pool:")]
        disks = [b for b in br if not b[0].startswith("pool:")]
        target = None
        if cache and share["cache"] in ("yes", "only", "prefer") and _free(cache[0][1]) - size > min_free:
            target = cache[0][1]
        elif share["cache"] == "only":
            raise FsError(f"the cache pool for {share['name']} is full")
        if target is None:
            target = _allocate(share, disks, rel, size, min_free)
    dest = target / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    return dest


def make_dir(share: dict, rel: str) -> Path:
    """Create the folder share/rel on the branch the share's rules choose for new files there."""
    rel = clean(rel)
    if not rel:
        raise FsError("name a folder")
    if locate(share, rel):
        raise FsError(f"{rel} exists")
    d = place(share, rel + "/_").parent
    d.mkdir(parents=True, exist_ok=True)
    return d


def _allocate(share: dict, disks: list[tuple[str, Path]], rel: str, size: int, min_free: int) -> Path:
    if not disks:
        raise FsError("no array disk is allowed for this share")
    present = [(n, b) for n, b in disks if b.parent.is_dir()]
    if not present:
        raise FsError("none of the share's disks can be reached")
    cands = present
    lvl = int(share.get("split_level") or 0)
    if lvl:
        parts = PurePosixPath(rel).parts[:-1][:lvl]
        if parts:
            prefix = "/".join(parts)
            holding = [(n, b) for n, b in present if (b / prefix).is_dir()]
            if holding:
                cands = holding
    room = [(n, b) for n, b in cands if _free(b.parent) - size > min_free]
    if not room:
        raise FsError(f"no disk of share {share['name']} has {size >> 20} MiB free above its minimum")
    alloc = share.get("allocation", "highwater")
    if alloc == "mostfree":
        return max(room, key=lambda nb: _free(nb[1].parent))[1]
    if alloc == "fillup":
        return room[0][1]
    mark = max(_total(b.parent) for _, b in room) / 2       # high-water: the first disk above the mark, halving it
    while mark >= 1 << 20:
        for n, b in room:
            if _free(b.parent) >= mark:
                return b
        mark /= 2
    return room[0][1]


def remove_path(share: dict, rel: str, recycle: Optional[bool] = None) -> int:
    """Delete share/rel on every branch (it may be on several); to the recycle bin unless turned off."""
    rel = clean(rel)
    if not rel:
        raise FsError("will not delete a whole share")
    use_bin = share.get("recycle_bin", True) if recycle is None else recycle
    n = 0
    stamp = time.strftime("%Y%m%d-%H%M%S")
    for _bname, base in branches(share):
        p = base / rel
        if not p.exists():
            continue
        if use_bin:
            dest = base.parent / RECYCLE / share["name"] / stamp / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            os.replace(p, dest)
        elif p.is_dir():
            shutil.rmtree(p)
        else:
            p.unlink()
        n += 1
    if not n:
        raise FsError(f"{rel} does not exist")
    return n


def move(share: dict, src: str, dst_share: dict, dst: str, overwrite: bool = False) -> None:
    src, dst = clean(src), clean(dst)
    hit = locate(share, src)
    if not hit:
        raise FsError(f"{src} does not exist")
    if locate(dst_share, dst):
        if not overwrite:
            raise FsError(f"{dst} exists")
        remove_path(dst_share, dst, recycle=False)
    _, sp = hit
    if dst_share is share or dst_share.get("name") == share.get("name"):
        dest = None
        # same share: keep it on the same branch (a rename, instant, never across disks)
        for _bname, base in branches(share):
            try:
                sp.relative_to(base)
            except ValueError:
                continue
            dest = base / dst
            break
        if dest is None:
            raise FsError("internal: the source is on no branch")
        dest.parent.mkdir(parents=True, exist_ok=True)
        os.replace(sp, dest)
        return
    size = sp.stat().st_size if sp.is_file() else 0
    dest = place(dst_share, dst, size)
    shutil.move(str(sp), str(dest))


def recycle_purge(days: Optional[int] = None) -> int:
    """Empty recycle-bin entries older than each share's recycle_days (or `days`)."""
    n = 0
    now = time.time()
    seen: set[Path] = set()
    from bot.fileserver import array
    bins = [Path(d["path"]) / RECYCLE for d in array.config()["disks"]] + [Path(p["path"]) / RECYCLE for p in pools().values()]
    bins += [Path(s["path"]).parent / RECYCLE for s in shares().values() if s.get("path")]
    for b in bins:
        if b in seen or not b.is_dir():
            continue
        seen.add(b)
        for share_dir in b.iterdir():
            keep = ((days if days is not None else shares().get(share_dir.name, {}).get("recycle_days", 30)) * 86400)
            for stamp in share_dir.iterdir():
                if now - stamp.stat().st_mtime >= keep:
                    shutil.rmtree(stamp, ignore_errors=True)
                    n += 1
    return n


# ---- links -------------------------------------------------------------------------------------------------------- #

def create_link(share: str, path: str, *, password: str = "", expires_days: float = 7, max_downloads: int = 0,
                allow_upload: bool = False, created_by: str = "") -> dict:
    s = get(share)
    rel = clean(path)
    if rel and not locate(s, rel):
        raise FsError(f"{rel} does not exist")
    token = secrets.token_urlsafe(18)
    link = {"share": s["name"], "path": rel, "created": int(time.time()), "by": created_by,
            "expires": int(time.time() + expires_days * 86400) if expires_days else 0, "max_downloads": int(max_downloads),
            "downloads": 0, "allow_upload": bool(allow_upload), "password_hash": _pw_hash(password) if password else ""}
    update("links", {}, lambda ls: ls.__setitem__(token, link))
    return {"token": token, **{k: v for k, v in link.items() if k != "password_hash"}, "has_password": bool(password)}


def links() -> list[dict]:
    return [{"token": t, **{k: v for k, v in l.items() if k != "password_hash"}, "has_password": bool(l.get("password_hash"))}
            for t, l in sorted(load("links", {}).items(), key=lambda kv: -kv[1]["created"])]


def open_link(token: str, password: str = "") -> dict:
    l = load("links", {}).get(token)
    if not l:
        raise FsError("this link does not exist (or was removed)")
    if l["expires"] and time.time() > l["expires"]:
        raise FsError("this link has expired")
    if l["max_downloads"] and l["downloads"] >= l["max_downloads"]:
        raise FsError("this link has been used up")
    if l["password_hash"] and not _pw_ok(password, l["password_hash"]):
        raise PermissionError("password")
    return l


def count_download(token: str) -> None:
    update("links", {}, lambda ls: ls[token].__setitem__("downloads", ls[token]["downloads"] + 1) if token in ls else None)


def remove_link(token: str) -> bool:
    return update("links", {}, lambda ls: ls.pop(token, None) is not None)


def walk(share: dict, rel: str = "") -> Iterator[tuple[str, Path]]:
    """Every file under share/rel: (relative path in the share, real path)."""
    rel = clean(rel)
    seen = set()
    for _bname, base in branches(share):
        top = base / rel if rel else base
        if not top.exists():
            continue
        if top.is_file():
            if rel not in seen:
                seen.add(rel)
                yield rel, top
            continue
        for dirpath, dirnames, filenames in os.walk(top):
            dirnames[:] = [d for d in dirnames if not d.startswith(".abp-")]
            for f in filenames:
                full = Path(dirpath) / f
                r = full.relative_to(base).as_posix()
                if r not in seen:
                    seen.add(r)
                    yield r, full
