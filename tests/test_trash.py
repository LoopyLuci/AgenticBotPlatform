"""The agent's trash (recovery plan P0, T2; contract C3): removals move files, nothing is unlinked, restore never overwrites,
purge is owner-only and waits for retention."""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from bot.agent_runtime import coding_tools, toolspec, tools, trash
from bot.agent_runtime.errors import ToolError

DAY = 86400


@pytest.fixture(autouse=True)
def _session():
    coding_tools.forget_reads()
    token = toolspec.session_var.set("test-session")
    yield
    toolspec.session_var.reset(token)
    coding_tools.forget_reads()


@pytest.fixture
def ws(tmp_path):
    root = tmp_path / "ws"
    root.mkdir()
    return root.resolve()


@pytest.fixture
def trash_home(tmp_path, monkeypatch):
    home = tmp_path / "trash"
    monkeypatch.setattr(trash, "trash_root", lambda: home)
    return home


def call(ws, name, **inp):
    return asyncio.run(tools.execute_tool(name, inp, workspace=ws))


def put(ws, rel, text, *, read=True):
    p = ws / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(text.encode("utf-8"))
    if read:
        call(ws, "read_file", path=rel)
    return p


def test_trash_file_moves_the_bytes_and_writes_the_sidecar(ws, trash_home):
    src = put(ws, "notes/a.txt", "keep me\n", read=False)
    item = trash.trash_file(ws, src, session="s1", reason="test", root=trash_home)
    assert not src.exists()
    assert (trash_home / item["id"]).read_bytes() == b"keep me\n"
    assert item["sha256"] == trash._sha256(trash_home / item["id"])
    assert item["size"] == 8 and item["retention_days"] == trash.RETENTION_DAYS
    assert Path(item["original"]) == src


def test_patch_delete_goes_to_the_trash_not_the_bin(ws, trash_home):
    put(ws, "gone.txt", "bye\n")
    call(ws, "apply_patch", patch="--- a/gone.txt\n+++ /dev/null\n@@ -1 +0,0 @@\n-bye\n")
    assert not (ws / "gone.txt").exists()
    [item] = trash.items(trash_home)
    assert (trash_home / item["id"]).read_text() == "bye\n"


def test_patch_rename_trashes_the_old_name_and_keeps_the_new_one(ws, trash_home):
    put(ws, "old.txt", "keep\nchange\n")
    call(ws, "apply_patch", patch="--- a/old.txt\n+++ b/new.txt\n@@ -1,2 +1,2 @@\n keep\n-change\n+changed\n")
    assert (ws / "new.txt").read_text() == "keep\nchanged\n" and not (ws / "old.txt").exists()
    [item] = trash.items(trash_home)
    assert Path(item["original"]).name == "old.txt"


def test_restore_puts_the_same_bytes_back_and_clears_the_trash(ws, trash_home):
    src = put(ws, "a.txt", "round trip\n", read=False)
    item = trash.trash_file(ws, src, session="s1", reason="test", root=trash_home)
    trash.restore(item["id"], root=trash_home, workspace=ws)
    assert src.read_text() == "round trip\n"
    assert trash.items(trash_home) == []


def test_restore_refuses_to_overwrite_a_live_path(ws, trash_home):
    src = put(ws, "a.txt", "old\n", read=False)
    item = trash.trash_file(ws, src, session="s1", reason="test", root=trash_home)
    src.write_text("new work\n")
    with pytest.raises(trash.TrashError, match="exists again"):
        trash.restore(item["id"], root=trash_home)
    assert src.read_text() == "new work\n" and (trash_home / item["id"]).exists()


def test_restore_from_an_agent_stays_inside_its_workspace(ws, trash_home, tmp_path):
    src = put(ws, "a.txt", "x\n", read=False)
    item = trash.trash_file(ws, src, session="s1", reason="test", root=trash_home)
    other = tmp_path / "elsewhere"
    other.mkdir()
    with pytest.raises(trash.TrashError, match="outside this workspace"):
        trash.restore(item["id"], root=trash_home, workspace=other)
    assert (trash_home / item["id"]).exists()


def test_purge_is_owner_only_and_waits_for_retention(ws, trash_home):
    src = put(ws, "a.txt", "x\n", read=False)
    item = trash.trash_file(ws, src, session="s1", reason="test", root=trash_home)
    with pytest.raises(trash.TrashError, match="owner"):
        trash.purge(item["id"], owner=False, root=trash_home)
    with pytest.raises(trash.TrashError, match="retention not over"):
        trash.purge(item["id"], owner=True, root=trash_home, now=item["trashed_at"] + DAY)
    trash.purge(item["id"], owner=True, root=trash_home, now=item["trashed_at"] + 31 * DAY)
    assert not (trash_home / item["id"]).exists() and trash.items(trash_home) == []


def test_agent_tools_list_and_restore_only_this_workspace(ws, trash_home, tmp_path):
    put(ws, "mine.txt", "m\n")
    call(ws, "apply_patch", patch="--- a/mine.txt\n+++ /dev/null\n@@ -1 +0,0 @@\n-m\n")
    other = tmp_path / "other"
    other.mkdir()
    theirs = other / "theirs.txt"
    theirs.write_text("t\n")
    trash.trash_file(other, theirs, session="s2", reason="test", root=trash_home)
    listing = call(ws, "trash_list")
    assert "mine.txt" in listing and "theirs.txt" not in listing
    [mine] = trash.items(trash_home, workspace=ws)
    assert "Restored" in call(ws, "trash_restore", id=mine["id"])
    assert (ws / "mine.txt").read_text() == "m\n"
    with pytest.raises(ToolError, match="no trashed item"):
        call(ws, "trash_restore", id="2000-01-01/root/nope.txt")


def test_a_failed_copy_to_the_trash_leaves_the_original_in_place(ws, trash_home, monkeypatch):
    src = put(ws, "a.txt", "precious\n", read=False)
    real_replace = trash.os.replace
    calls = {"n": 0}

    def cross_volume_first(a, b):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("cross-device link")         # forces the copy-verify path
        return real_replace(a, b)

    def corrupt_copy(a, b):
        Path(b).write_bytes(b"garbage")

    monkeypatch.setattr(trash.os, "replace", cross_volume_first)
    monkeypatch.setattr(trash.shutil, "copy2", corrupt_copy)
    with pytest.raises(trash.TrashError, match="did not verify"):
        trash.trash_file(ws, src, session="s1", reason="test", root=trash_home)
    assert src.read_text() == "precious\n"


def test_a_cross_volume_removal_copies_verifies_then_removes_the_source(ws, trash_home, monkeypatch):
    src = put(ws, "a.txt", "moved\n", read=False)
    real_replace = trash.os.replace
    calls = {"n": 0}

    def cross_volume_first(a, b):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("cross-device link")
        return real_replace(a, b)

    monkeypatch.setattr(trash.os, "replace", cross_volume_first)
    item = trash.trash_file(ws, src, session="s1", reason="test", root=trash_home)
    assert not src.exists() and (trash_home / item["id"]).read_text() == "moved\n"


def test_apply_patch_no_longer_unlinks_removed_files():
    import inspect

    assert ".unlink(" not in inspect.getsource(coding_tools._apply_patch)


def test_apply_patch_refuses_to_remove_a_link_and_changes_nothing(ws, trash_home):
    real = put(ws, "real.txt", "data\n")
    link = ws / "link.txt"
    try:
        link.symlink_to(real)
    except OSError:
        pytest.skip("creating a symlink needs privilege on this machine")
    put(ws, "other.txt", "o\n")
    patch = ("--- a/other.txt\n+++ b/other.txt\n@@ -1 +1 @@\n-o\n+O\n"
             "--- a/link.txt\n+++ /dev/null\n@@ -1 +0,0 @@\n-data\n")
    with pytest.raises(ToolError, match="is a link"):
        call(ws, "apply_patch", patch=patch)
    assert (ws / "other.txt").read_text() == "o\n" and link.is_symlink()
    assert trash.items(trash_home) == []


def test_the_recovery_api_is_owner_only_and_refuses_early_purges(ws, trash_home, tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from bot import state_backup
    from bot.dashboard import fileserver_api

    data = tmp_path / "data"
    monkeypatch.setattr(state_backup, "_defaults", lambda: (data, tmp_path, None, []))
    state_backup._write_status(data, {"last_run": {"snapshot": "abc123", "files": 3, "bytes": 9, "excluded": [],
                                                   "unlisted": [], "at": "2026-10-27T00:00:00Z"}})
    monkeypatch.setenv("DASHBOARD_TOKEN", "owner-token")
    src = put(ws, "a.txt", "x\n", read=False)
    item = trash.trash_file(ws, src, session="s1", reason="test", root=trash_home)
    app = FastAPI()
    fileserver_api.register(app, lambda: None)
    c = TestClient(app)
    owner = {"X-Dashboard-Token": "owner-token"}

    view = c.get("/api/fileserver/recovery").json()
    assert [t["id"] for t in view["trash"]] == [item["id"]] and view["state_backup"]["last_run"]["snapshot"] == "abc123"
    assert c.post("/api/fileserver/recovery/trash/restore", json={"id": item["id"]}).status_code == 403
    assert c.post("/api/fileserver/recovery/trash/restore", json={"id": item["id"]}, headers={"X-Dashboard-Token": "x"}).status_code == 403
    assert c.post("/api/fileserver/recovery/trash/purge", json={"id": item["id"]}, headers=owner).status_code == 400
    assert c.post("/api/fileserver/recovery/trash/restore", json={"id": "../../etc/passwd"}, headers=owner).status_code == 400
    assert c.post("/api/fileserver/recovery/trash/restore", json={"id": item["id"]}, headers=owner).status_code == 200
    assert src.read_text() == "x\n" and trash.items(trash_home) == []


def test_a_checkpoint_after_an_apply_patch_delete_brings_the_file_back(ws, trash_home, tmp_path, monkeypatch):
    """The agent loop checkpoints after every dangerous call, so a removal is in the shadow store too: a rollback
    to the checkpoint before the delete restores it. The trash is the per-file route; this is the whole-tree one."""
    from bot.agent_runtime import checkpoints, tool_loop

    monkeypatch.setattr("bot.envfile.PROJECT_ROOT", tmp_path / "shadow_root")
    put(ws, "gone.txt", "bye\n")
    checkpoints.create_checkpoint(ws, "before the delete")
    call(ws, "apply_patch", patch="--- a/gone.txt\n+++ /dev/null\n@@ -1 +0,0 @@\n-bye\n")
    tool_loop.try_checkpoint(ws, "apply_patch", {})
    assert not (ws / "gone.txt").exists()
    checkpoints.rollback(ws, steps=1)
    assert (ws / "gone.txt").read_text() == "bye\n"
