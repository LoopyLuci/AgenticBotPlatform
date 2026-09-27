"""The Python server, the desktop app (tauri.conf.json + Cargo.toml) and the
Android app ship as one release and must carry the same version, or crash
reports, update checks and the Android self-update compare the wrong numbers.
scripts/publish_release.py bumps all of them together."""
from __future__ import annotations

import json
import re
from pathlib import Path

import bot

ROOT = Path(__file__).resolve().parent.parent


def test_every_component_carries_the_same_version():
    tauri = json.loads((ROOT / "desktop-app/src-tauri/tauri.conf.json").read_text(encoding="utf-8"))["version"]
    cargo = re.search(r'(?m)^version = "([^"]+)"', (ROOT / "desktop-app/src-tauri/Cargo.toml").read_text(encoding="utf-8")).group(1)
    gradle = re.search(r'versionName = "([^"]+)"', (ROOT / "android-app/app/build.gradle.kts").read_text(encoding="utf-8")).group(1)
    assert bot.__version__ == tauri == cargo == gradle
