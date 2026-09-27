"""Database health checks and self-repair.

check_database() runs SQLite's own `quick_check` against the live file,
plus the cheap signals that come before most real failures: free disk
space, an unbounded WAL, and whether the file opens at all.

repair_database() is the escalation ladder when the check fails. Each rung
is tried only if the previous one did not produce a sound database, and the
damaged file is always preserved first, never deleted:

1. REINDEX: fixes index-only corruption, the most common kind, losslessly.
2. Salvage: copy every row that can still be read into a fresh file (a
   row-level dump, the same idea as sqlite3's `.recover`), then swap it in.
   This keeps data newer than the last backup.
3. Restore the newest *verified* backup set (bot/sentinel/backup.py).

Every step is journalled, and the outcome goes out as an alert.
"""
from __future__ import annotations

import logging
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from bot.sentinel import journal

logger = logging.getLogger("bot.sentinel.repair")

MIN_FREE_BYTES = 512 * 1024 * 1024
WAL_WARN_BYTES = 256 * 1024 * 1024


def check_database() -> dict[str, Any]:
    from bot import db

    path = Path(db.DB_PATH)
    result: dict[str, Any] = {"path": str(path), "ok": True, "problems": []}
    try:
        rows = db.get_conn().execute("PRAGMA quick_check").fetchall()
        verdict = "; ".join(str(r[0]) for r in rows[:5])
    except sqlite3.DatabaseError as exc:
        verdict = f"unreadable: {exc}"
    result["quick_check"] = verdict
    if verdict != "ok":
        result["ok"] = False
        result["problems"].append(f"integrity: {verdict}")
    try:
        usage = shutil.disk_usage(path.parent)
        result["free_bytes"] = usage.free
        if usage.free < MIN_FREE_BYTES:
            result["problems"].append(f"low disk: {usage.free // (1024 * 1024)} MB free")
    except OSError:
        pass
    wal = Path(str(path) + "-wal")
    if wal.exists():
        result["wal_bytes"] = wal.stat().st_size
        if result["wal_bytes"] > WAL_WARN_BYTES:
            result["problems"].append(f"WAL is {result['wal_bytes'] // (1024 * 1024)} MB (checkpoints not keeping up)")
    return result


def checkpoint_wal() -> None:
    """Folds the WAL back into the main file. Safe at any time; TRUNCATE
    shrinks the -wal file on disk too."""
    from bot import db

    with db._lock:
        db.get_conn().execute("PRAGMA wal_checkpoint(TRUNCATE)")


def _quarantine(path: Path) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    dest_dir = path.parent / "quarantine" / stamp
    dest_dir.mkdir(parents=True, exist_ok=True)
    for suffix in ("", "-wal", "-shm"):
        f = Path(str(path) + suffix)
        if f.exists():
            shutil.copy2(f, dest_dir / f.name)
    return dest_dir


def _salvage(src: Path, dest: Path) -> dict[str, int]:
    """Copies the schema and every readable row from src into a new file at
    dest. Tables that fail part-way keep the rows read before the failure."""
    if dest.exists():
        dest.unlink()
    source = sqlite3.connect(str(src))
    target = sqlite3.connect(str(dest))
    copied: dict[str, int] = {}
    try:
        schema = source.execute(
            "SELECT type, name, sql FROM sqlite_master WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%' "
            "ORDER BY CASE type WHEN 'table' THEN 0 ELSE 1 END"
        ).fetchall()
        for kind, _name, sql in schema:
            if kind == "table":
                target.execute(sql)
        for kind, name, _sql in schema:
            if kind != "table":
                continue
            n = 0
            try:
                cols = [r[1] for r in source.execute(f'PRAGMA table_info("{name}")')]
                marks = ",".join("?" * len(cols))
                for row in source.execute(f'SELECT * FROM "{name}"'):  # noqa: S608 — names come from sqlite_master
                    target.execute(f'INSERT OR IGNORE INTO "{name}" VALUES ({marks})', row)  # noqa: S608
                    n += 1
            except sqlite3.DatabaseError:
                logger.warning("salvage: table %s readable only up to row %d", name, n)
            copied[name] = n
        for kind, name, sql in schema:
            if kind in ("index", "trigger", "view"):
                try:
                    target.execute(sql)
                except sqlite3.DatabaseError:
                    logger.warning("salvage: could not recreate %s %s", kind, name)
        user_version = source.execute("PRAGMA user_version").fetchone()[0]
        target.execute(f"PRAGMA user_version = {int(user_version)}")
        target.commit()
    finally:
        source.close()
        target.close()
    return copied


def repair_database(*, allow_restore: bool = True) -> dict[str, Any]:
    """Runs the escalation ladder. Returns {"ok", "steps", "quarantine"}."""
    from bot import db
    from bot.sentinel import backup

    path = Path(db.DB_PATH)
    steps: list[str] = []
    journal.record("repair", "database repair started", level="warning")
    quarantine = _quarantine(path)
    steps.append(f"preserved the damaged files in {quarantine}")

    # 1. REINDEX
    try:
        with db._lock:
            db.get_conn().execute("REINDEX")
            db.get_conn().commit()
        if backup.check_sqlite(path) == "ok":
            steps.append("REINDEX fixed it")
            return _done(True, steps, quarantine)
        steps.append("REINDEX was not enough")
    except sqlite3.DatabaseError as exc:
        steps.append(f"REINDEX failed: {exc}")

    # 2. Salvage readable rows into a fresh file
    salvaged = path.with_name(path.name + ".salvage")
    try:
        with db._lock:
            db.close_conn()
            copied = _salvage(quarantine / path.name, salvaged)
            if backup.check_sqlite(salvaged) == "ok":
                for suffix in ("-wal", "-shm"):
                    Path(str(path) + suffix).unlink(missing_ok=True)
                salvaged.replace(path)
                steps.append(f"salvaged {sum(copied.values())} row(s) across {len(copied)} table(s) into a fresh file")
                db.get_conn()
                return _done(True, steps, quarantine)
            steps.append("salvaged copy did not verify")
    except (sqlite3.DatabaseError, OSError) as exc:
        steps.append(f"salvage failed: {exc}")
    finally:
        salvaged.unlink(missing_ok=True)

    # 3. Restore the newest verified backup
    if allow_restore:
        latest = backup.latest_verified()
        if latest is not None:
            try:
                backup.restore_backup(latest["name"], parts=("db",))
                steps.append(f"restored the database from verified backup {latest['name']}")
                return _done(True, steps, quarantine)
            except (ValueError, OSError, sqlite3.DatabaseError) as exc:
                steps.append(f"restore from {latest['name']} failed: {exc}")
        else:
            steps.append("no verified backup to restore from")
    try:
        db.get_conn()
    except sqlite3.DatabaseError as exc:
        steps.append(f"the database still does not open: {exc}")
    return _done(False, steps, quarantine)


def _done(ok: bool, steps: list[str], quarantine: Path) -> dict[str, Any]:
    msg = ("database repaired: " if ok else "database repair FAILED: ") + "; ".join(steps)
    journal.alert("db.repair", msg, level="warning" if ok else "critical")
    if ok:
        journal.clear("db.integrity")
    return {"ok": ok, "steps": steps, "quarantine": str(quarantine)}
