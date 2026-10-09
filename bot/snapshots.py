"""Point-in-time snapshot/restore for AgenticBotPlatform's own config and data —
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

A snapshot also holds the unit runtime's state (`runtime.json`: the live
manifest -- which units, versions, instances, settings and config were live --
and the stored branches). Restoring that part is one engine transaction (a
switch to the restored manifest, bot/runtime/snapshot.py), not a file copy
under a running engine.

What is atomic and what is not on restore: each config file and the database
file are written to a temporary file and moved into place with os.replace (a
crash leaves the old file or the new one, never half of one); the runtime part
is one engine transaction (all or nothing). The three parts together are *not*
one atomic step: config files, then the database, then the runtime transaction,
in that order, and a failure in a later part leaves the earlier ones restored
(the result reports it). The two config files are replaced one after the
other, so a crash between them leaves one restored and one not. Unit code is
not snapshotted: a restored unit version whose source has changed since cannot
be rebuilt, which fails the runtime transaction and leaves what is running
untouched.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import sqlite3
import time
from contextlib import nullcontext
from pathlib import Path
from typing import Any, Optional

from bot.envfile import PROJECT_ROOT

logger = logging.getLogger("bot.snapshots")

SNAPSHOTS_ROOT = PROJECT_ROOT / "data" / "snapshots"

# Relative to PROJECT_ROOT. Both are small, non-WAL files — a plain copy
# is fine, unlike the database.
CONFIG_FILES = ("config/backends.yaml", "config/providers.yaml")


def _snapshot_dir(name: str) -> Path:
    return SNAPSHOTS_ROOT / name


def create_snapshot(label: Optional[str] = None) -> dict:
    """Copies the live config files, a consistent point-in-time copy of the
    live database and the runtime's manifest and branches into
    data/snapshots/<timestamp>[-<label>]/. Returns the manifest dict. Safe
    to call at any time — never stops or blocks the running app for more
    than the final commit of the database backup."""
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

    runtime_part = None
    try:
        from bot.runtime import snapshot as runtime_snapshot

        runtime_part = runtime_snapshot.export()
    except Exception:  # noqa: BLE001 - best effort: config and data are saved either way
        logger.exception("snapshot: could not export the runtime state")
    if runtime_part is not None:
        (dest_dir / "runtime.json").write_text(json.dumps(runtime_part), encoding="utf-8")

    manifest = {"name": name, "label": label, "created_at": timestamp, "runtime": runtime_part is not None}
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


def _runtime_store() -> Any:
    """The running engine's state store (it holds the database file open), if there is one."""
    try:
        from bot.runtime import live

        return live._engine.store if live._engine is not None else None
    except Exception:  # noqa: BLE001
        return None


def restore_snapshot(name: str) -> dict:
    """Restores config files, the database and the runtime's manifest from a
    previously-created snapshot. Config files and the database are written to
    a temporary file and moved into place with os.replace; the database's
    shared connection (and the runtime's state store) is closed around the
    swap and reopened, and stale -wal/-shm files are cleared first so a
    half-applied WAL from before the restore can't resurface. The runtime part
    is one engine transaction (see the module docstring for what is and is not
    atomic). Raises ValueError if no snapshot named `name` exists. Returns
    {"runtime": {...}} describing the runtime part (None for a snapshot made
    before it existed)."""
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
        os.replace(tmp, dest)

    db_src = src_dir / "bot.db"
    if db_src.exists():
        store = _runtime_store()
        with (store.exclusive() if store is not None else nullcontext()):
            db_module.close_conn()
            dest = Path(db_module.DB_PATH)
            tmp = dest.with_name(dest.name + ".restore.tmp")
            shutil.copy2(db_src, tmp)  # the new file is complete before anything is touched
            for suffix in ("-wal", "-shm"):
                stale = Path(str(dest) + suffix)
                if stale.exists():
                    stale.unlink()
            os.replace(tmp, dest)
        db_module.get_conn()  # reopen eagerly so the next caller doesn't pay for it

    backends_config.reload(actor="snapshot-restore")
    provider_registry.reload(actor="snapshot-restore")

    runtime_result = None
    runtime_file = src_dir / "runtime.json"
    if runtime_file.exists():
        try:
            from bot.runtime import snapshot as runtime_snapshot

            runtime_result = runtime_snapshot.restore(json.loads(runtime_file.read_text(encoding="utf-8")),
                                                      label=f"snapshot restore {name}")
        except Exception as exc:  # noqa: BLE001 - config and data are restored; say what happened to the runtime
            logger.exception("snapshot restore: the runtime part failed")
            runtime_result = {"applied": False, "ok": False, "detail": f"{type(exc).__name__}: {exc}"}
    return {"runtime": runtime_result}


def delete_snapshot(name: str) -> bool:
    src_dir = _snapshot_dir(name)
    if not src_dir.exists():
        return False
    shutil.rmtree(src_dir)
    return True
