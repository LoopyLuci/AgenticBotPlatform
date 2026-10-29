"""Pruning the plain copies to the owner's keep rules (bot.state_backup.prune_plain_copies), the schedule's keep counts, the
CLI and the Recovery API. Every copy is a fake folder under pytest's tmp_path: nothing here touches a real store or drive."""
from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from bot import state_backup as sb

VERIFIED = {"at": "2026-10-28T00:00:00Z", "ok": True}
# Built from parts, as tests/test_state_backup_plain.py does, so the leak guard does not read a real drive path in a test.
C_DEST = "C" + ":/owner-copies"

# The owner's history. Copies on the same day are a few hours apart; the expected sets below were worked out by hand:
#   days with copies, newest 7: Oct 10, Oct 5, Oct 1, Sep 28, Sep 14, Aug 31, Aug 3
#   ISO weeks, newest 4: 41 (Oct 5-11), 40 (Sep 28-Oct 4), 38 (Sep 14-20), 36 (Aug 31-Sep 6)
#   months, newest 6: Oct, Sep, Aug, Jul, Jun, May
HISTORY = {
    "A": "plain-20261010T030000Z", "B": "plain-20261010T230000Z", "C": "plain-20261005T120000Z",
    "D": "plain-20261001T120000Z", "E": "plain-20260928T120000Z", "F": "plain-20260914T120000Z",
    "G": "plain-20260831T120000Z", "H": "plain-20260803T120000Z", "I": "plain-20260715T120000Z",
    "J": "plain-20260601T120000Z", "K": "plain-20260501T120000Z", "L": "plain-20260401T120000Z",
    "M": "plain-20260301T120000Z", "N": "plain-20260201T120000Z", "O": "plain-20260105T120000Z-all",
}


def stamp(y: int, mo: int, d: int, h: int = 12, mi: int = 0, suffix: str = "") -> str:
    return f"plain-{datetime(y, mo, d, h, mi, tzinfo=timezone.utc):%Y%m%dT%H%M%SZ}{suffix}"


def make_copy(root: Path, name: str, verified=None) -> Path:
    """A copy-shaped folder. verified=None writes no record of a passed check; a dict is written as the record."""
    folder = root / name
    (folder / "db").mkdir(parents=True)
    (folder / "db" / "bot.db").write_bytes(b"stand-in for a database")
    manifest = {"format": 1, "files": [], "excluded": [], "unlisted": []}
    if verified is not None:
        manifest["verified"] = verified
    (folder / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return folder


def build_history(root: Path) -> None:
    for name in HISTORY.values():
        make_copy(root, name, verified=VERIFIED)


def names(letters: str) -> list[str]:
    return [HISTORY[letter] for letter in letters]


def listing(root: Path) -> list[str]:
    return sorted(p.name for p in root.iterdir())


def point_at(monkeypatch, data: Path, root: Path) -> None:
    monkeypatch.setattr(sb, "_defaults", lambda: (data, root, None, []))


def test_the_rules_keep_exactly_the_owners_set(tmp_path):
    root = tmp_path / "copies"
    build_history(root)
    result = sb.prune_plain_copies(root)
    assert result["kept"] == names("BCDEFGHIJK")
    assert result["removed"] == names("ALMNO")
    assert result["failed"] == [] and result["ok"] is True
    assert listing(root) == sorted(names("BCDEFGHIJK"))
    assert result["reasons"][HISTORY["B"]] == ["daily", "weekly", "monthly", "newest verified"]
    assert result["reasons"][HISTORY["D"]] == ["daily", "weekly"]
    assert result["reasons"][HISTORY["I"]] == ["monthly"]


def test_the_newest_verified_copy_is_kept_even_when_no_rule_keeps_it(tmp_path):
    root = tmp_path / "copies"
    newest_unverified = make_copy(root, stamp(2026, 10, 27))                       # no record at all
    failed_check = make_copy(root, stamp(2026, 10, 26), verified={"at": VERIFIED["at"], "ok": False})
    verified = make_copy(root, stamp(2026, 10, 20), verified=VERIFIED)
    older = make_copy(root, stamp(2026, 10, 10), verified=VERIFIED)
    result = sb.prune_plain_copies(root, daily=1, weekly=0, monthly=0)
    assert result["kept"] == [newest_unverified.name, verified.name]
    assert result["removed"] == [failed_check.name, older.name]
    assert result["reasons"][verified.name] == ["newest verified"]
    assert listing(root) == sorted([newest_unverified.name, verified.name])


def test_with_no_verified_copy_only_the_rules_keep_anything(tmp_path):
    root = tmp_path / "copies"
    for day in (27, 26, 25):
        make_copy(root, stamp(2026, 10, day))
    result = sb.prune_plain_copies(root, daily=2, weekly=0, monthly=0)
    assert result["kept"] == [stamp(2026, 10, 27), stamp(2026, 10, 26)]
    assert result["removed"] == [stamp(2026, 10, 25)]
    assert all("newest verified" not in reasons for reasons in result["reasons"].values())


def test_only_plain_copy_folders_are_candidates(tmp_path):
    root = tmp_path / "copies"
    make_copy(root, stamp(2026, 10, 27), verified=VERIFIED)
    make_copy(root, stamp(2026, 10, 1), verified=VERIFIED)                 # the one copy that goes
    other = root / "keep-me"
    other.mkdir()
    (other / "note.txt").write_text("mine", encoding="utf-8")
    (root / "notes.txt").write_text("not a folder", encoding="utf-8")
    (root / "plain-2026-10-01").mkdir()                                     # not the timestamp form
    (root / "plain-20261001T120000Z.bak").mkdir()                           # no dash before the suffix
    (root / "plain-20261332T120000Z").mkdir()                               # not a date
    (root / "plain-20260901T120000Z").write_text("a file, not a copy", encoding="utf-8")
    result = sb.prune_plain_copies(root, daily=1, weekly=0, monthly=0)
    assert result["removed"] == [stamp(2026, 10, 1)]
    assert (other / "note.txt").read_text(encoding="utf-8") == "mine"
    assert listing(root) == sorted([
        stamp(2026, 10, 27), "keep-me", "notes.txt", "plain-2026-10-01", "plain-20261001T120000Z.bak",
        "plain-20261332T120000Z", "plain-20260901T120000Z"])


def test_a_dry_run_deletes_nothing_and_a_real_run_removes_what_it_listed(tmp_path, caplog):
    root = tmp_path / "copies"
    build_history(root)
    before = listing(root)
    with caplog.at_level(logging.INFO, logger="bot.state_backup"):
        preview = sb.prune_plain_copies(root, dry_run=True)
    assert listing(root) == before
    assert preview["dry_run"] is True and preview["removed"] == names("ALMNO")
    assert not [r for r in caplog.records if "removed plain state copy" in r.getMessage()]
    with caplog.at_level(logging.INFO, logger="bot.state_backup"):
        real = sb.prune_plain_copies(root)
    assert real["dry_run"] is False
    assert real["kept"] == preview["kept"] and real["removed"] == preview["removed"]
    logged = [r.getMessage() for r in caplog.records if "removed plain state copy" in r.getMessage()]
    assert len(logged) == len(real["removed"])
    assert all(any(name in message for message in logged) for name in real["removed"])


def test_a_copy_that_cannot_be_removed_is_reported_and_the_rest_still_go(tmp_path, monkeypatch):
    root = tmp_path / "copies"
    make_copy(root, stamp(2026, 10, 27), verified=VERIFIED)
    make_copy(root, stamp(2026, 10, 1), verified=VERIFIED)
    stuck = make_copy(root, stamp(2026, 9, 1), verified=VERIFIED)
    real_rmtree = sb.shutil.rmtree

    def rmtree(path, *args, **kwargs):
        if Path(path) == stuck:
            raise PermissionError("held open by another program")
        return real_rmtree(path, *args, **kwargs)
    monkeypatch.setattr(sb.shutil, "rmtree", rmtree)
    result = sb.prune_plain_copies(root, daily=1, weekly=0, monthly=0)
    assert result["failed"] == [stuck.name] and result["ok"] is False
    assert result["removed"] == [stamp(2026, 10, 1)]
    assert stuck.is_dir()


def test_the_keep_counts_are_whole_numbers_and_at_least_one_daily_copy(tmp_path):
    with pytest.raises(sb.StateBackupError, match="keep_daily"):
        sb.prune_plain_copies(tmp_path, daily=0)
    with pytest.raises(sb.StateBackupError, match="keep_weekly"):
        sb.prune_plain_copies(tmp_path, weekly=-1)
    with pytest.raises(sb.StateBackupError, match="keep_monthly"):
        sb.prune_plain_copies(tmp_path, monthly=True)
    with pytest.raises(sb.StateBackupError, match="keep_monthly"):
        sb.prune_plain_copies(tmp_path, monthly=1000)
    assert sb.prune_plain_copies(tmp_path / "not-made-yet") == {
        "root": str(tmp_path / "not-made-yet"), "dry_run": False, "kept": [], "removed": [], "failed": [],
        "reasons": {}, "ok": True}


def test_a_copy_written_by_plain_copy_is_recorded_as_verified_and_kept(tmp_path):
    root = tmp_path / "codebase"
    data = root / "data"
    data.mkdir(parents=True)
    conn = sqlite3.connect(data / "bot.db")
    conn.execute("CREATE TABLE jobs (id INTEGER PRIMARY KEY, tokens INTEGER)")
    conn.commit()
    conn.close()
    summary = sb.plain_copy(dest_root=tmp_path / "out", data=data, project_root=root, private_dir=None, tier_dirs=[])
    copy = Path(summary["copy"])
    assert json.loads((copy / "manifest.json").read_text(encoding="utf-8"))["verified"]["ok"] is True
    assert sb.prune_plain_copies(tmp_path / "out", daily=1, weekly=0, monthly=0)["kept"] == [copy.name]


def test_the_schedule_stores_the_keep_counts_and_keeps_them_when_left_out(tmp_path, monkeypatch):
    point_at(monkeypatch, tmp_path / "data", tmp_path)
    dest = str(tmp_path / "copies")
    assert sb.set_schedule(enabled=True, dest=dest, keep_daily=5, keep_weekly=2, keep_monthly=3) == {
        "enabled": True, "dest": dest, "keep_daily": 5, "keep_weekly": 2, "keep_monthly": 3}
    assert sb.set_schedule(enabled=True, dest=dest, keep_monthly=9)["keep_daily"] == 5
    assert sb.schedule()["keep_monthly"] == 9
    with pytest.raises(sb.StateBackupError, match="keep_daily"):
        sb.set_schedule(enabled=True, dest=dest, keep_daily=0)
    assert sb.schedule()["keep_daily"] == 5                                 # a refused save changes nothing


def test_the_prune_command_previews_then_removes_and_refuses_c(tmp_path, monkeypatch, capsys):
    point_at(monkeypatch, tmp_path / "data", tmp_path)
    root = tmp_path / "copies"
    for day in (27, 26, 1):
        make_copy(root, stamp(2026, 10, day), verified=VERIFIED)
    sb.set_schedule(enabled=False, dest="", keep_daily=1, keep_weekly=0, keep_monthly=0)
    assert sb.main(["prune", "--dest", str(root), "--dry-run"]) == 0
    preview = json.loads(capsys.readouterr().out)
    assert preview["removed"] == [stamp(2026, 10, 26), stamp(2026, 10, 1)] and preview["dry_run"] is True
    assert len(listing(root)) == 3
    assert sb.main(["prune", "--dest", str(root)]) == 0
    assert json.loads(capsys.readouterr().out)["removed"] == preview["removed"]
    assert listing(root) == [stamp(2026, 10, 27)]
    assert sb.main(["prune", "--dest", C_DEST]) == 1
    assert "never written to C:" in capsys.readouterr().err


def test_the_recovery_api_saves_the_keep_counts_and_refuses_a_bad_one(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from bot.dashboard import fileserver_api

    point_at(monkeypatch, tmp_path / "data", tmp_path)
    monkeypatch.setenv("DASHBOARD_TOKEN", "owner-token")
    app = FastAPI()
    fileserver_api.register(app, lambda: None)
    c = TestClient(app)
    owner = {"X-Dashboard-Token": "owner-token"}
    body = {"enabled": True, "dest": str(tmp_path / "copies"), "keep_daily": 5, "keep_weekly": 2, "keep_monthly": 3}
    assert c.put("/api/fileserver/recovery/schedule", json=body, headers=owner).status_code == 200
    saved = c.get("/api/fileserver/recovery").json()["schedule"]
    assert (saved["keep_daily"], saved["keep_weekly"], saved["keep_monthly"]) == (5, 2, 3)
    assert c.put("/api/fileserver/recovery/schedule", json={**body, "keep_daily": 0}, headers=owner).status_code == 400
    assert c.put("/api/fileserver/recovery/schedule", json={**body, "keep_weekly": "4"}, headers=owner).status_code == 400
