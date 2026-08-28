"""bot/config.py's ConfigManager — the hot-reload path every part of
BotServer trusts to never hand back a broken config. Covers the one real
gap found while hardening it for live development: a YAML file that
parses fine (so the existing try/except around yaml.safe_load never
fires) but has the wrong root shape (a list instead of a mapping) used
to get swapped straight into `_data`, ready to raise deep inside whatever
code path first calls `.get()` on it.
"""

from __future__ import annotations

from bot.config import ConfigManager


def test_reload_rejects_non_mapping_root(tmp_path):
    path = tmp_path / "backends.yaml"
    path.write_text("default_backend: cli\n", encoding="utf-8")
    manager = ConfigManager(path=path)
    assert manager.current["default_backend"] == "cli"

    path.write_text("- this\n- is\n- a list\n", encoding="utf-8")
    changed, summary = manager.reload()

    assert changed is False
    assert "mapping" in summary
    # The bad edit never took effect — readers still see the last good config.
    assert manager.current["default_backend"] == "cli"


def test_reload_rejects_scalar_root(tmp_path):
    path = tmp_path / "backends.yaml"
    path.write_text("default_backend: cli\n", encoding="utf-8")
    manager = ConfigManager(path=path)

    path.write_text("just a plain string\n", encoding="utf-8")
    changed, summary = manager.reload()

    assert changed is False
    assert manager.current["default_backend"] == "cli"


def test_reload_still_accepts_a_good_edit(tmp_path):
    path = tmp_path / "backends.yaml"
    path.write_text("default_backend: cli\n", encoding="utf-8")
    manager = ConfigManager(path=path)

    path.write_text("default_backend: api\n", encoding="utf-8")
    changed, summary = manager.reload()

    assert changed is True
    assert manager.current["default_backend"] == "api"


def test_reload_still_rejects_invalid_yaml_syntax(tmp_path):
    path = tmp_path / "backends.yaml"
    path.write_text("default_backend: cli\n", encoding="utf-8")
    manager = ConfigManager(path=path)

    path.write_text("default_backend: [unclosed\n", encoding="utf-8")
    changed, summary = manager.reload()

    assert changed is False
    assert manager.current["default_backend"] == "cli"


def test_read_raw_bypasses_cache(tmp_path):
    path = tmp_path / "backends.yaml"
    path.write_text("default_backend: cli\n", encoding="utf-8")
    manager = ConfigManager(path=path)

    # Edit the file without going through reload()/set_value() — read_raw()
    # must still see it, unlike .current (the cached in-memory copy).
    path.write_text("default_backend: api\n", encoding="utf-8")
    assert manager.current["default_backend"] == "cli"
    assert manager.read_raw()["default_backend"] == "api"
