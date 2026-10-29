"""State backup (recovery plan P0, T1; contract C4): every store is staged, a configured tier folder outside the data
folder is in the backup, the drill restores and verifies, and the live stores are never written."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from bot import state_backup as sb

PASSWORD = "correct horse battery"


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _db(path: Path, rows: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE jobs (id INTEGER PRIMARY KEY, name TEXT)")
    conn.executemany("INSERT INTO jobs (name) VALUES (?)", [(f"job{i}",) for i in range(rows)])
    conn.commit()
    conn.close()


@pytest.fixture
def world(tmp_path):
    project = tmp_path / "codebase"                      # not "project": the conftest already makes that folder
    (project / "config").mkdir(parents=True)
    (project / "config" / "backends.yaml").write_text("backends: {}\n")
    (project / "config" / "providers.yaml").write_text("providers: {}\n")
    private = tmp_path / "private"
    (private / "config").mkdir(parents=True)
    (private / "config" / "private-backends.yaml").write_text("secret_like: value\n")
    data = tmp_path / "data"
    _db(data / "bot.db", rows=3)
    _db(data / "context_vault" / "index.db", rows=2)
    _db(data / "storage_tiering.db", rows=1)
    _db(data / "fileserver" / "store.db", rows=4)
    _db(data / "misc" / "stray.db", rows=5)              # no store claims this one: the coverage audit must say so
    _db(data / "snapshots" / "old.db", rows=6)           # snapshots are backups already: never walked
    (data / "vault.enc").write_bytes(b"ciphertext")
    (data / "vault.key").write_bytes(b"not-for-the-backup")
    tier = tmp_path / "elsewhere" / "nvme-tier"          # a tier folder outside the data folder
    tier.mkdir(parents=True)
    (tier / "item.bin").write_bytes(b"\x00\x01 tier body")
    return {"project": project, "private": private, "data": data, "tier": tier, "repo": tmp_path / "repo",
            "tmp": tmp_path}


def _plan(w):
    return sb.collect(data=w["data"], project_root=w["project"], private_dir=w["private"],
                      tier_dirs=[("0-nvme", w["tier"])])


def test_collect_claims_every_store_and_audits_the_rest(world):
    plan = _plan(world)
    files = {rel for rel, _ in plan.files}
    dbs = {rel for rel, _ in plan.sqlite}
    trees = {prefix for prefix, _ in plan.trees}
    assert {"config/backends.yaml", "config/providers.yaml", "private/config/private-backends.yaml", "vault/vault.enc"} <= files
    assert {"db/bot.db", "db/storage_tiering.db", "db/context_vault/index.db", "db/fileserver/store.db"} <= dbs
    assert trees == {"tiers/0-nvme"}
    assert [e["path"] for e in plan.excluded if e["label"] == "vault key"] == ["vault.key"]
    assert plan.unlisted == ["misc/stray.db"]


def test_a_tier_folder_outside_the_data_folder_is_in_the_backup(world):
    sb.run(repo=world["repo"], password=PASSWORD, data=world["data"], project_root=world["project"],
           private_dir=world["private"], tier_dirs=[("0-nvme", world["tier"])])
    from bot.fileserver import backup as fs_backup

    repo = fs_backup.Repo(world["repo"], PASSWORD)
    snap = repo.snapshot(repo.snapshots()[-1]["id"])
    staged = {f["path"] for f in snap["files"]}
    assert "tiers/0-nvme/item.bin" in staged
    assert "manifest.json" in staged and "db/bot.db" in staged
    assert "vault.key" not in staged                      # deliberately left out


def test_a_missing_tier_folder_is_reported_not_silently_skipped(world, tmp_path):
    plan = sb.collect(data=world["data"], project_root=world["project"], private_dir=world["private"],
                      tier_dirs=[("0-gone", tmp_path / "does-not-exist")])
    assert plan.trees == []
    assert any(e["label"] == "context-vault tier 0-gone" and e["reason"] == "folder does not exist" for e in plan.excluded)


def test_the_drill_restores_the_newest_backup_and_verifies_it(world):
    sb.run(repo=world["repo"], password=PASSWORD, data=world["data"], project_root=world["project"],
           private_dir=world["private"], tier_dirs=[("0-nvme", world["tier"])])
    report = sb.drill(repo=world["repo"], password=PASSWORD, data=world["data"])
    assert report["ok"] is True, report
    assert report["bad_files"] == [] and report["bad_databases"] == []
    assert report["databases"] == 4 and report["unlisted"] == ["misc/stray.db"]
    assert not any((world["data"] / "state_backup" / "drill").glob("drill-*"))   # scratch cleaned up


def test_run_never_writes_the_live_stores(world):
    live = [world["data"] / "bot.db", world["data"] / "vault.enc", world["project"] / "config" / "backends.yaml",
            world["tier"] / "item.bin"]
    before = {p: _sha(p) for p in live}
    sb.run(repo=world["repo"], password=PASSWORD, data=world["data"], project_root=world["project"],
           private_dir=world["private"], tier_dirs=[("0-nvme", world["tier"])])
    assert {p: _sha(p) for p in live} == before
    assert not (world["data"] / "state_backup" / "staging").exists() or not any((world["data"] / "state_backup" / "staging").iterdir())


def test_verify_catches_a_changed_file_and_a_wrong_database(world, tmp_path):
    staging = tmp_path / "stage"
    sb.stage(_plan(world), staging, runtime=None)
    assert sb.verify(staging)["ok"] is True
    (staging / "config" / "backends.yaml").write_text("tampered\n")
    (staging / "db" / "bot.db").unlink()
    _db(staging / "db" / "bot.db", rows=99)              # same name, different bytes: the SHA-256 catches it
    report = sb.verify(staging)
    assert report["ok"] is False
    assert report["bad_files"] == ["config/backends.yaml", "db/bot.db"]

    # Row counts are a second check: same bytes, but the manifest says the table should hold other rows.
    staging2 = tmp_path / "stage2"
    manifest = sb.stage(_plan(world), staging2, runtime=None)
    for entry in manifest["files"]:
        if entry["path"] == "db/bot.db":
            entry["tables"] = {"jobs": 999}
    (staging2 / "manifest.json").write_text(json.dumps(manifest))
    report2 = sb.verify(staging2)
    assert report2["bad_databases"] == ["db/bot.db"] and report2["bad_files"] == []


def test_the_database_copies_keep_their_row_counts(world, tmp_path):
    staging = tmp_path / "stage"
    manifest = sb.stage(_plan(world), staging, runtime=None)
    entry = next(e for e in manifest["files"] if e["path"] == "db/bot.db")
    assert entry["tables"] == {"jobs": 3}


def test_a_wrong_password_is_refused(world):
    sb.run(repo=world["repo"], password=PASSWORD, data=world["data"], project_root=world["project"],
           private_dir=world["private"], tier_dirs=[("0-nvme", world["tier"])])
    with pytest.raises(Exception, match="wrong repository password"):
        sb.drill(repo=world["repo"], password="not the password", data=world["data"])


def test_status_records_the_last_run_and_drill(world):
    sb.run(repo=world["repo"], password=PASSWORD, data=world["data"], project_root=world["project"],
           private_dir=world["private"], tier_dirs=[("0-nvme", world["tier"])])
    sb.drill(repo=world["repo"], password=PASSWORD, data=world["data"])
    status = sb.status(world["data"])
    assert status["last_run"]["files"] > 0 and status["last_run"]["unlisted"] == ["misc/stray.db"]
    assert status["last_drill"]["ok"] is True
