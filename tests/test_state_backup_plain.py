"""The plain copy (bot.state_backup.plain_copy): an unencrypted copy with no live credential in it. Nothing here touches a
real store: every database and config file is a fixture under pytest's tmp_path."""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from bot import state_backup as sb

SECRET = "credential-value-XYZ-123456"
KEY = "sk-abcdefghijklmnopqrstuvwxyz0123"


def make_bot_db(path: Path, credential: str = SECRET) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.executescript(f"""
        CREATE TABLE bot_instances (id INTEGER PRIMARY KEY, name TEXT, credentials TEXT);
        INSERT INTO bot_instances (name, credentials) VALUES ('a', '{credential}');
        CREATE TABLE audit_log (id INTEGER PRIMARY KEY, detail TEXT);
        INSERT INTO audit_log (detail) VALUES ('set key {KEY} for the bot');
        CREATE TABLE jobs (id INTEGER PRIMARY KEY, tokens INTEGER);
        INSERT INTO jobs (tokens) VALUES (42);
    """)
    conn.commit()
    conn.close()


@pytest.fixture
def live(tmp_path):
    root = tmp_path / "codebase"
    data = root / "data"
    (root / "config").mkdir(parents=True)
    (root / "config" / "backends.yaml").write_text(f"api_key: {KEY}\n", encoding="utf-8")
    make_bot_db(data / "bot.db")
    make_bot_db(data / "backups" / "2026-10-01" / "bot.db")         # an unlisted copy of the database
    return root, data


def copy_of(summary: dict) -> Path:
    return Path(summary["copy"])


def everything(folder: Path) -> bytes:
    return b"".join(p.read_bytes() for p in folder.rglob("*") if p.is_file())


def test_the_copy_holds_no_credential_and_no_key_shaped_text(live, tmp_path):
    root, data = live
    summary = sb.plain_copy(dest_root=tmp_path / "out", data=data, project_root=root, private_dir=None, tier_dirs=[])
    assert summary["verify_ok"]
    copy = copy_of(summary)
    assert SECRET.encode() not in everything(copy) and KEY.encode() not in everything(copy)
    assert not (copy / "config").exists()                       # config/backends.yaml holds a key: never copied
    assert "config/backends.yaml" in summary["excluded"]


def test_key_shaped_text_is_redacted_and_the_rest_of_the_row_is_kept(live, tmp_path):
    root, data = live
    copy = copy_of(sb.plain_copy(dest_root=tmp_path / "out", data=data, project_root=root, private_dir=None, tier_dirs=[]))
    conn = sqlite3.connect(copy / "db" / "bot.db")
    try:
        assert conn.execute("SELECT detail FROM audit_log").fetchone()[0] == "set key [redacted] for the bot"
        assert conn.execute("SELECT credentials FROM bot_instances").fetchone()[0] == ""
        assert conn.execute("SELECT tokens FROM jobs").fetchone()[0] == 42     # counts are not secrets
    finally:
        conn.close()


def test_unlisted_databases_are_copied_with_the_same_blanking(live, tmp_path):
    root, data = live
    copy = copy_of(sb.plain_copy(dest_root=tmp_path / "out", data=data, project_root=root, private_dir=None, tier_dirs=[]))
    unlisted = copy / "unlisted" / "backups" / "2026-10-01" / "bot.db"
    assert unlisted.is_file()
    conn = sqlite3.connect(unlisted)
    try:
        assert conn.execute("SELECT credentials FROM bot_instances").fetchone()[0] == ""
    finally:
        conn.close()


def test_the_live_databases_are_read_never_written(live, tmp_path):
    root, data = live
    before = {p: p.read_bytes() for p in data.rglob("*.db")}
    sb.plain_copy(dest_root=tmp_path / "out", data=data, project_root=root, private_dir=None, tier_dirs=[])
    assert {p: p.read_bytes() for p in data.rglob("*.db")} == before


def test_a_copy_that_still_holds_a_credential_is_removed_and_refused(live, tmp_path, monkeypatch):
    root, data = live
    monkeypatch.setattr(sb, "_scrub_copy", lambda db: {})           # a scrub that does nothing
    dest_root = tmp_path / "out"
    with pytest.raises(sb.StateBackupError, match="credential"):
        sb.plain_copy(dest_root=dest_root, data=data, project_root=root, private_dir=None, tier_dirs=[])
    assert not any(dest_root.glob("plain-*"))


def test_the_last_plain_copy_is_recorded_in_the_status(live, tmp_path):
    root, data = live
    summary = sb.plain_copy(dest_root=tmp_path / "out", data=data, project_root=root, private_dir=None, tier_dirs=[])
    assert sb.status(data)["last_plain"]["copy"] == summary["copy"]


def point_at(monkeypatch, data: Path, root: Path) -> None:
    monkeypatch.setattr(sb, "_defaults", lambda: (data, root, None, []))


def test_the_schedule_is_off_until_the_owner_turns_it_on(live, monkeypatch, tmp_path):
    root, data = live
    point_at(monkeypatch, data, root)
    assert sb.schedule() == {"enabled": False, "dest": ""}
    with pytest.raises(sb.StateBackupError, match="choose the folder"):
        sb.set_schedule(enabled=True, dest="")
    assert sb.set_schedule(enabled=True, dest=str(tmp_path / "copies")) == {"enabled": True, "dest": str(tmp_path / "copies")}
    sb.set_schedule(enabled=False, dest=str(tmp_path / "copies"))
    with pytest.raises(sb.StateBackupError, match="off"):
        sb.run_scheduled()


def test_a_copy_is_never_written_to_c(live, monkeypatch):
    root, data = live
    point_at(monkeypatch, data, root)
    with pytest.raises(sb.StateBackupError, match="never written to C:"):
        sb.set_schedule(enabled=True, dest="C:/owner-copies")
    with pytest.raises(sb.StateBackupError, match="never written to C:"):
        sb.plain_copy(dest_root=Path("C:/owner-copies"), data=data, project_root=root, private_dir=None, tier_dirs=[])


def test_the_daily_pass_does_nothing_while_the_schedule_is_off(monkeypatch):
    import asyncio

    from bot import retention
    ran = []
    monkeypatch.setattr(sb, "schedule", lambda: {"enabled": False, "dest": ""})
    monkeypatch.setattr(sb, "run_scheduled", lambda: ran.append(1))
    asyncio.run(retention.run_state_backup_once())
    assert ran == []


def test_the_daily_pass_writes_the_copy_when_on(monkeypatch):
    import asyncio

    from bot import retention
    ran = []
    monkeypatch.setattr(sb, "schedule", lambda: {"enabled": True, "dest": "D:/owner-copies"})
    monkeypatch.setattr(sb, "run_scheduled", lambda: ran.append(1) or {"copy": "x", "databases": 1})
    asyncio.run(retention.run_state_backup_once())
    assert ran == [1]


def test_the_recovery_api_schedules_and_copies_with_the_owner_token_only(live, monkeypatch, tmp_path):
    import time

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from bot.dashboard import fileserver_api

    root, data = live
    point_at(monkeypatch, data, root)
    monkeypatch.setenv("DASHBOARD_TOKEN", "owner-token")
    app = FastAPI()
    fileserver_api.register(app, lambda: None)
    c = TestClient(app)
    owner = {"X-Dashboard-Token": "owner-token"}
    dest = str(tmp_path / "copies")

    assert c.get("/api/fileserver/recovery").json()["schedule"] == {"enabled": False, "dest": ""}
    assert c.put("/api/fileserver/recovery/schedule", json={"enabled": True, "dest": dest}).status_code == 403
    assert c.put("/api/fileserver/recovery/schedule", json={"enabled": True, "dest": dest}, headers=owner).status_code == 200
    assert c.get("/api/fileserver/recovery").json()["schedule"]["enabled"] is True
    assert c.put("/api/fileserver/recovery/schedule", json={"enabled": True, "dest": "C:/owner-copies"},
                 headers=owner).status_code == 400
    assert c.post("/api/fileserver/recovery/plain-copy", json={"dest": "C:/owner-copies"}, headers=owner).status_code == 400
    assert c.post("/api/fileserver/recovery/plain-copy", json={"dest": dest}).status_code == 403

    run = c.post("/api/fileserver/recovery/plain-copy", json={"dest": dest}, headers=owner).json()
    got = {}
    for _ in range(400):
        got = c.get(f"/api/fileserver/runs/{run['run']}").json()
        if got["done"]:
            break
        time.sleep(0.05)
    assert got["done"] and got["error"] is None, got.get("error")
    assert got["result"]["verify_ok"] and Path(got["result"]["copy"]).is_dir()
