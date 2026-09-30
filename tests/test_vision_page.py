"""The Vision page: one panel file, identical in the dashboard and the desktop app, and both pages wire it in."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def test_the_panel_is_the_same_file_in_both_uis_and_both_pages_have_it():
    a = (ROOT / "bot/dashboard/static/vision-panel.js").read_text(encoding="utf-8")
    b = (ROOT / "desktop-app/ui/vision-panel.js").read_text(encoding="utf-8")
    assert a == b, "desktop-app/ui/vision-panel.js differs from bot/dashboard/static/vision-panel.js: copy it over"
    for page in ("bot/dashboard/static/dashboard.html", "desktop-app/ui/index.html"):
        html = (ROOT / page).read_text(encoding="utf-8")
        assert 'id="vision"' in html and 'id="vsp-root"' in html and "vision-panel.js" in html and 'href="#vision"' in html
    for route in ("/api/vision/analyze", "/api/vision/find", "/api/vision/compare", "/api/vision/edit", "/api/vision"):
        assert route in a


@pytest.mark.skipif(not shutil.which("node"), reason="node is needed to check the script's syntax")
def test_the_panel_parses():
    r = subprocess.run(["node", "--check", str(ROOT / "bot/dashboard/static/vision-panel.js")], capture_output=True,
                       text=True, timeout=60)
    assert r.returncode == 0, r.stderr
