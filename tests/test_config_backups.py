"""Config history (bot/config_backups.py): a copy of a config file before every write, kept once per content,
shown with secrets masked, and restorable (keeping the version it replaces)."""
from __future__ import annotations

import pathlib

import pytest
from fastapi.testclient import TestClient

from bot import config_backups as cb

TOKEN = "unused-dashboard-token"
ROOT = pathlib.Path(__file__).resolve().parent.parent


@pytest.fixture
def live(tmp_path, monkeypatch):
    """A tracked config file in a temp folder, with the history root beside it."""
    cfg = tmp_path / "config" / "backends.yaml"
    cfg.parent.mkdir()
    cfg.write_text("default_backend: api\nproviders:\n  x:\n    api_key: first-value\n", encoding="utf-8")
    hist = tmp_path / "history"
    monkeypatch.setattr(cb, "_tracked", lambda: {"backends.yaml": cfg})
    monkeypatch.setattr(cb, "ROOT", hist)
    monkeypatch.setattr(cb, "CONFIG_DIR", cfg.parent)
    reloads = []
    monkeypatch.setattr(cb, "_reload", lambda name, actor: (reloads.append(name) or (True, "reloaded")))
    return cfg, hist, reloads


def test_a_copy_is_kept_before_each_write_and_once_per_content(live):
    cfg, hist, _ = live
    assert cb.before_write(cfg, "dashboard")["file"] == "backends.yaml"
    assert cb.before_write(cfg, "dashboard") is None          # same content: nothing new
    cfg.write_text("default_backend: cli\n", encoding="utf-8")
    assert cb.before_write(cfg, "agent:3") is not None
    versions = cb.list_versions("backends.yaml")
    assert [v["actor"] for v in versions] == ["agent-3", "dashboard"]
    assert versions[0]["reason"] == "before a change"


def test_only_the_newest_versions_are_kept(live, monkeypatch):
    cfg, _, _ = live
    monkeypatch.setattr(cb, "KEEP", 3)
    for i in range(6):
        cfg.write_text(f"n: {i}\n", encoding="utf-8")
        cb.before_write(cfg, "t")
    assert len(cb.list_versions("backends.yaml")) == 3


def test_a_file_abp_does_not_own_is_never_kept(tmp_path, monkeypatch):
    monkeypatch.setattr(cb, "ROOT", tmp_path / "history")
    stray = tmp_path / "other.yaml"
    stray.write_text("a: 1\n", encoding="utf-8")
    assert cb.before_write(stray, "t") is None
    # a tracked writer pointed at a temporary file (as tests do) is not kept either
    monkeypatch.setattr(cb, "_tracked", lambda: {"providers.yaml": stray})
    assert cb.before_write(stray, "t") is None
    assert not (tmp_path / "history").exists()


def test_secrets_are_masked_when_shown(live):
    cfg, _, _ = live
    cb.before_write(cfg, "dashboard")
    v = cb.list_versions("backends.yaml")[0]["version"]
    text = cb.read_masked("backends.yaml", v)
    assert "first-value" not in text and "<hidden>" in text
    cfg.write_text("default_backend: api\nproviders:\n  x:\n    api_key: second-value\n", encoding="utf-8")
    diff = cb.list_versions("backends.yaml")[0]["restoring_changes"]
    assert "api_key: changed" in diff and "value" not in diff


def test_restore_puts_a_version_back_keeps_the_current_one_and_reloads(live):
    cfg, _, reloads = live
    cb.before_write(cfg, "dashboard")
    old = cb.list_versions("backends.yaml")[0]["version"]
    cfg.write_text("default_backend: broken\n", encoding="utf-8")
    result = cb.restore("backends.yaml", old, actor="dashboard")
    assert result["restored"] == old and reloads == ["backends.yaml"]
    assert "api_key: first-value" in cfg.read_text(encoding="utf-8")
    kept = cb.list_versions("backends.yaml")[0]
    assert kept["reason"] == f"before restoring {old}"
    assert "broken" in cb.read_masked("backends.yaml", kept["version"])   # the restore can be undone


def test_names_are_checked(live):
    with pytest.raises(ValueError):
        cb.list_versions("../secrets.yaml")
    with pytest.raises(ValueError):
        cb.read_masked("backends.yaml", "../../.env")
    with pytest.raises(FileNotFoundError):
        cb.read_masked("backends.yaml", "20260101T000000000Z__x.yaml")


def test_the_api_lists_shows_and_restores(live, monkeypatch):
    from bot.dashboard.server import build_app

    cfg, _, _ = live
    cb.before_write(cfg, "dashboard")
    monkeypatch.setenv("DASHBOARD_TOKEN", TOKEN)
    client = TestClient(build_app(), base_url="http://127.0.0.1:8787", client=("127.0.0.1", 50000))
    h = {"X-Dashboard-Token": TOKEN}
    assert client.get("/api/config/backups").status_code == 401
    files = client.get("/api/config/backups", headers=h).json()["files"]
    assert files == [{"file": "backends.yaml", "exists": True, "versions": 1}]
    version = client.get("/api/config/backups/backends.yaml", headers=h).json()["versions"][0]["version"]
    shown = client.get(f"/api/config/backups/backends.yaml/{version}", headers=h).json()["text"]
    assert "first-value" not in shown
    assert client.post(f"/api/config/backups/backends.yaml/{version}/restore", headers=h).json()["restored"] == version
    assert client.get("/api/config/backups/backends.yaml/not-a-version", headers=h).status_code == 400


@pytest.mark.parametrize("html, js", [("bot/dashboard/static/dashboard.html", "bot/dashboard/static/dashboard.js"),
                                      ("desktop-app/ui/index.html", "desktop-app/ui/main.js")])
def test_both_uis_have_the_config_history_card(html, js):
    assert 'id="config-backups-card"' in (ROOT / html).read_text(encoding="utf-8")
    script = (ROOT / js).read_text(encoding="utf-8")
    assert "async function refreshConfigBackups()" in script and "pollWhenVisible(refreshConfigBackups" in script
