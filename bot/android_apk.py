"""Where the last-built Android debug APK lives, and how to label it —
shared by bot/dashboard/server.py's android-apk-push routes and
bot/support_bot/actions.py's "update the app" intent, so both paths mean
exactly the same file when they say "the latest APK" rather than two
independently-maintained path constants drifting apart.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from bot.envfile import PROJECT_ROOT


def latest_apk_path() -> Path:
    return PROJECT_ROOT / "android-app" / "app" / "build" / "outputs" / "apk" / "debug" / "app-debug.apk"


def apk_version_label(path: Path) -> str:
    return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).isoformat()
