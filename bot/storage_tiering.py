"""Storage tiering: cold, write-once files move from the hot state folder to an archive location, and an index
keeps them findable and recallable.

Only categories that are written once and read rarely are tiered (see CATEGORIES):

    attachments       data/attachments/<file>            chat attachments (not thumbnails, not in-flight uploads)
    logs              logs/<name>.log.<n>                rotated log files (never the live log)
    support_bundles   logs/support_bundles/*.zip         diagnostic bundles

Never tiered, by construction: SQLite databases, config, .env, the vault, keys, anything under data/ that a
component keeps open (see _NEVER). Only files directly inside a category's folder are looked at.

A move is crash-safe: copy to `<archive>/<category>/<rel>.part` -> verify (size + sha256) -> rename into place ->
write the index row (state "copied") -> remove the hot copy -> mark the row "archived". A run that dies anywhere in
between is finished or rolled back by recover() at the start of the next run. A file another process has open (or
that changed while it was copied) is skipped and left where it is, never forced.

`storage_tiering.schedule.daily_at: "03:30"` (local time) runs apply() once a day from the scheduler's poll loop
(scheduled_tick -> a worker thread, one run at a time); the outcome is kept in the index and shown by status().

Settings (config/backends.yaml `storage_tiering:`) are off by default and need an explicit `archive_dir` that is
outside the state folder and writable. The index is data/storage_tiering.db. Surfaces: `python -m bot.storage_tiering
scan|apply|recall|status`, /api/fileserver/tiering/*, the Storage page's overview, and bot.attachments callers use
ensure_hot() so an archived attachment is brought back when it is opened.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import shutil
import sqlite3
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional

DAY = 86400.0
INDEX_NAME = "storage_tiering.db"
_CHUNK = 1024 * 1024

#: category -> (folder relative to the state root, file-name test, default after_days)
CATEGORIES: dict[str, dict[str, Any]] = {
    "attachments": {"dir": "data/attachments", "match": lambda n: True, "after_days": 90},
    "logs": {"dir": "logs", "match": lambda n: bool(re.search(r"\.log\.\d+$", n)), "after_days": 14},
    "support_bundles": {"dir": "logs/support_bundles", "match": lambda n: n.lower().endswith(".zip"), "after_days": 30},
}
DEFAULTS: dict[str, Any] = {"enabled": False, "archive_dir": "", "max_files": 200, "max_bytes": 2 * 1024 ** 3,
                            "schedule": {}}
#: Belt and braces: whatever a category folder holds, these are never moved.
_NEVER = re.compile(r"(\.(db|sqlite|sqlite3|db-wal|db-shm|db-journal|env|yaml|yml|key|pem|vault|part)|\.env\..*)$", re.I)


class TieringError(Exception):
    """A refusal or a failure the caller should show as-is."""


# ---- settings ------------------------------------------------------------------------------------------------------- #

def _state_root(root: Optional[Path] = None) -> Path:
    if root is not None:
        return Path(root)
    from bot.envfile import PROJECT_ROOT

    return Path(PROJECT_ROOT)


def settings(cfg: Optional[dict] = None) -> dict:
    """The effective settings: defaults filled in under the configured block."""
    if cfg is None:
        from bot.config import config

        cfg = config.current.get("storage_tiering") or {}
    out = {**DEFAULTS, **{k: v for k, v in cfg.items() if k in DEFAULTS}}
    cats = cfg.get("categories") or {}
    out["categories"] = {}
    for name, spec in CATEGORIES.items():
        c = cats.get(name) or {}
        out["categories"][name] = {"enabled": bool(c.get("enabled", True)),
                                   "after_days": float(c.get("after_days", spec["after_days"]))}
    out["enabled"] = bool(out["enabled"])
    out["archive_dir"] = str(out["archive_dir"] or "").strip()
    sched = out["schedule"] if isinstance(out["schedule"], dict) else {}
    out["schedule"] = {"daily_at": str(sched.get("daily_at") or "").strip()}
    return out


def check_archive(cfg: dict, root: Optional[Path] = None) -> Path:
    """The validated archive folder, or TieringError. Must be set, outside the state folder (and not containing it),
    and writable."""
    raw = cfg.get("archive_dir") or ""
    if not str(raw).strip():
        raise TieringError("storage_tiering.archive_dir is not set: choose the archive location explicitly")
    state = _state_root(root).resolve()
    arch = Path(str(raw)).expanduser().resolve()
    if arch == state or arch.is_relative_to(state):
        raise TieringError("storage_tiering.archive_dir must be outside ABP's data folder")
    if state.is_relative_to(arch):
        raise TieringError("storage_tiering.archive_dir must not contain ABP's data folder")
    try:
        arch.mkdir(parents=True, exist_ok=True)
        probe = arch / f".abp-write-test-{os.getpid()}"
        probe.write_bytes(b"x")
        probe.unlink()
    except OSError as e:
        raise TieringError(f"storage_tiering.archive_dir is not writable: {e}") from e
    return arch


def _need_ready(cfg: dict, root: Optional[Path]) -> Path:
    if not cfg["enabled"]:
        raise TieringError("storage tiering is off (storage_tiering.enabled: false)")
    return check_archive(cfg, root)


# ---- index ---------------------------------------------------------------------------------------------------------- #

def _index(root: Path) -> sqlite3.Connection:
    p = root / "data" / INDEX_NAME
    p.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(p), timeout=10)
    con.row_factory = sqlite3.Row
    con.execute("""CREATE TABLE IF NOT EXISTS tiered (
        path TEXT PRIMARY KEY, category TEXT NOT NULL, size INTEGER NOT NULL, sha256 TEXT NOT NULL,
        moved_at REAL NOT NULL, archive_path TEXT NOT NULL, state TEXT NOT NULL)""")
    con.execute("CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT NOT NULL)")
    con.commit()
    return con


def _rel(root: Path, p: Path) -> str:
    return p.relative_to(root).as_posix()


def sha256_of(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        while b := f.read(_CHUNK):
            h.update(b)
    return h.hexdigest()


def _copy_file(src: Path, dst: Path) -> None:
    """The copy step on its own so a test can corrupt it."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, dst)


def _is_locked(p: Path) -> bool:
    """True when another process holds the file open in a way that blocks a rename/delete (Windows), or it cannot be
    opened for writing at all. A rename to a temporary name and back is the probe."""
    tmp = p.with_name(p.name + ".tiering-probe")
    try:
        with open(p, "r+b"):
            pass
        os.replace(p, tmp)
    except OSError:
        return True
    try:
        os.replace(tmp, p)
    except OSError:
        return True
    return False


def _remove_hot(p: Path) -> bool:
    """Delete via a rename, which fails while the file is open elsewhere. False = could not (left in place)."""
    tomb = p.with_name(p.name + ".tiering-del")
    try:
        os.replace(p, tomb)
    except OSError:
        return False
    try:
        tomb.unlink()
    except OSError:
        pass
    return True


# ---- scan ----------------------------------------------------------------------------------------------------------- #

def _candidates(root: Path, cfg: dict, now: float) -> list[dict]:
    out: list[dict] = []
    for cat, spec in CATEGORIES.items():
        c = cfg["categories"][cat]
        if not c["enabled"]:
            continue
        d = root / spec["dir"]
        if not d.is_dir():
            continue
        cutoff = now - c["after_days"] * DAY
        for p in sorted(d.iterdir()):
            try:
                if p.is_symlink() or not p.is_file():
                    continue
                n = p.name
                if _NEVER.search(n) or n.endswith((".tiering-probe", ".tiering-del")) or not spec["match"](n):
                    continue
                st = p.stat()
            except OSError:
                continue
            if st.st_mtime <= cutoff:
                out.append({"path": _rel(root, p), "category": cat, "size": st.st_size, "mtime": st.st_mtime,
                            "age_days": round((now - st.st_mtime) / DAY, 1)})
    out.sort(key=lambda r: r["mtime"])  # oldest first, so a budget takes the coldest data
    return out


def scan(root: Optional[Path] = None, cfg: Optional[dict] = None, now: Optional[float] = None) -> dict:
    """What apply() would move: a dry run. Refuses like apply() does when tiering is not set up."""
    root = _state_root(root)
    cfg = settings(cfg)
    _need_ready(cfg, root)
    now = time.time() if now is None else now
    found = _candidates(root, cfg, now)
    picked, total = _within_budget(found, cfg)
    return {"candidates": picked, "files": len(picked), "bytes": total, "over_budget": len(found) - len(picked)}


def _within_budget(found: list[dict], cfg: dict) -> tuple[list[dict], int]:
    picked, total = [], 0
    for r in found:
        if len(picked) >= int(cfg["max_files"]) or (picked and total + r["size"] > int(cfg["max_bytes"])):
            break
        picked.append(r)
        total += r["size"]
    return picked, total


# ---- apply / recover ------------------------------------------------------------------------------------------------ #

def recover(root: Optional[Path] = None, cfg: Optional[dict] = None) -> list[dict]:
    """Finish or roll back moves a crashed run left at state "copied". Hot copy still identical -> finish the delete;
    hot copy gone and the archive copy verifies -> mark archived; hot copy changed or archive copy bad -> roll back
    (drop the archive copy and the row)."""
    root = _state_root(root)
    cfg = settings(cfg)
    arch = check_archive(cfg, root)
    done: list[dict] = []
    con = _index(root)
    try:
        for row in con.execute("SELECT * FROM tiered WHERE state='copied'").fetchall():
            hot, cold = root / row["path"], arch / row["archive_path"]
            cold_ok = cold.is_file() and cold.stat().st_size == row["size"] and sha256_of(cold) == row["sha256"]
            hot_exists = hot.is_file()
            if hot_exists and cold_ok and sha256_of(hot) == row["sha256"]:
                if _remove_hot(hot):
                    con.execute("UPDATE tiered SET state='archived' WHERE path=?", (row["path"],))
                    done.append({"path": row["path"], "action": "finished"})
                else:
                    done.append({"path": row["path"], "action": "pending-locked"})
            elif not hot_exists and cold_ok:
                con.execute("UPDATE tiered SET state='archived' WHERE path=?", (row["path"],))
                done.append({"path": row["path"], "action": "finished"})
            else:  # the hot file changed or the archive copy is bad: keep the hot file, forget the move
                cold.unlink(missing_ok=True) if hot_exists else None
                con.execute("DELETE FROM tiered WHERE path=?", (row["path"],))
                done.append({"path": row["path"], "action": "rolled-back"})
            con.commit()
    finally:
        con.close()
    return done


def apply(root: Optional[Path] = None, cfg: Optional[dict] = None, now: Optional[float] = None) -> dict:
    """Move the candidates (within the per-run budget). Returns what was moved and what was skipped and why."""
    root = _state_root(root)
    cfg = settings(cfg)
    arch = _need_ready(cfg, root)
    now = time.time() if now is None else now
    recovered = recover(root, cfg)
    picked, _ = _within_budget(_candidates(root, cfg, now), cfg)
    moved: list[dict] = []
    skipped: list[dict] = []
    con = _index(root)
    try:
        for r in picked:
            rel = r["path"]
            hot = root / rel
            if con.execute("SELECT 1 FROM tiered WHERE path=?", (rel,)).fetchone():
                skipped.append({"path": rel, "reason": "already indexed"})
                continue
            if _is_locked(hot):
                skipped.append({"path": rel, "reason": "in use"})
                continue
            cold_rel = f"{r['category']}/{Path(rel).name}"
            cold = arch / cold_rel
            part = cold.with_name(cold.name + ".part")
            try:
                before = hot.stat()
                digest = sha256_of(hot)
                _copy_file(hot, part)
                if part.stat().st_size != before.st_size or sha256_of(part) != digest:
                    part.unlink(missing_ok=True)
                    skipped.append({"path": rel, "reason": "verification failed (size or sha256)"})
                    continue
                after = hot.stat()
                if (after.st_size, after.st_mtime_ns) != (before.st_size, before.st_mtime_ns):
                    part.unlink(missing_ok=True)
                    skipped.append({"path": rel, "reason": "changed while copying"})
                    continue
                os.replace(part, cold)
                con.execute("INSERT OR REPLACE INTO tiered VALUES (?,?,?,?,?,?,?)",
                            (rel, r["category"], before.st_size, digest, time.time(), cold_rel, "copied"))
                con.commit()
                if not _remove_hot(hot):  # opened since the probe: undo, never force
                    con.execute("DELETE FROM tiered WHERE path=?", (rel,))
                    con.commit()
                    cold.unlink(missing_ok=True)
                    skipped.append({"path": rel, "reason": "in use"})
                    continue
                con.execute("UPDATE tiered SET state='archived' WHERE path=?", (rel,))
                con.commit()
                moved.append({"path": rel, "category": r["category"], "size": before.st_size, "archive_path": cold_rel})
            except OSError as e:
                part.unlink(missing_ok=True)
                skipped.append({"path": rel, "reason": f"{type(e).__name__}: {e}"})
    finally:
        con.close()
    return {"moved": moved, "skipped": skipped, "recovered": recovered, "files": len(moved),
            "bytes": sum(m["size"] for m in moved)}


# ---- recall / status ------------------------------------------------------------------------------------------------ #

def recall(path: str, root: Optional[Path] = None, cfg: Optional[dict] = None, keep_archive: bool = False) -> dict:
    """Bring one archived file back to its original place, verified against the indexed sha256. `path` is the
    indexed path (relative to the state folder, forward slashes) or an absolute path inside it."""
    root = _state_root(root)
    cfg = settings(cfg)
    arch = check_archive(cfg, root)  # recall works while disabled; it still needs a valid archive location
    rel = _norm(root, path)
    # Two downloads of one archived attachment at once must not both write the same .recall.part: one recalls, the
    # other then finds the file back in place.
    with _recall_lock:
        return _recall_locked(root, arch, rel, keep_archive)


_recall_lock = threading.RLock()   # re-entered by ensure_hot -> recall


def _recall_locked(root: Path, arch: Path, rel: str, keep_archive: bool) -> dict:
    con = _index(root)
    try:
        row = con.execute("SELECT * FROM tiered WHERE path=?", (rel,)).fetchone()
        if row is None:
            raise TieringError(f"{rel} is not in the tiering index")
        hot, cold = root / rel, arch / row["archive_path"]
        if hot.exists():
            raise TieringError(f"{rel} already exists in the data folder")
        if not cold.is_file():
            raise TieringError(f"archive copy {row['archive_path']} is missing")
        tmp = hot.with_name(hot.name + ".recall.part")
        _copy_file(cold, tmp)
        if tmp.stat().st_size != row["size"] or sha256_of(tmp) != row["sha256"]:
            tmp.unlink(missing_ok=True)
            raise TieringError(f"archive copy of {rel} failed verification (size or sha256); nothing restored")
        os.replace(tmp, hot)
        con.execute("DELETE FROM tiered WHERE path=?", (rel,))
        con.commit()
        if not keep_archive:
            cold.unlink(missing_ok=True)
        return {"path": rel, "size": row["size"], "category": row["category"]}
    finally:
        con.close()


def _norm(root: Path, path: str) -> str:
    p = Path(path)
    if p.is_absolute():
        try:
            return _rel(root.resolve(), p.resolve())
        except ValueError:
            return path
    return str(path).replace("\\", "/").lstrip("/")


def is_archived(path: Path, root: Optional[Path] = None) -> bool:
    """Cheap check used by callers: is this hot path in the index? (No archive access.)"""
    root = _state_root(root)
    if not (root / "data" / INDEX_NAME).is_file():
        return False
    try:
        rel = _rel(root.resolve(), Path(path).resolve())
    except ValueError:
        return False
    con = _index(root)
    try:
        return con.execute("SELECT 1 FROM tiered WHERE path=? AND state='archived'", (rel,)).fetchone() is not None
    finally:
        con.close()


def ensure_hot(path: Path, root: Optional[Path] = None, cfg: Optional[dict] = None) -> bool:
    """For callers that are about to open `path`: if it is missing because it was tiered, recall it first. True when
    the file is there afterwards. Never raises; a failed recall just leaves the file missing (the caller's own
    'file missing' answer applies, and status() shows it as archived)."""
    p = Path(path)
    if p.exists():
        return True
    with _recall_lock:               # one check-and-recall at a time: a second caller finds the file back
        if p.exists():
            return True
        try:
            if not is_archived(p, root):
                return False
            recall(str(p), root, cfg)
        except Exception:  # noqa: BLE001 - archive drive away, etc.
            pass
        return p.is_file()


def status(root: Optional[Path] = None, cfg: Optional[dict] = None) -> dict:
    """Read-only summary for the CLI, the API and the Storage overview. Never raises for a bad setup."""
    root = _state_root(root)
    cfg = settings(cfg)
    out: dict[str, Any] = {"enabled": cfg["enabled"], "archive_dir": cfg["archive_dir"], "ready": False, "error": "",
                           "categories": {k: dict(v) for k, v in cfg["categories"].items()},
                           "archived": {"files": 0, "bytes": 0}, "by_category": {}}
    try:
        check_archive(cfg, root)
        out["ready"] = True
    except TieringError as e:
        out["error"] = str(e)
    if (root / "data" / INDEX_NAME).is_file():
        con = _index(root)
        try:
            for r in con.execute("SELECT category, COUNT(*) n, SUM(size) b FROM tiered WHERE state='archived' "
                                 "GROUP BY category"):
                out["by_category"][r["category"]] = {"files": r["n"], "bytes": r["b"] or 0}
            out["archived"] = {"files": sum(v["files"] for v in out["by_category"].values()),
                               "bytes": sum(v["bytes"] for v in out["by_category"].values())}
            out["pending"] = con.execute("SELECT COUNT(*) FROM tiered WHERE state='copied'").fetchone()[0]
            row = con.execute("SELECT v FROM meta WHERE k='last_scheduled_run'").fetchone()
            if row:
                out["last_scheduled_run"] = json.loads(row["v"])
        finally:
            con.close()
    out["schedule"] = dict(cfg["schedule"])
    if cfg["schedule"]["daily_at"] and _parse_daily(cfg["schedule"]["daily_at"]) is None:
        out["schedule_error"] = f"storage_tiering.schedule.daily_at {cfg['schedule']['daily_at']!r} is not HH:MM"
    return out


def listing(root: Optional[Path] = None, limit: int = 200) -> list[dict]:
    root = _state_root(root)
    if not (root / "data" / INDEX_NAME).is_file():
        return []
    con = _index(root)
    try:
        return [dict(r) for r in con.execute("SELECT * FROM tiered ORDER BY moved_at DESC LIMIT ?", (limit,))]
    finally:
        con.close()


# ---- schedule ------------------------------------------------------------------------------------------------------- #

_run_lock = threading.Lock()   # one scheduled run at a time


def _parse_daily(text: str) -> Optional[tuple[int, int]]:
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", text.strip())
    if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
        return None
    return int(m.group(1)), int(m.group(2))


def schedule_due(cfg: dict, last_at: float, now: float) -> bool:
    """True when today's `daily_at` (local time) has passed and no run was attempted since it. A run missed because
    ABP was down starts when ABP is next up the same day; a failed run is not retried until the next day."""
    hm = _parse_daily(cfg["schedule"]["daily_at"]) if cfg["schedule"]["daily_at"] else None
    if hm is None:
        return False
    local = time.localtime(now)
    slot = time.mktime((local.tm_year, local.tm_mon, local.tm_mday, hm[0], hm[1], 0, 0, 0, -1))
    return now >= slot and last_at < slot


def _record_run(root: Path, rec: dict) -> None:
    con = _index(root)
    try:
        con.execute("INSERT OR REPLACE INTO meta VALUES ('last_scheduled_run', ?)", (json.dumps(rec),))
        con.commit()
    finally:
        con.close()


def _last_run_at(root: Path) -> float:
    if not (root / "data" / INDEX_NAME).is_file():
        return 0.0
    con = _index(root)
    try:
        row = con.execute("SELECT v FROM meta WHERE k='last_scheduled_run'").fetchone()
        return float(json.loads(row["v"]).get("at", 0)) if row else 0.0
    except (ValueError, TypeError):
        return 0.0
    finally:
        con.close()


def run_scheduled(root: Optional[Path] = None, cfg: Optional[dict] = None, now: Optional[float] = None) -> Optional[dict]:
    """Blocking: apply() if the schedule says a run is due, tiering is enabled and the archive is valid; the outcome
    is recorded (status() shows it). None when nothing was due or a run is already going. Never raises."""
    try:
        root = _state_root(root)
        cfg = settings(cfg)
        now = time.time() if now is None else now
        if not cfg["enabled"] or not schedule_due(cfg, _last_run_at(root), now):
            return None
        if not _run_lock.acquire(blocking=False):
            return None
        try:
            rec: dict[str, Any] = {"at": now}
            try:
                res = apply(root, cfg, now)
                rec.update(ok=True, files=res["files"], bytes=res["bytes"], skipped=len(res["skipped"]))
            except Exception as e:  # noqa: BLE001 - a bad archive or a disk error: record it, try again tomorrow
                rec.update(ok=False, error=f"{type(e).__name__}: {e}")
            _record_run(root, rec)
            return rec
        finally:
            _run_lock.release()
    except Exception:  # noqa: BLE001
        logging.getLogger("bot.storage_tiering").exception("scheduled storage tiering run failed")
        return None


async def scheduled_tick(root: Optional[Path] = None, cfg: Optional[dict] = None, now: Optional[float] = None) -> Optional[dict]:
    """Called by the scheduler's poll loop every few seconds: a cheap config check, and the run itself in a worker
    thread so the event loop never waits on file copies."""
    if cfg is None:
        try:
            if not settings().get("enabled"):
                return None
        except Exception:  # noqa: BLE001
            return None
    rec = await asyncio.to_thread(run_scheduled, root, cfg, now)
    if rec is not None:
        logging.getLogger("bot.storage_tiering").log(
            logging.INFO if rec.get("ok") else logging.WARNING, "scheduled storage tiering run: %s", rec)
    return rec


# ---- CLI ------------------------------------------------------------------------------------------------------------ #

def main(argv: Optional[list[str]] = None, out: Callable[[str], None] = print) -> int:
    import argparse

    ap = argparse.ArgumentParser(prog="python -m bot.storage_tiering",
                                 description="Move cold files to the archive location and bring them back.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("scan", help="list what would be moved (moves nothing)")
    sub.add_parser("apply", help="move the candidates now")
    r = sub.add_parser("recall", help="bring one archived file back")
    r.add_argument("path", help="the indexed path, e.g. data/attachments/<name>")
    sub.add_parser("status", help="settings, archive health and what is archived")
    a = ap.parse_args(argv)
    try:
        if a.cmd == "scan":
            res: Any = scan()
        elif a.cmd == "apply":
            res = apply()
        elif a.cmd == "recall":
            res = recall(a.path)
        else:
            res = status()
    except TieringError as e:
        out(f"refused: {e}")
        return 2
    out(json.dumps(res, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
