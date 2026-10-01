"""The mover: files between a share's cache pool and the array, as the share says.

    cache yes     cache -> array (new files land on the fast cache, then move to protected array disks)
    cache prefer  array -> cache while the pool has room above its minimum
    cache no/only nothing

Files written in the last `settle_minutes` are left alone (they may still be open), and a file that cannot be moved
(in use) is tried again next time. With tiering on (`hot_days`), files read through the file server within that many
days stay on the cache (yes) or are pulled to it first (prefer): the access log is what the file server records for
every read (access.db), the "model" a simple, explainable recency/frequency score.
"""
from __future__ import annotations

import os
import shutil
import time
from pathlib import Path
from typing import Callable

from bot.fileserver import shares
from bot.fileserver.store import FsError, db, load

Log = Callable[[str], None]


def _access():
    con = db("access")
    con.execute("CREATE TABLE IF NOT EXISTS hits(share TEXT, path TEXT, n INT, last INT, PRIMARY KEY(share, path))")
    return con


def record_access(share: str, path: str) -> None:
    con = _access()
    with con:
        con.execute("INSERT INTO hits(share, path, n, last) VALUES(?,?,1,?) ON CONFLICT(share, path) DO UPDATE "
                    "SET n = n + 1, last = excluded.last", (share, path, int(time.time())))
    con.close()


def heat(share: str, hot_days: float) -> dict[str, float]:
    """path -> score (reads, decayed by age: a read today counts 1, one hot_days ago 1/2)."""
    con = _access()
    now = time.time()
    out = {}
    for path, n, last in con.execute("SELECT path, n, last FROM hits WHERE share=?", (share,)):
        age = (now - last) / 86400
        if age <= hot_days:
            out[path] = n * 0.5 ** (age / max(hot_days, 0.01))
    con.close()
    return out


def settings() -> dict:
    return {"settle_minutes": 10, "hot_days": 0, **load("mover", {})}


def _movable(p: Path, settle_s: float) -> bool:
    try:
        return time.time() - p.stat().st_mtime > settle_s
    except OSError:
        return False


def _move_file(src: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".abp-moving")
    shutil.copy2(src, tmp)
    if tmp.stat().st_size != src.stat().st_size:
        tmp.unlink()
        raise OSError("size changed while moving")
    os.replace(tmp, dest)
    try:
        src.unlink()
    except OSError:
        dest.unlink(missing_ok=True)       # still open on the cache: keep the original, try again later
        raise


def run(log: Log = lambda m: None, only_share: str = "") -> dict:
    st = settings()
    settle = st["settle_minutes"] * 60
    out = {"moved": 0, "bytes": 0, "skipped": 0, "errors": []}
    for name, s in shares.shares().items():
        if only_share and name != only_share:
            continue
        s = {**s, "name": name}
        if s.get("path") or s["cache"] in ("no", "only"):
            continue
        br = shares.branches(s)
        cache = next(((n, b) for n, b in br if n.startswith("pool:")), None)
        if not cache:
            continue
        hot = heat(name, st["hot_days"]) if st["hot_days"] else {}
        if s["cache"] == "yes" and cache[1].is_dir():
            disks = [b for b in br if not b[0].startswith("pool:")]
            for dirpath, dirnames, files in os.walk(cache[1]):
                dirnames[:] = [d for d in dirnames if not d.startswith(".abp-")]
                for f in files:
                    src = Path(dirpath) / f
                    rel = src.relative_to(cache[1]).as_posix()
                    if f.endswith((".abp-moving", ".abp-upload")) or not _movable(src, settle) or rel in hot:
                        out["skipped"] += 1
                        continue
                    try:
                        size = src.stat().st_size
                        target = shares._allocate(s, disks, rel, size, int(float(s["min_free_gb"]) * (1 << 30)))
                        _move_file(src, target / rel)
                        out["moved"] += 1
                        out["bytes"] += size
                    except (OSError, FsError) as e:
                        out["errors"].append(f"{name}/{rel}: {e}")
            _prune_empty(cache[1])
        elif s["cache"] == "prefer":
            min_free = int(float(s["min_free_gb"]) * (1 << 30))
            items = []
            for bname, base in br:
                if bname.startswith("pool:") or not base.is_dir():
                    continue
                for dirpath, dirnames, files in os.walk(base):
                    dirnames[:] = [d for d in dirnames if not d.startswith(".abp-")]
                    for f in files:
                        p = Path(dirpath) / f
                        items.append((hot.get(p.relative_to(base).as_posix(), 0.0), p, base))
            for _score, src, base in sorted(items, key=lambda t: -t[0]):     # the hottest first, while there is room
                rel = src.relative_to(base).as_posix()
                try:
                    size = src.stat().st_size
                    if shutil.disk_usage(cache[1].parent).free - size < min_free:
                        break
                    if not _movable(src, settle):
                        continue
                    _move_file(src, cache[1] / rel)
                    out["moved"] += 1
                    out["bytes"] += size
                except OSError as e:
                    out["errors"].append(f"{name}/{rel}: {e}")
    log(f"mover: {out['moved']} file(s), {out['bytes'] >> 20} MiB moved; {out['skipped']} left for later; {len(out['errors'])} error(s)")
    return out


def _prune_empty(top: Path) -> None:
    for dirpath, _dirnames, _files in os.walk(top, topdown=False):
        if dirpath != str(top) and not os.listdir(dirpath):
            try:
                os.rmdir(dirpath)
            except OSError:
                pass
