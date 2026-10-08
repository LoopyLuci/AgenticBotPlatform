"""With a private overlay (<ABP_PRIVATE_DIR>/config/backends.yaml), a setting saved through ABP goes into the
overlay: the overlay wins the merge, so an edit under it would never take effect, and the machine's own settings
must not land in the public checkout's tracked file."""
from __future__ import annotations

import yaml

from bot import config_backups as cb
from bot.config import ConfigManager


def _setup(tmp_path, monkeypatch, overlay_text: str | None):
    tracked = tmp_path / "repo" / "config" / "backends.yaml"
    tracked.parent.mkdir(parents=True)
    tracked.write_text("default_backend: api\nmodules:\n  build_cache: null\n", encoding="utf-8")
    private = tmp_path / "private"
    (private / "config").mkdir(parents=True)
    if overlay_text is not None:
        (private / "config" / "backends.yaml").write_text(overlay_text, encoding="utf-8")
    monkeypatch.setenv("ABP_PRIVATE_DIR", str(private))
    monkeypatch.setattr(cb, "before_write", lambda *a, **k: None)
    return tracked, private / "config" / "backends.yaml", ConfigManager(path=tracked)


def test_a_save_goes_to_the_overlay_and_takes_effect(tmp_path, monkeypatch):
    tracked, overlay, mgr = _setup(tmp_path, monkeypatch, "modules:\n  build_cache: /path/redacted\n")
    before = tracked.read_text(encoding="utf-8")
    mgr.set_values({("modules", "build_cache"): "Y:/elsewhere", ("power", "wake"): {"Server": {"mac": "x"}}})
    assert tracked.read_text(encoding="utf-8") == before            # the public file is untouched
    saved = yaml.safe_load(overlay.read_text(encoding="utf-8"))
    assert saved["modules"]["build_cache"] == "Y:/elsewhere"
    assert saved["power"]["wake"]["Server"]["mac"] == "x"
    assert mgr.current["modules"]["build_cache"] == "Y:/elsewhere"  # and it is what ABP now runs with
    assert mgr.current["default_backend"] == "api"


def test_without_an_overlay_a_save_goes_to_the_tracked_file(tmp_path, monkeypatch):
    tracked, overlay, mgr = _setup(tmp_path, monkeypatch, None)
    mgr.set_values({("default_backend",): "cli"})
    assert not overlay.exists()
    assert yaml.safe_load(tracked.read_text(encoding="utf-8"))["default_backend"] == "cli"


def test_the_overlay_has_its_own_history_name(tmp_path, monkeypatch):
    tracked, overlay, mgr = _setup(tmp_path, monkeypatch, "modules: {}\n")
    from bot import config as config_mod

    monkeypatch.setattr(config_mod, "CONFIG_PATH", tracked)
    monkeypatch.setattr(config_mod, "config", mgr)
    monkeypatch.setattr(cb, "CONFIG_DIR", tracked.parent)
    names = cb._tracked()
    assert names["private-backends.yaml"] == overlay and names["backends.yaml"] == tracked
    assert cb._history_name(overlay) == "private-backends.yaml"
    assert cb._history_name(tracked) == "backends.yaml"
    assert cb._history_name(tmp_path / "elsewhere.yaml") is None
