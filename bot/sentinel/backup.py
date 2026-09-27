"""Automatic, verified backups: everything needed to bring ABP back on a
fresh disk or a new machine.

A backup set is a directory under data/backups/<UTC timestamp>/ holding:

- bot.db and provider_store.db: consistent online copies via SQLite's
  backup API (never a raw file copy of a live WAL database);
- vault.key (without it, the encrypted provider keys are unreadable),
  server_id, .env, and config/*.yaml;
- the pinned Android signing keystore (ADR-0010), when this machine has one.

Every set is verified after it is written: each database copy must pass
`PRAGMA integrity_check`, and every file's SHA-256 goes into manifest.json.
Only verified sets are candidates for automatic restore (bot/sentinel/repair.py).

Retention keeps the newest N sets plus one per day and one per week, so a
problem noticed late can still be rolled back past. An optional mirror
directory (another drive, a synced folder, a NAS mount) receives a copy of
every set, which is what survives losing the disk.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from bot.envfile import PROJECT_ROOT

logger = logging.getLogger("bot.sentinel.backup")

BACKUPS_ROOT = PROJECT_ROOT / "data" / "backups"
MANIFEST = "manifest.json"
CONFIG_GLOB = "config/*.yaml"


def _db_sources() -> dict[str, Path]:
    from bot import db, provider_store

    return {"bot.db": Path(db.DB_PATH), "provider_store.db": Path(provider_store.STORE_PATH)}


def _file_sources() -> dict[str, Path]:
    from bot import envfile

    out: dict[str, Path] = {}
    try:
        from bot import vault

        out["vault.key"] = vault._dir() / "vault.key"
    except Exception:  # noqa: BLE001 — a missing vault module/dir just means nothing to copy
        pass
    out["server_id"] = PROJECT_ROOT / "data" / "server_id"
    out[".env"] = Path(envfile.resolve())
    for p in sorted(PROJECT_ROOT.glob(CONFIG_GLOB)):
        out[f"config/{p.name}"] = p
    keystore = Path(os.environ.get("ABP_ANDROID_KEYSTORE") or Path.home() / ".abp" / "android-release.keystore")
    out["android-release.keystore"] = keystore
    return out


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _online_copy(src: Path, dest: Path) -> None:
    from bot import db

    if src == Path(db.DB_PATH):
        source = db.get_conn()
        close_source = False
    else:
        source = sqlite3.connect(str(src))
        close_source = True
    target = sqlite3.connect(str(dest))
    try:
        source.backup(target)
    finally:
        target.close()
        if close_source:
            source.close()


def _user_version(path: Path) -> Optional[int]:
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            return int(conn.execute("PRAGMA user_version").fetchone()[0])
        finally:
            conn.close()
    except sqlite3.DatabaseError:
        return None


def latest_verified_for_schema(max_version: int, root: Optional[Path] = None) -> Optional[dict[str, Any]]:
    """Newest verified set whose database this build can open: what a
    downgrade rolls back to."""
    for m in list_backups(root):
        v = (m.get("files", {}).get("bot.db") or {}).get("schema_version")
        if m.get("verified") and v is not None and v <= max_version:
            return m
    return None


def check_sqlite(path: Path, *, quick: bool = False) -> str:
    """'ok', or SQLite's own description of what is wrong with the file."""
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            rows = conn.execute("PRAGMA quick_check" if quick else "PRAGMA integrity_check").fetchall()
        finally:
            conn.close()
    except sqlite3.DatabaseError as exc:
        return f"unreadable: {exc}"
    result = "; ".join(str(r[0]) for r in rows[:5])
    return "ok" if result == "ok" else result


def create_backup(reason: str = "scheduled", *, root: Optional[Path] = None) -> dict[str, Any]:
    """Writes and verifies one backup set. Returns its manifest. A set that
    fails verification is kept (for forensics) but marked verified=false."""
    root = root or BACKUPS_ROOT
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    dest = root / stamp
    n = 1
    while dest.exists():
        n += 1
        dest = root / f"{stamp}-{n}"
    dest.mkdir(parents=True)
    files: dict[str, dict[str, Any]] = {}
    problems: list[str] = []
    started = time.monotonic()

    for name, src in _db_sources().items():
        if not src.exists():
            continue
        out = dest / name
        try:
            _online_copy(src, out)
        except sqlite3.Error as exc:
            problems.append(f"{name}: copy failed: {exc}")
            continue
        verdict = check_sqlite(out)
        if verdict != "ok":
            problems.append(f"{name}: {verdict}")
        files[name] = {"sha256": _sha256(out), "bytes": out.stat().st_size, "integrity": verdict,
                       "schema_version": _user_version(out)}

    for name, src in _file_sources().items():
        if not src.is_file():
            continue
        out = dest / name
        out.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.copy2(src, out)
        except OSError as exc:
            problems.append(f"{name}: copy failed: {exc}")
            continue
        files[name] = {"sha256": _sha256(out), "bytes": out.stat().st_size, "source": str(src)}

    from bot import __version__

    manifest = {
        "name": dest.name,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "reason": reason,
        "app_version": __version__,
        "files": files,
        "verified": not problems and "bot.db" in files,
        "problems": problems,
        "seconds": round(time.monotonic() - started, 2),
    }
    (dest / MANIFEST).write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return manifest


def verify_backup(path: Path) -> list[str]:
    """Re-checks a backup set against its manifest: every file present with
    the recorded hash, every database copy still intact. [] means sound."""
    try:
        manifest = json.loads((path / MANIFEST).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return [f"manifest unreadable: {exc}"]
    problems = []
    for name, meta in manifest.get("files", {}).items():
        f = path / name
        if not f.is_file():
            problems.append(f"{name}: missing")
            continue
        if _sha256(f) != meta.get("sha256"):
            problems.append(f"{name}: hash mismatch (bit rot or tampering)")
            continue
        if name.endswith(".db"):
            verdict = check_sqlite(f)
            if verdict != "ok":
                problems.append(f"{name}: {verdict}")
    return problems


def list_backups(root: Optional[Path] = None) -> list[dict[str, Any]]:
    """Newest first."""
    root = root or BACKUPS_ROOT
    if not root.exists():
        return []
    out = []
    for d in sorted((p for p in root.iterdir() if p.is_dir()), reverse=True):
        try:
            manifest = json.loads((d / MANIFEST).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        manifest["size_bytes"] = sum(m.get("bytes", 0) for m in manifest.get("files", {}).values())
        manifest["path"] = str(d)
        out.append(manifest)
    return out


def latest_verified(root: Optional[Path] = None) -> Optional[dict[str, Any]]:
    for m in list_backups(root):
        if m.get("verified"):
            return m
    return None


def prune(keep_recent: int = 7, keep_daily: int = 14, keep_weekly: int = 8, root: Optional[Path] = None) -> list[str]:
    """Deletes sets outside the retention policy. The newest verified set is
    always kept, whatever the policy says. Returns the names removed."""
    sets = list_backups(root)
    keep: set[str] = {m["name"] for m in sets[:max(keep_recent, 1)]}
    newest_ok = next((m["name"] for m in sets if m.get("verified")), None)
    if newest_ok:
        keep.add(newest_ok)
    days: dict[str, str] = {}
    weeks: dict[str, str] = {}
    for m in sets:  # newest first, so the first per bucket is the newest of that day/week
        ts = m["name"][:8]
        try:
            d = datetime.strptime(ts, "%Y%m%d")
        except ValueError:
            continue
        days.setdefault(ts, m["name"])
        weeks.setdefault(f"{d.isocalendar()[0]}-{d.isocalendar()[1]}", m["name"])
    keep.update(list(days.values())[:keep_daily])
    keep.update(list(weeks.values())[:keep_weekly])
    removed = []
    for m in sets:
        if m["name"] not in keep:
            shutil.rmtree(m["path"], ignore_errors=True)
            removed.append(m["name"])
    return removed


def mirror(manifest: dict[str, Any], mirror_dir: Path) -> list[str]:
    """Copies one backup set to the mirror directory and verifies the copy
    there. Returns problems ([] on success)."""
    src = BACKUPS_ROOT / manifest["name"]
    dest = Path(mirror_dir) / manifest["name"]
    try:
        if dest.exists():
            shutil.rmtree(dest)
        shutil.copytree(src, dest)
    except OSError as exc:
        return [f"mirror copy to {mirror_dir} failed: {exc}"]
    return verify_backup(dest)


def restore_backup(name: str, *, parts: tuple[str, ...] = ("db", "config", "secrets"),
                   root: Optional[Path] = None) -> dict[str, Any]:
    """Restores from a backup set. The current files are first moved into
    data/backups/_replaced/<timestamp>/ (never deleted), so a restore can
    itself be undone. Raises ValueError for a missing or unsound set."""
    from bot import db

    root = root or BACKUPS_ROOT
    src = root / name
    problems = verify_backup(src)
    if problems:
        raise ValueError(f"backup {name} is not sound: {problems}")
    manifest = json.loads((src / MANIFEST).read_text(encoding="utf-8"))
    quarantine = BACKUPS_ROOT / "_replaced" / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    restored = []

    def _swap(backup_file: Path, live: Path) -> None:
        quarantine.mkdir(parents=True, exist_ok=True)
        live.parent.mkdir(parents=True, exist_ok=True)
        for suffix in ("", "-wal", "-shm"):
            old = Path(str(live) + suffix)
            if old.exists():
                shutil.move(str(old), str(quarantine / (live.name + suffix)))
        shutil.copy2(backup_file, live)
        restored.append(str(live))

    if "db" in parts:
        dbs = _db_sources()
        db.close_conn()
        try:
            from bot import provider_store

            closer = getattr(provider_store, "close", None)
            if callable(closer):
                closer()
        except Exception:  # noqa: BLE001
            pass
        for name_, live in dbs.items():
            if name_ in manifest["files"]:
                _swap(src / name_, live)
        db.get_conn()
    sources = _file_sources()
    for name_ in manifest["files"]:
        if name_.endswith(".db"):
            continue
        is_config = name_.startswith("config/")
        if (is_config and "config" in parts) or (not is_config and "secrets" in parts):
            live = sources.get(name_) or (PROJECT_ROOT / name_)
            _swap(src / name_, live)
    if "config" in parts:
        try:
            from bot import providers
            from bot.config import config

            config.reload(actor="sentinel-restore")
            providers.reload(actor="sentinel-restore")
        except Exception:  # noqa: BLE001 — config reload refuses bad files on its own and logs why
            logger.exception("config reload after restore failed")
    return {"restored": restored, "previous_files": str(quarantine) if quarantine.exists() else None}
