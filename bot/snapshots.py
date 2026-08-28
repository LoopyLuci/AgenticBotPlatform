"""Point-in-time snapshot/restore for BotServer's own config and data —
the safety net for live development (an agent editing this codebase
while the app keeps running) to recover from a bad change without
reaching for a full backup/rebuild. Complements bot/config.py's
hot-reload (which handles "the new config is bad" by refusing it) with
"the new *data* turned out wrong" (a migration that corrupted rows, a
bug that wrote garbage) by keeping a point-in-time copy to go back to.

Creating a snapshot never requires stopping the app or interrupting an
in-flight request: the database is copied via sqlite3's own online
backup API (Connection.backup()), which the live, already-open
connection can run against itself without being closed. Restoring one
does briefly interrupt DB access — the shared connection has to be
closed and reopened to swap the underlying file safely — but that's a
few-millisecond in-process pause, not a process restart.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import time
from pathlib import Path
from typing import Optional

from bot.envfile import PROJECT_ROOT

SNAPSHOTS_ROOT = PROJECT_ROOT / "data" / "snapshots"

# Relative to PROJECT_ROOT. Both are small, non-WAL files — a plain copy
# is fine, unlike the database.
CONFIG_FILES = ("config/backends.yaml", "config/providers.yaml")


def _snapshot_dir(name: str) -> Path:
    return SNAPSHOTS_ROOT / name


def create_snapshot(label: Optional[str] = None) -> dict:
    """Copies the live config files and a consistent point-in-time copy
    of the live database into data/snapshots/<timestamp>[-<label>]/.
    Returns the manifest dict. Safe to call at any time — never stops or
    blocks the running app for more than the final commit of the
    database backup."""
    from bot import db as db_module

    timestamp = time.strftime("%Y%m%d-%H%M%S")
    name = f"{timestamp}-{label}" if label else timestamp
    dest_dir = _snapshot_dir(name)
    dest_dir.mkdir(parents=True, exist_ok=False)

    for rel in CONFIG_FILES:
        src = PROJECT_ROOT / rel
        if src.exists():
            shutil.copy2(src, dest_dir / Path(rel).name)

    if db_module.DB_PATH.exists():
        source_conn = db_module.get_conn()
        dest_conn = sqlite3.connect(str(dest_dir / "bot.db"))
        try:
            source_conn.backup(dest_conn)
        finally:
            dest_conn.close()

    manifest = {"name": name, "label": label, "created_at": timestamp}
    (dest_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return manifest


def list_snapshots() -> list[dict]:
    """Newest first. Skips any directory that isn't a real snapshot (no
    manifest.json) rather than raising — a snapshot directory the user
    poked at by hand shouldn't break this listing."""
    if not SNAPSHOTS_ROOT.exists():
        return []
    out = []
    for entry in sorted(SNAPSHOTS_ROOT.iterdir(), reverse=True):
        if not entry.is_dir():
            continue
        manifest_path = entry / "manifest.json"
        if not manifest_path.exists():
            continue
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        size = sum(f.stat().st_size for f in entry.iterdir() if f.is_file())
        out.append({**manifest, "size_bytes": size})
    return out


def restore_snapshot(name: str) -> None:
    """Restores config files and the database from a previously-created
    snapshot. Config files go through the same atomic write-then-replace
    pattern bot/config.py's own set_value() uses; the database is
    restored by closing the shared connection, replacing the file (and
    clearing any stale -wal/-shm so a half-applied WAL from before the
    restore can't resurface), and reopening. Raises ValueError if no
    snapshot named `name` exists."""
    src_dir = _snapshot_dir(name)
    if not (src_dir / "manifest.json").exists():
        raise ValueError(f"no snapshot named {name!r}")

    from bot import db as db_module
    from bot import providers as provider_registry
    from bot.config import config as backends_config

    for rel in CONFIG_FILES:
        src = src_dir / Path(rel).name
        if not src.exists():
            continue
        dest = PROJECT_ROOT / rel
        tmp = dest.with_suffix(dest.suffix + ".tmp")
        shutil.copy2(src, tmp)
        tmp.replace(dest)

    db_src = src_dir / "bot.db"
    if db_src.exists():
        db_module.close_conn()
        dest = db_module.DB_PATH
        for suffix in ("", "-wal", "-shm"):
            stale = Path(str(dest) + suffix)
            if stale.exists():
                stale.unlink()
        shutil.copy2(db_src, dest)
        db_module.get_conn()  # reopen eagerly so the next caller doesn't pay for it

    backends_config.reload(actor="snapshot-restore")
    provider_registry.reload(actor="snapshot-restore")


def delete_snapshot(name: str) -> bool:
    src_dir = _snapshot_dir(name)
    if not src_dir.exists():
        return False
    shutil.rmtree(src_dir)
    return True
