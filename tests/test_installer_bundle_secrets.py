"""The installer must never ship per-user, secret-bearing files.

tauri.conf.json used to bundle the whole `../../config` folder, and the
gitignored config/providers.yaml (which holds the developer's own provider
API keys) sat in it — so every published installer carried those keys to
every machine, and the repo is public. Only tracked, keyless defaults may
be bundled, and a fresh install without providers.yaml must still start.
"""
from __future__ import annotations

import json
import subprocess

from bot.config import ConfigManager
from bot.envfile import PROJECT_ROOT

TAURI_CONF = PROJECT_ROOT / "desktop-app" / "src-tauri" / "tauri.conf.json"


def _resources() -> dict[str, str]:
    conf = json.loads(TAURI_CONF.read_text(encoding="utf-8"))
    return conf["bundle"]["resources"]


def test_installer_does_not_bundle_the_whole_config_directory():
    sources = list(_resources())
    assert "../../config" not in sources
    assert not any(s.rstrip("/").endswith("config") for s in sources)


def test_installer_does_not_bundle_env_or_local_secret_files():
    for source in _resources():
        name = source.replace("\\", "/").rsplit("/", 1)[-1].lower()
        assert name not in {"providers.yaml", "file_share.yaml", ".env"}, source


def test_every_bundled_config_file_is_tracked_by_git():
    """Anything bundled from config/ must be a committed default — an
    untracked file there is by definition local/per-user."""
    for source in _resources():
        normalized = source.replace("\\", "/")
        if "/config/" not in normalized:
            continue
        rel = normalized.split("../../", 1)[-1]
        tracked = subprocess.run(
            ["git", "ls-files", "--error-unmatch", rel],
            cwd=PROJECT_ROOT, capture_output=True, text=True,
        )
        assert tracked.returncode == 0, f"{rel} is bundled but not tracked by git"


def test_missing_optional_config_loads_as_empty(tmp_path):
    manager = ConfigManager(path=tmp_path / "providers.yaml", missing_ok=True)
    assert manager.current == {}


def test_missing_required_config_still_fails_loudly(tmp_path):
    try:
        ConfigManager(path=tmp_path / "backends.yaml")
    except FileNotFoundError:
        return
    raise AssertionError("a missing required config file must not silently load as empty")
