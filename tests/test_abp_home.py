"""ABP_HOME — keep all mutable state (.env, config/, data/, logs/) outside the
checkout, for an ABP that is embedded as a submodule/sidecar of another server.

The old code hardcoded one developer's path (Z:\\Projects\\AgenticBotPlatform)
and preferred it on any machine where it existed, and had no override at all,
so a read-only checkout crashed at import and two hosts shared one database.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from bot import envfile, hotreload

REPO = Path(__file__).resolve().parent.parent


# ------------------------------------------------------------ root resolution
def test_without_abp_home_state_lives_beside_the_code(tmp_path):
    code, state, active = envfile.resolve_roots(tmp_path, {})
    assert code == state == tmp_path and active is False


def test_a_blank_abp_home_is_ignored(tmp_path):
    for blank in ("", "   "):
        code, state, active = envfile.resolve_roots(tmp_path, {"ABP_HOME": blank})
        assert state == code and active is False


def test_abp_home_moves_state_but_not_code(tmp_path):
    home = tmp_path / "state"
    code, state, active = envfile.resolve_roots(tmp_path / "checkout", {"ABP_HOME": str(home)})
    assert code == tmp_path / "checkout"
    assert state == home.resolve() and active is True


def test_abp_home_expands_variables_and_tilde(tmp_path, monkeypatch):
    monkeypatch.setenv("ABP_TEST_BASE", str(tmp_path))
    _c, state, _a = envfile.resolve_roots(tmp_path, {"ABP_HOME": "$ABP_TEST_BASE/state"})
    assert state == (tmp_path / "state").resolve()
    _c, state, _a = envfile.resolve_roots(tmp_path, {"ABP_HOME": "~/abp-home-test"})
    assert state == (Path.home() / "abp-home-test").resolve()


def test_running_from_a_build_output_inside_a_checkout_uses_the_checkout(tmp_path):
    """The developer case the old hardcoded path was for: the built app's
    bundled copy of bot/ must share .env/config/data with the source tree —
    without naming any machine's directory."""
    checkout = tmp_path / "repo"
    (checkout / "bot").mkdir(parents=True)
    (checkout / "bot" / "main.py").write_text("", encoding="utf-8")
    build = checkout / "desktop-app" / "src-tauri" / "target" / "release"
    build.mkdir(parents=True)

    code, state, _active = envfile.resolve_roots(build, {})

    assert code == state == checkout


def test_a_build_output_layout_without_a_real_checkout_is_not_mistaken_for_one(tmp_path):
    build = tmp_path / "desktop-app" / "src-tauri" / "target" / "release"
    build.mkdir(parents=True)  # no bot/main.py three levels up
    code, state, _a = envfile.resolve_roots(build, {})
    assert code == state == build


def test_no_developer_machine_path_is_hardcoded_in_envfile():
    source = (REPO / "bot" / "envfile.py").read_text(encoding="utf-8")
    assert "_CANONICAL_ROOT" not in source
    assert 'Path(r"Z:' not in source


def test_in_this_test_environment_state_is_beside_the_code():
    assert "ABP_HOME" not in os.environ
    assert envfile.PROJECT_ROOT == envfile.CODE_ROOT


# ----------------------------------------------------------- first-run layout
def test_prepare_state_dir_creates_the_layout_and_seeds_the_default_config(tmp_path):
    code = tmp_path / "code"
    (code / "config").mkdir(parents=True)
    (code / "config" / "backends.yaml").write_text("default_backend: cli\n", encoding="utf-8")
    home = tmp_path / "home"

    envfile.prepare_state_dir(home, code)

    for sub in ("config", "data", "logs"):
        assert (home / sub).is_dir()
    assert (home / "config" / "backends.yaml").read_text(encoding="utf-8") == "default_backend: cli\n"


def test_prepare_state_dir_never_overwrites_an_existing_config(tmp_path):
    code = tmp_path / "code"
    (code / "config").mkdir(parents=True)
    (code / "config" / "backends.yaml").write_text("default_backend: cli\n", encoding="utf-8")
    home = tmp_path / "home"
    (home / "config").mkdir(parents=True)
    (home / "config" / "backends.yaml").write_text("default_backend: api\n", encoding="utf-8")

    envfile.prepare_state_dir(home, code)

    assert (home / "config" / "backends.yaml").read_text(encoding="utf-8") == "default_backend: api\n"


def test_an_unusable_abp_home_fails_with_a_message_naming_the_variable(tmp_path):
    blocker = tmp_path / "a-file"
    blocker.write_text("x", encoding="utf-8")
    with pytest.raises(SystemExit, match="ABP_HOME"):
        envfile.prepare_state_dir(blocker / "state", tmp_path)


# ---------------------------------------------------------------- isolation
def test_the_global_claude_env_is_not_consulted_when_abp_home_is_set(monkeypatch):
    monkeypatch.setattr(envfile, "ABP_HOME_ACTIVE", True)
    assert envfile.candidates() == [envfile.PROJECT_ENV]
    monkeypatch.setattr(envfile, "ABP_HOME_ACTIVE", False)
    assert envfile.GLOBAL_ENV in envfile.candidates()


def test_hot_reload_defaults_off_when_embedded_but_config_can_override(monkeypatch):
    monkeypatch.setattr(envfile, "ABP_HOME_ACTIVE", True)
    assert hotreload._enabled_by_default() is False
    monkeypatch.setattr(envfile, "ABP_HOME_ACTIVE", False)
    assert hotreload._enabled_by_default() is True


def test_hot_reload_watches_the_code_tree_not_the_state_tree():
    assert hotreload.BOT_PKG_DIR == envfile.CODE_ROOT / "bot"


# --------------------------------------------- end to end, in a fresh process
def test_a_fresh_process_writes_all_state_under_abp_home_and_nothing_into_the_repo(tmp_path):
    home = tmp_path / "abp-state"
    repo_env = REPO / ".env"
    before = repo_env.stat().st_mtime_ns if repo_env.exists() else None
    env = {k: v for k, v in os.environ.items() if k not in ("DASHBOARD_TOKEN", "ABP_HOME")}
    env.update(ABP_HOME=str(home), PYTHONPATH=str(REPO), PYTHONUTF8="1")

    result = subprocess.run(
        [sys.executable, "-c",
         "import bot.main; from bot import db, envfile, providers, config; db.init_db(); "
         "print(envfile.PROJECT_ROOT); print(db.DB_PATH); print(envfile.CODE_ROOT)"],
        cwd=str(tmp_path), env=env, capture_output=True, text=True, timeout=120,
    )

    assert result.returncode == 0, result.stderr[-1500:]
    project_root, db_path, code_root = result.stdout.strip().splitlines()[-3:]
    assert Path(project_root) == home.resolve()
    assert Path(db_path) == home.resolve() / "data" / "bot.db" and Path(db_path).is_file()
    assert Path(code_root) == REPO
    assert (home / "config" / "backends.yaml").is_file()
    assert "DASHBOARD_TOKEN=" in (home / ".env").read_text(encoding="utf-8")
    assert (home / "logs").is_dir()
    # the repo's own .env (this machine's real secrets) was neither read nor written
    assert (repo_env.stat().st_mtime_ns if repo_env.exists() else None) == before
