"""The runtime's part of a snapshot (bot/snapshots.py): which units, versions, instances and config were live, and the
stored branches. Restoring it is one engine transaction (a switch to the restored manifest), never a file copy under a
running engine.

* `export()` -> a JSON-able dict, or None when the runtime has never run in this data folder.
* `restore(data, label)` -> {"applied": bool, "ok": bool, ...}. Stored branches are written first (atomically, one file
  each); then, with the engine running, the live manifest is applied as a transaction (`engine.apply`): it stages the
  restored versions beside the running ones, health-checks them and commits once, emitting the usual `runtime.changed`
  event, so a version that cannot be rebuilt (its code changed since the snapshot) fails the transaction and leaves
  what runs untouched. With the engine not running, the live branch file is written (atomically) and takes effect at the
  next start. The manifest before the restore becomes the `previous` branch, so a restore can be rolled back.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Optional

from bot.runtime.manifest import LIVE, PREVIOUS, BranchStore, Manifest, valid_branch_name

logger = logging.getLogger("bot.runtime.snapshot")

FORMAT = 1


def _store_dir() -> Path:
    from bot import db

    return Path(db.DB_PATH).parent / "runtime" / "branches"


def _running() -> Any:
    from bot.runtime import live

    return live._engine if (live._engine is not None and live.booted) else None


def export() -> Optional[dict]:
    engine = _running()
    if engine is not None:
        manifest, store, generation = engine.manifest, engine.branches, engine.generation
    else:
        directory = _store_dir()
        if not directory.is_dir():
            return None
        store, generation = BranchStore(directory), None
        manifest = store.get(LIVE)
        if manifest is None:
            return None
    branches = {}
    for name in store.names():
        if name == LIVE:
            continue
        stored = store.get(name)
        if stored is not None:
            branches[name] = stored.to_dict()
    return {"format": FORMAT, "generation": generation, "manifest_id": manifest.id,
            "live": manifest.to_dict(), "branches": branches}


def restore(data: dict, label: str = "snapshot restore") -> dict[str, Any]:
    from bot.runtime import live

    if not isinstance(data, dict) or "live" not in data:
        raise ValueError("the snapshot has no usable runtime manifest")
    manifest = Manifest.from_dict(data["live"])
    engine = _running()
    store = engine.branches if engine is not None else BranchStore(_store_dir())
    for name, raw in (data.get("branches") or {}).items():
        if name in (LIVE, PREVIOUS) or not valid_branch_name(name):
            continue
        store.save(name, Manifest.from_dict(raw))
    if engine is None:
        store.save(LIVE, manifest)
        return {"applied": False, "ok": True, "manifest_id": manifest.id,
                "detail": "the runtime is not running; the restored manifest is used at the next start"}
    report = live.run_sync(lambda: engine.apply(manifest, label=label), timeout=180.0)
    if report is None:      # called on the engine's own loop: the transaction is scheduled and cannot be waited for
        return {"applied": False, "ok": True, "scheduled": True, "manifest_id": manifest.id,
                "detail": "the restore transaction was scheduled"}
    return {"applied": True, "ok": report.ok, "manifest_id": manifest.id, "generation": report.generation,
            "report": report.to_dict(), "detail": "" if report.ok else report.error}
