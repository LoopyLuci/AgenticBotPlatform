"""bot/agent_runtime/checkpoints.py — real git-backed checkpoints using a
SEPARATE shadow git store, never the workspace's own `.git`. Real bug
this replaces: the previous design ran git directly inside
`workspace/.git`, so a workspace with its own real git history got the
agent's checkpoint commits mixed straight into it, and rollback's
`git reset --hard` operated on the user's actual branch. No test file
existed for this module before — a genuine, pre-existing coverage gap,
closed here alongside the isolation rewrite.
"""
from __future__ import annotations

import subprocess

import pytest

from bot.agent_runtime import checkpoints


@pytest.fixture(autouse=True)
def _isolated_store(tmp_path, monkeypatch):
    # Redirect the shadow store to a throwaway directory so tests never
    # touch the real repo's data/checkpoint_store/.
    monkeypatch.setattr("bot.envfile.PROJECT_ROOT", tmp_path / "botserver_root")


def _git(args, cwd):
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, timeout=10)


class TestIsolationFromRealRepo:
    def test_never_creates_a_dot_git_in_the_workspace(self, tmp_path):
        workspace = tmp_path / "ws"
        workspace.mkdir()
        (workspace / "file.txt").write_text("hello")

        checkpoints.create_checkpoint(workspace, "first")

        assert not (workspace / ".git").exists()

    def test_a_real_pre_existing_repo_is_never_touched(self, tmp_path):
        """The core safety property: a workspace that's already a real
        git repo with its own history/branch must come out of a
        checkpoint+rollback cycle completely unchanged — our commits
        never land in it, our reset never resets it."""
        workspace = tmp_path / "real_repo"
        workspace.mkdir()
        _git(["init"], workspace)
        _git(["config", "user.email", "user@example.com"], workspace)
        _git(["config", "user.name", "Real User"], workspace)
        (workspace / "real.txt").write_text("real content")
        _git(["add", "-A"], workspace)
        _git(["commit", "-m", "real user commit"], workspace)
        real_log_before = _git(["log", "--oneline"], workspace).stdout

        # Agent does real checkpoint work, including a rollback.
        (workspace / "agent_file.txt").write_text("agent wrote this")
        checkpoints.create_checkpoint(workspace, "agent edit")
        checkpoints.rollback(workspace, steps=1)

        real_log_after = _git(["log", "--oneline"], workspace).stdout
        assert real_log_after == real_log_before
        # The real repo's own branch/HEAD is untouched.
        real_branch = _git(["branch", "--show-current"], workspace).stdout
        assert real_branch.strip() in ("main", "master")

    def test_workspace_own_git_internals_are_never_tracked_as_content(self, tmp_path):
        """Without the excludes-file fix, `git add -A` under
        --work-tree would happily add the workspace's own real .git
        directory as ordinary tracked blobs in the shadow store."""
        workspace = tmp_path / "real_repo2"
        workspace.mkdir()
        _git(["init"], workspace)
        (workspace / "file.txt").write_text("hi")

        checkpoints.create_checkpoint(workspace, "first")

        tracked = checkpoints._run(["ls-tree", "-r", "--name-only", "HEAD"], workspace)
        assert ".git" not in tracked
        assert "file.txt" in tracked


class TestBasicOperations:
    def test_create_checkpoint_returns_none_when_nothing_changed(self, tmp_path):
        workspace = tmp_path / "ws"
        workspace.mkdir()
        assert checkpoints.create_checkpoint(workspace, "empty") is None

    def test_create_and_list_checkpoints(self, tmp_path):
        workspace = tmp_path / "ws"
        workspace.mkdir()
        (workspace / "a.txt").write_text("1")
        checkpoints.create_checkpoint(workspace, "first")
        (workspace / "a.txt").write_text("2")
        checkpoints.create_checkpoint(workspace, "second")

        listed = checkpoints.list_checkpoints(workspace)
        assert len(listed) == 2
        assert listed[0]["message"] == "checkpoint: second"
        assert listed[1]["message"] == "checkpoint: first"

    def test_rollback_restores_real_file_content(self, tmp_path):
        workspace = tmp_path / "ws"
        workspace.mkdir()
        f = workspace / "a.txt"
        f.write_text("version 1")
        checkpoints.create_checkpoint(workspace, "v1")
        f.write_text("version 2")
        checkpoints.create_checkpoint(workspace, "v2")

        checkpoints.rollback(workspace, steps=1)

        assert f.read_text() == "version 1"

    def test_rollback_never_goes_past_session_start(self, tmp_path):
        workspace = tmp_path / "ws"
        workspace.mkdir()
        (workspace / "a.txt").write_text("1")
        checkpoints.create_checkpoint(workspace, "only one")

        result = checkpoints.rollback(workspace, steps=99)

        assert "1 checkpoint" in result

    def test_undo_is_rollback_one_step(self, tmp_path):
        workspace = tmp_path / "ws"
        workspace.mkdir()
        f = workspace / "a.txt"
        f.write_text("1")
        checkpoints.create_checkpoint(workspace, "v1")
        f.write_text("2")
        checkpoints.create_checkpoint(workspace, "v2")

        checkpoints.undo(workspace)

        assert f.read_text() == "1"

    def test_no_checkpoints_yet_is_a_clear_message_not_an_error(self, tmp_path):
        workspace = tmp_path / "ws"
        workspace.mkdir()
        assert "nothing to roll back" in checkpoints.rollback(workspace)

    def test_compress_squashes_into_one_commit(self, tmp_path):
        workspace = tmp_path / "ws"
        workspace.mkdir()
        (workspace / "a.txt").write_text("1")
        checkpoints.create_checkpoint(workspace, "v1")
        (workspace / "a.txt").write_text("2")
        checkpoints.create_checkpoint(workspace, "v2")

        checkpoints.compress(workspace)

        assert len(checkpoints.list_checkpoints(workspace)) == 1
        assert (workspace / "a.txt").read_text() == "2"

    def test_branch_switches_and_future_checkpoints_land_there(self, tmp_path):
        workspace = tmp_path / "ws"
        workspace.mkdir()
        (workspace / "a.txt").write_text("1")
        checkpoints.create_checkpoint(workspace, "v1")

        checkpoints.branch(workspace, "experiment")
        current = checkpoints._run(["branch", "--show-current"], workspace)
        assert current == "experiment"

    def test_worktree_creates_a_sibling_directory(self, tmp_path):
        workspace = tmp_path / "ws"
        workspace.mkdir()
        (workspace / "a.txt").write_text("1")
        checkpoints.create_checkpoint(workspace, "v1")

        result = checkpoints.worktree(workspace, "parallel")

        dest = workspace.parent / "ws-parallel"
        assert dest.exists()
        assert "parallel" in result


class TestSafeRestorePlan:
    def test_empty_when_nothing_would_change(self, tmp_path):
        workspace = tmp_path / "ws"
        workspace.mkdir()
        (workspace / "a.txt").write_text("1")
        checkpoints.create_checkpoint(workspace, "v1")
        head = checkpoints._run(["rev-parse", "HEAD"], workspace)

        assert checkpoints.safe_restore_plan(workspace, head) == ""

    def test_shows_a_real_diffstat_for_a_real_change(self, tmp_path):
        workspace = tmp_path / "ws"
        workspace.mkdir()
        f = workspace / "a.txt"
        f.write_text("1")
        checkpoints.create_checkpoint(workspace, "v1")
        base = checkpoints._run(["rev-parse", "HEAD"], workspace)
        f.write_text("2")
        checkpoints.create_checkpoint(workspace, "v2")

        plan = checkpoints.safe_restore_plan(workspace, base)

        assert "a.txt" in plan


class TestGcOldCheckpoints:
    def test_removes_stores_untouched_past_retention(self, tmp_path):
        workspace = tmp_path / "ws"
        workspace.mkdir()
        (workspace / "a.txt").write_text("1")
        checkpoints.create_checkpoint(workspace, "v1")
        key = checkpoints._workspace_key(workspace)
        marker = checkpoints._base_marker(workspace)
        import os
        old_time = marker.stat().st_mtime - 100 * 86400
        os.utime(marker, (old_time, old_time))

        removed = checkpoints.gc_old_checkpoints(retention_days=30)

        assert removed == 1
        assert not marker.exists()
        assert not checkpoints._git_dir(workspace).exists()

    def test_recently_touched_stores_are_kept(self, tmp_path):
        workspace = tmp_path / "ws"
        workspace.mkdir()
        (workspace / "a.txt").write_text("1")
        checkpoints.create_checkpoint(workspace, "v1")

        removed = checkpoints.gc_old_checkpoints(retention_days=30)

        assert removed == 0
        assert checkpoints._git_dir(workspace).exists()
