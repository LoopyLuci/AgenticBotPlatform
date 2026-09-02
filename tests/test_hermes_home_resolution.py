"""bot.hermes_config._default_hermes_home() — where Hermes's own
config.yaml actually lives when no per-instance hermes_home override is
given. Found via a real production mistake: an earlier version
hardcoded ~/.hermes/config.yaml unconditionally, which is Hermes's *nix
default but NOT its Windows default (%LOCALAPPDATA%\\hermes) — a write
against a real Windows machine silently landed in a file the actual,
already-running Hermes process never reads. These lock in the fix
against Hermes's own real hermes_constants.py resolution order:
HERMES_HOME env var, then the platform-native default.
"""

from __future__ import annotations

from pathlib import Path

from bot import hermes_config


def test_prefers_hermes_home_env_var_over_any_platform_default(monkeypatch):
    monkeypatch.setenv("HERMES_HOME", "/custom/hermes/home")
    assert hermes_config._default_hermes_home() == Path("/custom/hermes/home")


def test_windows_default_is_localappdata_hermes_not_dot_hermes(monkeypatch):
    monkeypatch.delenv("HERMES_HOME", raising=False)
    monkeypatch.setattr(hermes_config.sys, "platform", "win32")
    monkeypatch.setenv("LOCALAPPDATA", r"C:\Users\test\AppData\Local")

    home = hermes_config._default_hermes_home()

    assert home == Path(r"C:\Users\test\AppData\Local\hermes")
    assert ".hermes" not in str(home)


def test_windows_falls_back_to_home_appdata_local_when_localappdata_unset(monkeypatch):
    monkeypatch.delenv("HERMES_HOME", raising=False)
    monkeypatch.setattr(hermes_config.sys, "platform", "win32")
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    monkeypatch.setattr(Path, "home", lambda: Path("/home/test"))

    home = hermes_config._default_hermes_home()

    assert home == Path("/home/test/AppData/Local/hermes")


def test_non_windows_default_is_dot_hermes(monkeypatch):
    monkeypatch.delenv("HERMES_HOME", raising=False)
    monkeypatch.setattr(hermes_config.sys, "platform", "linux")
    monkeypatch.setattr(Path, "home", lambda: Path("/home/test"))

    home = hermes_config._default_hermes_home()

    assert home == Path("/home/test/.hermes")
