"""The parity array: data disks (each a folder on its own drive, any size, any filesystem) protected by one or two
parity disks. The method is snapshot parity, like SnapRAID and in effect like Unraid's array:

  - every file on a data disk is cut into blocks (256 KiB by default) placed at *positions* of that disk; a disk's
    positions are its own (disk 1's position 7 and disk 2's position 7 are unrelated files)
  - parity position i covers position i of every data disk: P = XOR of the blocks, Q = sum g^d * block (GF(2^8)),
    written to `abp-parity.p` / `abp-parity.q` on the parity disks at offset i * block size
  - every block's BLAKE2b hash is kept, so silent corruption (bitrot) is found by `scrub`, and every rebuilt block is
    checked against its hash before it is written

  sync     scan the disks, place new and changed files' blocks, free deleted ones, recompute parity where anything changed
  scrub    read a share of the positions (round robin), check every block's hash and the parity
  fix      rebuild files: a lost or replaced disk, missing files, corrupted blocks; one lost disk with P or Q, any two
           with P and Q
  emulate  read a lost disk's files on the fly from parity (what the file server serves while a disk is out)

Files changed after the last sync are not protected until the next one (the same as SnapRAID); status() counts them.
"""
from __future__ import annotations

import fnmatch
import hashlib
import os
import time
from pathlib import Path
from typing import Callable, Iterator, Optional

import numpy as np

from bot.fileserver import gf
from bot.fileserver.store import FsError, db, load, root, save

Log = Callable[[str], None]
PARITY_FILES = {"P": "abp-parity.p", "Q": "abp-parity.q"}
DEFAULT_EXCLUDE = ["*.tmp", "*.part", "~$*", "Thumbs.db", ".DS_Store", "desktop.ini", "$RECYCLE.BIN/*",
                   "System Volume Information/*", ".abp-*", ".Trash-*/*", "*.abp-upload", "*.abp-moving",
                   "*.abp-rebuild"]
BATCH = 32          # stripes handled at once (memory: BATCH * block size * disks)


# ---- configuration ---------------------------------------------------------------------------------------------- #

def config() -> dict:
    return {"block_kib": 256, "disks": [], "parity": [], "exclude": list(DEFAULT_EXCLUDE), "next_index": 0,
            **load("array", {})}


def _device(path: str) -> Optional[int]:
    try:
        return os.stat(path).st_dev
    except OSError:
        return None


def configure(disks: list[dict], parity: list[dict], block_kib: int = 256, exclude: Optional[list[str]] = None,
              allow_same_drive: bool = False) -> dict:
    """Set the array up (or change it). disks: [{"name", "path"}]; parity: [{"path"}] (one: P, two: P and Q).
    A disk keeps its index (its place in Q) for as long as it is in the array."""
    cur = config()
    if cur["disks"] and block_kib != cur["block_kib"] and _con_count() > 0:
        raise FsError("the block size cannot change once the array has been synced")
    if not (1 <= len(parity) <= 2):
        raise FsError("one parity disk (single parity) or two (dual parity)")
    if not disks:
        raise FsError("an array needs at least one data disk")
    if block_kib not in (64, 128, 256, 512, 1024):
        raise FsError("the block size is 64, 128, 256, 512 or 1024 KiB")
    names, paths = set(), set()
    old = {d["name"]: d for d in cur["disks"]}
    nxt = cur["next_index"]
    out_disks = []
    for d in disks:
        name = str(d.get("name") or "").strip()
        path = str(Path(d.get("path") or "").expanduser())
        if not name or not name.replace("-", "").replace("_", "").isalnum() or name in names:
            raise FsError(f"disk names are unique letters/digits (got {name!r})")
        if not Path(path).is_dir():
            raise FsError(f"{path} is not a folder")
        names.add(name)
        paths.add(os.path.normcase(os.path.abspath(path)))
        if name in old:
            idx = old[name]["index"]
        else:
            idx, nxt = nxt, nxt + 1
        out_disks.append({"name": name, "path": path, "index": idx})
    if nxt > 250:
        raise FsError("too many disks have been added over time (indexes run out at 250)")
    out_par = []
    for i, p in enumerate(parity):
        path = str(Path(p.get("path") or "").expanduser())
        Path(path).mkdir(parents=True, exist_ok=True)
        ap = os.path.normcase(os.path.abspath(path))
        if any(ap == d or ap.startswith(d + os.sep) for d in paths):
            raise FsError(f"parity {path} is inside a data disk")
        out_par.append({"name": "parity" if i == 0 else "parity2", "path": path, "kind": "PQ"[i]})
    if not allow_same_drive:
        devs = {_device(d["path"]) for d in out_disks}
        for p in out_par:
            if _device(p["path"]) in devs:
                raise FsError(f"parity {p['path']} is on the same drive as a data disk: losing that drive would lose both")
    removed = [n for n in old if n not in names]
    cfg = {**cur, "disks": out_disks, "parity": out_par, "block_kib": block_kib, "next_index": nxt}
    if exclude is not None:
        cfg["exclude"] = exclude
    save("array", cfg)
    if removed:
        con = _con()
        with con:
            for n in removed:     # their blocks leave the parity: every position they held is recomputed
                con.execute("INSERT OR IGNORE INTO dirty(pos) SELECT pos FROM blocks WHERE disk=?", (n,))
                con.execute("DELETE FROM blocks WHERE disk=?", (n,))
                con.execute("DELETE FROM files WHERE disk=?", (n,))
        con.close()
    return cfg


def _con():
    con = db("array")
    con.executescript("""
        CREATE TABLE IF NOT EXISTS files(disk TEXT, path TEXT, size INT, mtime_ns INT, runs TEXT, synced INT,
                                         PRIMARY KEY(disk, path));
        CREATE TABLE IF NOT EXISTS blocks(disk TEXT, pos INT, path TEXT, idx INT, hash BLOB, PRIMARY KEY(disk, pos));
        CREATE INDEX IF NOT EXISTS blocks_file ON blocks(disk, path, idx);
        CREATE TABLE IF NOT EXISTS dirty(pos INT PRIMARY KEY);
        CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE IF NOT EXISTS errors(disk TEXT, path TEXT, idx INT, pos INT, kind TEXT, at INT,
                                          PRIMARY KEY(disk, pos, kind));
    """)
    return con


def _con_count() -> int:
    if not (root() / "array.db").exists():
        return 0
    con = _con()
    n = con.execute("SELECT COUNT(*) FROM files").fetchone()[0]
    con.close()
    return n


def _meta(con, key: str, value=None):
    if value is None:
        r = con.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return r[0] if r else None
    con.execute("INSERT OR REPLACE INTO meta(key, value) VALUES(?, ?)", (key, str(value)))


def _excluded(rel: str, patterns: list[str]) -> bool:
    name = rel.rsplit("/", 1)[-1]
    return any(fnmatch.fnmatch(rel, p) or fnmatch.fnmatch(name, p) for p in patterns)


def scan(disk: dict, exclude: list[str]) -> dict[str, tuple[int, int]]:
    """{relative posix path: (size, mtime_ns)} of every file on the disk."""
    base = Path(disk["path"])
    out: dict[str, tuple[int, int]] = {}
    for dirpath, dirnames, filenames in os.walk(base):
        rel_dir = Path(dirpath).relative_to(base).as_posix()
        rel_dir = "" if rel_dir == "." else rel_dir + "/"
        dirnames[:] = [d for d in dirnames if not _excluded(rel_dir + d + "/x", exclude) and not _excluded(rel_dir + d, exclude)]
        for f in filenames:
            rel = rel_dir + f
            if _excluded(rel, exclude):
                continue
            try:
                st = os.stat(os.path.join(dirpath, f))
            except OSError:
                continue
            out[rel] = (st.st_size, st.st_mtime_ns)
    return out


# ---- block placement -------------------------------------------------------------------------------------------- #

class Allocator:
    """Free positions of one disk: the gaps between used positions (first fit), then the end."""

    def __init__(self, used: list[int]):
        self.gaps: list[list[int]] = []
        prev = -1
        for p in used:
            if p > prev + 1:
                self.gaps.append([prev + 1, p - prev - 1])
            prev = p
        self.end = prev + 1

    def take(self, n: int) -> list[list[int]]:
        runs = []
        while n and self.gaps:
            g = self.gaps[0]
            k = min(n, g[1])
            runs.append([g[0], k])
            g[0] += k
            g[1] -= k
            n -= k
            if g[1] == 0:
                self.gaps.pop(0)
        if n:
            runs.append([self.end, n])
            self.end += n
        return runs

    def give(self, runs: list[list[int]]) -> None:
        for start, n in runs:
            self.gaps.append([start, n])
        self.gaps.sort()
        merged: list[list[int]] = []
        for g in self.gaps:
            if merged and merged[-1][0] + merged[-1][1] == g[0]:
                merged[-1][1] += g[1]
            else:
                merged.append(list(g))
        self.gaps = merged


def positions(runs: list[list[int]]) -> list[int]:
    return [s + i for s, n in runs for i in range(n)]


def _hash(block: bytes) -> bytes:
    return hashlib.blake2b(block, digest_size=16).digest()


def _blocks_of(path: Path, size: int, bs: int) -> Iterator[bytes]:
    with open(path, "rb") as f:
        remaining = size
        while remaining > 0:
            b = f.read(min(bs, remaining))
            if not b:
                break
            remaining -= len(b)
            yield b.ljust(bs, b"\0")


# ---- sync ------------------------------------------------------------------------------------------------------- #

def sync(log: Log = lambda m: None, dry_run: bool = False) -> dict:
    cfg = config()
    if not cfg["disks"] or not cfg["parity"]:
        raise FsError("the array is not set up (no data or parity disks)")
    missing = [d["name"] for d in cfg["disks"] if not Path(d["path"]).is_dir()]
    if missing:
        raise FsError(f"disk(s) {', '.join(missing)} cannot be found: fix or rebuild them before syncing "
                      "(a sync now would drop their files from parity)")
    bs = cfg["block_kib"] * 1024
    con = _con()
    t0 = time.time()
    stats = {"added": 0, "changed": 0, "removed": 0, "skipped_busy": 0, "bytes_hashed": 0, "stripes": 0}
    try:
        for p in cfg["parity"]:
            if not (Path(p["path"]) / PARITY_FILES[p["kind"]]).exists() and con.execute("SELECT 1 FROM blocks LIMIT 1").fetchone():
                log(f"{p['name']} has no parity file: it is rebuilt in full")
                con.execute("INSERT OR IGNORE INTO dirty(pos) SELECT DISTINCT pos FROM blocks")
        for d in cfg["disks"]:
            now = scan(d, cfg["exclude"])
            known = {r[0]: (r[1], r[2], r[3]) for r in con.execute("SELECT path, size, mtime_ns, runs FROM files WHERE disk=?", (d["name"],))}
            gone = [p for p in known if p not in now]
            new = [p for p in now if p not in known]
            changed = [p for p in now if p in known and (known[p][0], known[p][1]) != now[p]]
            log(f"{d['name']}: {len(new)} new, {len(changed)} changed, {len(gone)} removed (of {len(now)} files)")
            if dry_run:
                stats["added"] += len(new)
                stats["changed"] += len(changed)
                stats["removed"] += len(gone)
                continue
            alloc = Allocator([r[0] for r in con.execute("SELECT pos FROM blocks WHERE disk=? ORDER BY pos", (d["name"],))])
            import json as _j
            with con:
                for p in gone:
                    runs = _j.loads(known[p][2])
                    _free(con, d["name"], runs)
                    alloc.give(runs)
                    con.execute("DELETE FROM files WHERE disk=? AND path=?", (d["name"], p))
                    stats["removed"] += 1
            for p in new + changed:
                full = Path(d["path"]) / p
                size, mtime = now[p]
                try:
                    hashes = [_hash(b) for b in _blocks_of(full, size, bs)]
                    st = os.stat(full)
                except OSError as e:
                    log(f"{d['name']}/{p}: cannot read ({e}); skipped")
                    stats["skipped_busy"] += 1
                    continue
                if (st.st_size, st.st_mtime_ns) != (size, mtime):
                    stats["skipped_busy"] += 1        # changing while read: the next sync takes it
                    continue
                with con:
                    if p in known:
                        old_runs = _j.loads(known[p][2])
                        _free(con, d["name"], old_runs)
                        alloc.give(old_runs)
                    runs = alloc.take(len(hashes))
                    pos = positions(runs)
                    con.executemany("INSERT OR REPLACE INTO blocks(disk, pos, path, idx, hash) VALUES(?,?,?,?,?)",
                                    [(d["name"], ps, p, i, h) for i, (ps, h) in enumerate(zip(pos, hashes))])
                    con.executemany("INSERT OR IGNORE INTO dirty(pos) VALUES(?)", [(ps,) for ps in pos])
                    con.execute("INSERT OR REPLACE INTO files(disk, path, size, mtime_ns, runs, synced) VALUES(?,?,?,?,?,?)",
                                (d["name"], p, size, mtime, _j.dumps(runs), int(time.time())))
                stats["changed" if p in known else "added"] += 1
                stats["bytes_hashed"] += size
        if not dry_run:
            stats["stripes"] = _recompute(con, cfg, log)
            _meta(con, "last_sync", int(time.time()))
            con.commit()
    finally:
        con.close()
    stats["seconds"] = round(time.time() - t0, 2)
    log(f"sync done: {stats}")
    return stats


def _free(con, disk: str, runs: list[list[int]]) -> None:
    for start, n in runs:
        con.execute("INSERT OR IGNORE INTO dirty(pos) SELECT pos FROM blocks WHERE disk=? AND pos BETWEEN ? AND ?", (disk, start, start + n - 1))
        con.execute("DELETE FROM blocks WHERE disk=? AND pos BETWEEN ? AND ?", (disk, start, start + n - 1))


class _Reader:
    """Reads block `idx` of a file on a disk, keeping a few files open."""

    def __init__(self, cfg: dict):
        self.bs = cfg["block_kib"] * 1024
        self.base = {d["name"]: Path(d["path"]) for d in cfg["disks"]}
        self.open: dict[tuple[str, str], object] = {}

    def block(self, disk: str, path: str, idx: int) -> Optional[bytes]:
        key = (disk, path)
        f = self.open.get(key)
        if f is None:
            try:
                f = open(self.base[disk] / path, "rb")
            except OSError:
                return None
            if len(self.open) > 64:
                for k in list(self.open)[:32]:
                    self.open.pop(k).close()
            self.open[key] = f
        f.seek(idx * self.bs)
        return f.read(self.bs).ljust(self.bs, b"\0")

    def close(self):
        for f in self.open.values():
            f.close()
        self.open.clear()


def _stripes(con, cfg: dict, lo: int, hi: int) -> dict[str, dict[int, tuple[str, int, bytes]]]:
    """disk -> {pos: (path, idx, hash)} for positions lo..hi."""
    out: dict[str, dict] = {d["name"]: {} for d in cfg["disks"]}
    for disk, pos, path, idx, h in con.execute("SELECT disk, pos, path, idx, hash FROM blocks WHERE pos BETWEEN ? AND ?", (lo, hi)):
        if disk in out:
            out[disk][pos] = (path, idx, h)
    return out


def _parity_handles(cfg: dict, mode: str) -> dict[str, object]:
    hs = {}
    for p in cfg["parity"]:
        f = Path(p["path"]) / PARITY_FILES[p["kind"]]
        if mode == "w" and not f.exists():
            f.touch()
        if f.exists():
            hs[p["kind"]] = open(f, "r+b" if mode == "w" else "rb")
    return hs


def _runs_of(sorted_pos: list[int], cap: int = BATCH) -> Iterator[tuple[int, int]]:
    i = 0
    while i < len(sorted_pos):
        start = sorted_pos[i]
        j = i
        while j + 1 < len(sorted_pos) and sorted_pos[j + 1] == sorted_pos[j] + 1 and j + 1 - i < cap:
            j += 1
        yield start, sorted_pos[j]
        i = j + 1


def _recompute(con, cfg: dict, log: Log) -> int:
    dirty = [r[0] for r in con.execute("SELECT pos FROM dirty ORDER BY pos")]
    if not dirty:
        _truncate(con, cfg)
        return 0
    bs = cfg["block_kib"] * 1024
    rd = _Reader(cfg)
    hs = _parity_handles(cfg, "w")
    want_q = "Q" in hs
    done = 0
    try:
        for lo, hi in _runs_of(dirty):
            n = hi - lo + 1
            st = _stripes(con, cfg, lo, hi)
            blocks, idxs = [], []
            for d in cfg["disks"]:
                arr = np.zeros((n, bs), dtype=np.uint8)
                any_ = False
                for pos, (path, idx, _h) in st[d["name"]].items():
                    b = rd.block(d["name"], path, idx)
                    if b is not None:
                        arr[pos - lo] = np.frombuffer(b, dtype=np.uint8)
                        any_ = True
                if any_:
                    blocks.append(arr)
                    idxs.append(d["index"])
            if blocks:
                p, q = gf.parity(blocks, idxs, want_q)
            else:
                p = np.zeros((n, bs), dtype=np.uint8)
                q = p.copy() if want_q else None
            for kind, h in hs.items():
                h.seek(lo * bs)
                h.write((p if kind == "P" else q).tobytes())
            con.execute("DELETE FROM dirty WHERE pos BETWEEN ? AND ?", (lo, hi))
            done += n
            if done % (BATCH * 64) < n:
                con.commit()
                log(f"parity: {done}/{len(dirty)} stripes")
    finally:
        rd.close()
        for h in hs.values():
            h.close()
    con.commit()
    _truncate(con, cfg)
    return done


def _truncate(con, cfg: dict) -> None:
    top = con.execute("SELECT MAX(pos) FROM blocks").fetchone()[0]
    size = 0 if top is None else (top + 1) * cfg["block_kib"] * 1024
    for p in cfg["parity"]:
        f = Path(p["path"]) / PARITY_FILES[p["kind"]]
        if f.exists() and f.stat().st_size > size:
            with open(f, "r+b") as h:
                h.truncate(size)


# ---- scrub ------------------------------------------------------------------------------------------------------ #

def scrub(percent: float = 10.0, log: Log = lambda m: None, repair_parity: bool = True) -> dict:
    """Check `percent` of the positions (continuing where the last scrub stopped): every block against its hash, and
    the parity against the blocks. Corrupted data blocks are recorded (fix() repairs them); wrong parity is rewritten."""
    cfg = config()
    bs = cfg["block_kib"] * 1024
    con = _con()
    top = con.execute("SELECT MAX(pos) FROM blocks").fetchone()[0]
    if top is None:
        con.close()
        return {"checked": 0, "data_errors": 0, "parity_errors": 0}
    total = top + 1
    count = max(1, int(total * min(100.0, max(0.1, percent)) / 100))
    start = int(_meta(con, "scrub_cursor") or 0) % total
    pos_list = [(start + i) % total for i in range(count)]
    rd = _Reader(cfg)
    hs = _parity_handles(cfg, "w" if repair_parity else "r")
    out = {"checked": 0, "data_errors": 0, "parity_errors": 0, "parity_repaired": 0, "unsynced": 0}
    synced_mtime = {(r[0], r[1]): r[2] for r in con.execute("SELECT disk, path, mtime_ns FROM files")}
    try:
        for lo, hi in _runs_of(sorted(pos_list)):
            n = hi - lo + 1
            st = _stripes(con, cfg, lo, hi)
            blocks, idxs = [], []
            bad_pos: set[int] = set()
            for d in cfg["disks"]:
                arr = np.zeros((n, bs), dtype=np.uint8)
                for pos, (path, idx, h) in st[d["name"]].items():
                    b = rd.block(d["name"], path, idx)
                    if b is None:
                        bad_pos.add(pos)
                        continue
                    if _hash(b) != h:
                        bad_pos.add(pos)
                        try:
                            now_m = os.stat(Path(d["path"]) / path).st_mtime_ns
                        except OSError:
                            now_m = None
                        if now_m != synced_mtime.get((d["name"], path)):
                            out["unsynced"] += 1
                            continue
                        out["data_errors"] += 1
                        con.execute("INSERT OR REPLACE INTO errors VALUES(?,?,?,?,?,?)", (d["name"], path, idx, pos, "data", int(time.time())))
                        log(f"corrupted block: {d['name']}/{path} block {idx}")
                        continue
                    arr[pos - lo] = np.frombuffer(b, dtype=np.uint8)
                blocks.append(arr)
                idxs.append(d["index"])
            p, q = gf.parity(blocks, idxs, "Q" in hs)
            for kind, h in hs.items():
                h.seek(lo * bs)
                stored = np.frombuffer(h.read(n * bs).ljust(n * bs, b"\0"), dtype=np.uint8).reshape(n, bs)
                calc = p if kind == "P" else q
                for k in range(n):
                    if lo + k in bad_pos:
                        continue
                    if not np.array_equal(stored[k], calc[k]):
                        out["parity_errors"] += 1
                        if repair_parity:
                            h.seek((lo + k) * bs)
                            h.write(calc[k].tobytes())
                            out["parity_repaired"] += 1
            out["checked"] += n
        _meta(con, "scrub_cursor", (start + count) % total)
        _meta(con, "last_scrub", int(time.time()))
        con.commit()
    finally:
        rd.close()
        for h in hs.values():
            h.close()
        con.close()
    log(f"scrub: {out}")
    return out


# ---- rebuild ---------------------------------------------------------------------------------------------------- #

class Rebuilder:
    """Reconstructs blocks of lost or damaged files from the other disks and parity, verifying each against its hash."""

    def __init__(self, cfg: dict, lost_disks: set[str]):
        self.cfg = cfg
        self.bs = cfg["block_kib"] * 1024
        self.lost = set(lost_disks)
        self.rd = _Reader(cfg)
        self.hs = _parity_handles(cfg, "r")
        self.con = _con()
        self.index = {d["name"]: d["index"] for d in cfg["disks"]}

    def close(self):
        self.rd.close()
        for h in self.hs.values():
            h.close()
        self.con.close()

    def _parity(self, kind: str, pos: int) -> Optional[np.ndarray]:
        h = self.hs.get(kind)
        if not h:
            return None
        h.seek(pos * self.bs)
        b = h.read(self.bs)
        return np.frombuffer(b.ljust(self.bs, b"\0"), dtype=np.uint8).copy() if b else None

    def block(self, disk: str, pos: int) -> bytes:
        """The block at `pos` of `disk`, rebuilt."""
        st = _stripes(self.con, self.cfg, pos, pos)
        want = st[disk].get(pos)
        if not want:
            raise FsError(f"{disk} has no block at position {pos}")
        good, good_idx, lost = [], [], [disk]
        for d in self.cfg["disks"]:
            n = d["name"]
            if n == disk or pos not in st[n]:
                continue
            path, idx, h = st[n][pos]
            b = None if n in self.lost else self.rd.block(n, path, idx)
            if b is None or _hash(b) != h:
                lost.append(n)
                continue
            good.append(np.frombuffer(b, dtype=np.uint8))
            good_idx.append(self.index[n])
        p, q = self._parity("P", pos), self._parity("Q", pos)
        if len(lost) == 1 and p is not None:
            out = gf.recover_one_p(p, good)
        elif len(lost) == 1 and q is not None:
            out = gf.recover_one_q(q, good, good_idx, self.index[disk])
        elif len(lost) == 2 and p is not None and q is not None:
            x, y = self.index[lost[0]], self.index[lost[1]]
            dx, dy = gf.recover_two(p, q, good, good_idx, x, y)
            out = dx
        else:
            raise FsError(f"position {pos}: {len(lost)} block(s) lost ({', '.join(lost)}) — more than the parity can rebuild")
        data = out.tobytes()
        if _hash(data) != want[2]:
            raise FsError(f"{disk}/{want[0]} block {want[1]}: the rebuilt block does not match its hash "
                          "(another disk changed since the last sync)")
        return data

    def file(self, disk: str, path: str) -> Iterator[bytes]:
        row = self.con.execute("SELECT size, runs FROM files WHERE disk=? AND path=?", (disk, path)).fetchone()
        if not row:
            raise FsError(f"{disk}/{path} is not in the array's records")
        import json as _j
        size = row[0]
        for pos in positions(_j.loads(row[1])):
            data = self.block(disk, pos)
            take = min(self.bs, size)
            size -= take
            yield data[:take]


def fix(disk: Optional[str] = None, files: Optional[list[str]] = None, target: Optional[str] = None,
        log: Log = lambda m: None) -> dict:
    """Rebuild files. With `disk` and no `files`: every file of that disk that is missing or damaged (a lost disk:
    all of them; give `target`, the replacement drive's folder, and the disk is moved there). With neither: every
    block the last scrubs found corrupted, and every file missing from any disk."""
    cfg = config()
    names = {d["name"]: d for d in cfg["disks"]}
    if disk and disk not in names:
        raise FsError(f"no data disk {disk}")
    con = _con()
    todo: list[tuple[str, str]] = []
    if disk and files:
        todo = [(disk, f) for f in files]
    else:
        scope = [disk] if disk else list(names)
        for n in scope:
            base = Path(target) if (target and n == disk) else Path(names[n]["path"])
            for (p,) in con.execute("SELECT path FROM files WHERE disk=?", (n,)):
                if not (base / p).exists():
                    todo.append((n, p))
        for n, p in con.execute("SELECT DISTINCT disk, path FROM errors WHERE kind='data'"):
            if (not disk or n == disk) and (n, p) not in todo:
                todo.append((n, p))
    con.close()
    lost = {n for n, d in names.items() if not Path(d["path"]).is_dir()}
    if disk and target:
        lost.add(disk)
    rb = Rebuilder(cfg, lost)
    out = {"rebuilt": 0, "failed": [], "bytes": 0}
    try:
        for n, p in todo:
            base = Path(target) if (target and n == disk) else Path(names[n]["path"])
            dest = base / p
            dest.parent.mkdir(parents=True, exist_ok=True)
            tmp = dest.with_name(dest.name + ".abp-rebuild")
            try:
                with open(tmp, "wb") as f:
                    for chunk in rb.file(n, p):
                        f.write(chunk)
                        out["bytes"] += len(chunk)
                os.replace(tmp, dest)
                row = rb.con.execute("SELECT mtime_ns FROM files WHERE disk=? AND path=?", (n, p)).fetchone()
                os.utime(dest, ns=(row[0], row[0]))      # as it was: the next sync sees it unchanged
                rb.con.execute("DELETE FROM errors WHERE disk=? AND path=?", (n, p))
                rb.con.commit()
                out["rebuilt"] += 1
                log(f"rebuilt {n}/{p}")
            except (FsError, OSError) as e:
                tmp.unlink(missing_ok=True)
                out["failed"].append({"disk": n, "path": p, "error": str(e)})
                log(f"could not rebuild {n}/{p}: {e}")
    finally:
        rb.close()
    if disk and target and not out["failed"]:
        cfg["disks"] = [{**d, "path": target} if d["name"] == disk else d for d in cfg["disks"]]
        save("array", cfg)
        log(f"{disk} now lives at {target}")
    return out


def emulate(disk: str, path: str) -> Iterator[bytes]:
    """A lost disk's file, read from parity and the other disks (verified block by block)."""
    cfg = config()
    rb = Rebuilder(cfg, {disk})
    try:
        yield from rb.file(disk, path)
    finally:
        rb.close()


# ---- status ----------------------------------------------------------------------------------------------------- #

def status(scan_changes: bool = False) -> dict:
    import shutil as _sh
    cfg = config()
    con = _con()
    out = {"configured": bool(cfg["disks"] and cfg["parity"]), "block_kib": cfg["block_kib"],
           "dual_parity": len(cfg["parity"]) == 2, "disks": [], "parity": [],
           "last_sync": int(_meta(con, "last_sync") or 0) or None, "last_scrub": int(_meta(con, "last_scrub") or 0) or None,
           "pending_parity": con.execute("SELECT COUNT(*) FROM dirty").fetchone()[0],
           "errors": [dict(zip(("disk", "path", "block", "pos", "kind", "at"), r)) for r in con.execute("SELECT * FROM errors LIMIT 200")]}
    for d in cfg["disks"]:
        n_files, n_bytes = con.execute("SELECT COUNT(*), COALESCE(SUM(size),0) FROM files WHERE disk=?", (d["name"],)).fetchone()
        ok = Path(d["path"]).is_dir()
        info = {"name": d["name"], "path": d["path"], "index": d["index"], "present": ok, "files": n_files,
                "protected_bytes": n_bytes, "state": "ok" if ok else "missing — emulated from parity"}
        if ok:
            u = _sh.disk_usage(d["path"])
            info.update(size=u.total, free=u.free)
            if scan_changes:
                now = scan(d, cfg["exclude"])
                known = {r[0]: (r[1], r[2]) for r in con.execute("SELECT path, size, mtime_ns FROM files WHERE disk=?", (d["name"],))}
                info["unsynced"] = sum(1 for p, v in now.items() if known.get(p) != v) + sum(1 for p in known if p not in now)
        out["disks"].append(info)
    top = con.execute("SELECT MAX(pos) FROM blocks").fetchone()[0]
    need = 0 if top is None else (top + 1) * cfg["block_kib"] * 1024
    for p in cfg["parity"]:
        f = Path(p["path"]) / PARITY_FILES[p["kind"]]
        present = f.exists()
        info = {"name": p["name"], "path": p["path"], "kind": p["kind"], "present": present,
                "size": f.stat().st_size if present else 0, "needed": need}
        if Path(p["path"]).is_dir():
            info["free"] = _sh.disk_usage(p["path"]).free
        out["parity"].append(info)
    largest = max((d.get("protected_bytes", 0) for d in out["disks"]), default=0)
    out["warnings"] = []
    for p in out["parity"]:
        if p.get("free") is not None and p["free"] + p["size"] < need:
            out["warnings"].append(f"{p['name']} is running out of room: parity needs {need >> 20} MiB")
    if out["pending_parity"]:
        out["warnings"].append(f"{out['pending_parity']} stripe(s) wait for parity: run a sync")
    if any(not d["present"] for d in out["disks"]):
        out["warnings"].append("a data disk is missing: its files are served from parity; replace it and rebuild")
    con.close()
    out["largest_disk_bytes"] = largest
    return out
