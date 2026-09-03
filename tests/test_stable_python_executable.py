"""bot.envfile.stable_python_executable() — the fix for a real deploy
failure: a Hermes/Claude-Desktop-registered MCP server subprocess spawned
from sys.executable (the Tauri-bundled venv in production) held that
venv's DLL files open long enough to make `cargo tauri build` fail with
a Windows file-in-use error. This should always prefer the project's own
top-level .venv, which is never a build target, and only fall back to
sys.executable when that venv genuinely doesn't exist.
"""

from __future__ import annotations

import sys
from pathlib import Path

from bot import envfile


def test_prefers_project_venv_when_it_exists(tmp_path, monkeypatch):
    monkeypatch.setattr(envfile, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(sys, "platform", "win32")
    venv_python = tmp_path / ".venv" / "Scripts" / "python.exe"
    venv_python.parent.mkdir(parents=True)
    venv_python.write_text("", encoding="utf-8")

    result = envfile.stable_python_executable()

    assert result == str(venv_python)


def test_falls_back_to_sys_executable_when_no_project_venv(tmp_path, monkeypatch):
    monkeypatch.setattr(envfile, "PROJECT_ROOT", tmp_path)  # no .venv under here

    result = envfile.stable_python_executable()

    assert result == sys.executable


def test_uses_unix_layout_on_non_windows(tmp_path, monkeypatch):
    monkeypatch.setattr(envfile, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(sys, "platform", "linux")
    venv_python = tmp_path / ".venv" / "bin" / "python"
    venv_python.parent.mkdir(parents=True)
    venv_python.write_text("", encoding="utf-8")

    result = envfile.stable_python_executable()

    assert result == str(venv_python)


def test_never_returns_a_bundled_release_path(tmp_path, monkeypatch):
    # The whole point: never resolve to a path cargo tauri build would
    # need to overwrite. Simulate PROJECT_ROOT pointing at a real repo
    # layout with a bundled build dir but no top-level .venv, and confirm
    # the result is sys.executable (this test's own interpreter), not
    # anything containing "target/release".
    monkeypatch.setattr(envfile, "PROJECT_ROOT", tmp_path)
    (tmp_path / "desktop-app" / "src-tauri" / "target" / "release" / ".venv" / "Scripts").mkdir(parents=True)
    bundled = tmp_path / "desktop-app" / "src-tauri" / "target" / "release" / ".venv" / "Scripts" / "python.exe"
    bundled.write_text("", encoding="utf-8")

    result = envfile.stable_python_executable()

    assert "target" not in result
    assert "release" not in result
