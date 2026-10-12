"""Removing an installed skill pack: skill_install.remove_installed, DELETE /api/skills/packs/{name}, /skills remove."""
from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from bot import bot_instances, commands, db, skill_install, skill_packs
from bot.agent_runtime.errors import ToolError
from bot.dashboard.server import build_app

H = {"X-Dashboard-Token": "test-token"}


@pytest.fixture
def root(tmp_path, monkeypatch):
    r = tmp_path / "skill_packs"
    r.mkdir()
    monkeypatch.setattr(skill_packs, "user_root", lambda: r)
    return r


def make(root, name):
    d = root / name
    d.mkdir()
    (d / "SKILL.md").write_text(f"---\nname: {name}\ndescription: Use for {name} things.\n---\nBody\n", encoding="utf-8")
    (d / "scripts").mkdir()
    (d / "scripts" / "run.py").write_text("print(1)", encoding="utf-8")
    return d


def test_remove_deletes_only_that_pack(root):
    make(root, "keep-me")
    gone = make(root, "drop-me")
    assert skill_install.remove_installed("drop-me") == {"removed": "drop-me"}
    assert not gone.exists() and (root / "keep-me" / "SKILL.md").is_file()
    assert set(skill_packs.discover(None)) == {"keep-me"}


@pytest.mark.parametrize("name", ["../outside", "..", "a/b", "a\b", "", "UPPER", "nothere", "x" * 80, ".git"])
def test_bad_or_unknown_names_are_refused_and_nothing_outside_goes(root, tmp_path, name):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "f.txt").write_text("precious")
    make(root, "real")
    with pytest.raises(ToolError):
        skill_install.remove_installed(name)
    assert (outside / "f.txt").read_text() == "precious" and (root / "real").is_dir()


def test_a_symlinked_pack_is_not_followed(root, tmp_path):
    target = tmp_path / "elsewhere"
    target.mkdir()
    (target / "f.txt").write_text("precious")
    try:
        (root / "linked").symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("cannot create symlinks here")
    with pytest.raises(ToolError):
        skill_install.remove_installed("linked")
    assert (target / "f.txt").read_text() == "precious"


def test_route_removes_audits_and_needs_the_token(temp_db, root, monkeypatch):
    monkeypatch.setenv("DASHBOARD_TOKEN", "test-token")
    client = TestClient(build_app())
    make(root, "doomed")
    assert client.delete("/api/skills/packs/doomed").status_code == 401
    assert (root / "doomed").is_dir()
    r = client.delete("/api/skills/packs/doomed", headers=H)
    assert r.status_code == 200 and r.json() == {"removed": "doomed"} and not (root / "doomed").exists()
    assert client.delete("/api/skills/packs/doomed", headers=H).status_code == 400
    assert client.delete("/api/skills/packs/..%2Fescape", headers=H).status_code in (400, 404)
    assert any(a["action"] == "skill_pack_remove" and a["detail"] == "doomed" for a in db.list_audit_log(limit=20))


class Ctx:
    def __init__(self, iid, user_id=1):
        self.instance_id, self.user_id, self.chat_id, self.thread_id = iid, user_id, 1, None
        self.instance_name, self.actor, self.session, self.notify_approval = "t", "t", {}, None


def test_slash_remove_falls_through_to_installed_packs_for_admins_only(temp_db, root):
    iid = bot_instances.create_instance(name="a", platform="telegram", backend="api",
                                        credentials={"bot_token": "123456789:AAExampleTokenFromBotFather1234"},
                                        allowed_user_ids=[1, 2], admin_user_ids=[1])
    make(root, "doomed")
    assert "Only this bot's admins" in asyncio.run(commands.cmd_skills(Ctx(iid, 2), ["remove", "doomed"]))
    assert (root / "doomed").is_dir()
    assert "Removed" in asyncio.run(commands.cmd_skills(Ctx(iid, 1), ["remove", "doomed"]))
    assert not (root / "doomed").exists()
