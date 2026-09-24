"""Writes browser-extension/src/shared/sensitive-hosts.json from bot/browser_policy.py (the single source of truth), so the
extension ships the same sensitive-site table the server enforces. tests/test_browser_bridge.py fails if the file is stale.
Run: python scripts/gen_extension_policy.py"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from bot import browser_policy  # noqa: E402

TARGET = ROOT / "browser-extension" / "src" / "shared" / "sensitive-hosts.json"


def render() -> str:
    return json.dumps({"sensitive_hosts": {k: list(v) for k, v in browser_policy.SENSITIVE_HOSTS.items()}}, indent=2) + "\n"


if __name__ == "__main__":
    TARGET.write_text(render(), encoding="utf-8")
    print("wrote", TARGET)
