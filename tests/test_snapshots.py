"""bot/snapshots.py — the point-in-time snapshot/restore safety net for
BotServer's own config + database, meant to let an agent editing this
codebase recover from a bad change without a full backup/rebuild.
Exercises real file/DB copies against temp paths, not mocks, since a
restore that silently does the wrong thing is exactly the failure mode
this module exists to prevent.
"""

from __future__ import annotations

import pytest

from bot import db as db_module
from bot import providers, snapshots
from bot.config import ConfigManager


@pytest.fixture(autouse=True)
def _isolated_paths(tmp_path, monkeypatch):
    snap_root = tmp_path / "snapshots"
    monkeypatch.setattr(snapshots, "SNAPSHOTS_ROOT", snap_root)

    backends_path = tmp_path / "backends.yaml"
    backends_path.write_text("default_backend: cli\n", encoding="utf-8")
    backends_manager = ConfigManager(path=backends_path)
    monkeypatch.setattr("bot.config.config", backends_manager)
    monkeypatch.setattr(snapshots, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(snapshots, "CONFIG_FILES", ("backends.yaml", "providers.yaml"))

    providers_path = tmp_path / "providers.yaml"
    providers_path.write_text("providers: {}\n", encoding="utf-8")
    monkeypatch.setattr(providers, "_manager", ConfigManager(path=providers_path))

    yield backends_manager


def test_create_snapshot_copies_config_files(temp_db):
    manifest = snapshots.create_snapshot(label="before-risky-edit")
    assert manifest["label"] == "before-risky-edit"
    snap_dir = snapshots.SNAPSHOTS_ROOT / manifest["name"]
    assert (snap_dir / "backends.yaml").read_text(encoding="utf-8") == "default_backend: cli\n"
    assert (snap_dir / "manifest.json").exists()


def test_create_snapshot_copies_live_database(temp_db):
    db_module.log_audit(actor="test", action="before_snapshot", detail="")
    manifest = snapshots.create_snapshot()
    snap_dir = snapshots.SNAPSHOTS_ROOT / manifest["name"]
    assert (snap_dir / "bot.db").exists()

    import sqlite3

    conn = sqlite3.connect(str(snap_dir / "bot.db"))
    rows = conn.execute("SELECT action FROM audit_log WHERE actor='test'").fetchall()
    conn.close()
    assert rows and rows[0][0] == "before_snapshot"


def test_list_snapshots_newest_first(temp_db):
    first = snapshots.create_snapshot(label="one")
    import time

    time.sleep(1.1)  # timestamp granularity is 1s
    second = snapshots.create_snapshot(label="two")
    listed = snapshots.list_snapshots()
    names = [s["name"] for s in listed]
    assert names[0] == second["name"]
    assert second["name"] in names and first["name"] in names


def test_list_snapshots_empty_when_none_taken(temp_db):
    assert snapshots.list_snapshots() == []


def test_restore_snapshot_restores_config_and_db(temp_db, _isolated_paths):
    backends_manager = _isolated_paths
    db_module.log_audit(actor="pre-restore", action="marker", detail="")
    manifest = snapshots.create_snapshot(label="good-state")

    # Simulate a bad edit after the snapshot: config changes, and a bogus
    # row gets written to the db.
    backends_manager.set_value(["default_backend"], "api", actor="test")
    db_module.log_audit(actor="post-restore", action="should-be-gone", detail="")

    snapshots.restore_snapshot(manifest["name"])

    assert backends_manager.current["default_backend"] == "cli"
    rows = db_module.get_conn().execute(
        "SELECT actor FROM audit_log WHERE action IN ('marker', 'should-be-gone')"
    ).fetchall()
    actors = {r["actor"] for r in rows}
    assert actors == {"pre-restore"}


def test_restore_missing_snapshot_raises(temp_db):
    with pytest.raises(ValueError):
        snapshots.restore_snapshot("does-not-exist")


def test_delete_snapshot(temp_db):
    manifest = snapshots.create_snapshot()
    assert snapshots.delete_snapshot(manifest["name"]) is True
    assert snapshots.list_snapshots() == []


def test_delete_missing_snapshot_returns_false(temp_db):
    assert snapshots.delete_snapshot("nope") is False
