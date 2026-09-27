"""The dashboard pages run under a strict, nonce-based Content-Security-Policy:
no inline script or on*= handler may creep back into either UI, and the one
inline script that must exist (the injected token) carries the nonce."""
from __future__ import annotations

import re
from pathlib import Path

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
PAGES = [ROOT / "bot/dashboard/static/dashboard.html", ROOT / "desktop-app/ui/index.html"]


def test_pages_have_no_inline_script_or_handlers():
    for page in PAGES:
        html = page.read_text(encoding="utf-8")
        inline = re.findall(r"<script(?![^>]*\bsrc=)[^>]*>", html)
        assert inline == [], f"{page.name}: inline <script> blocks are blocked by the CSP; move them to a .js file"
        assert not re.search(r"\son[a-z]+\s*=\s*[\"']", html), f"{page.name}: on*= attributes are blocked by the CSP"
        assert "javascript:" not in html


def test_the_prelude_is_shared_and_loaded_first():
    a = (ROOT / "bot/dashboard/static/ui-prelude.js").read_text(encoding="utf-8")
    b = (ROOT / "desktop-app/ui/ui-prelude.js").read_text(encoding="utf-8")
    assert a == b
    for page in PAGES:
        html = page.read_text(encoding="utf-8")
        assert html.index("ui-prelude.js") < html.index("</head>")


def test_served_pages_carry_a_fresh_nonce_that_matches_the_token_script(temp_db, monkeypatch):
    from bot.dashboard.server import build_app

    monkeypatch.setenv("DASHBOARD_TOKEN", "t" * 48)
    client = TestClient(build_app(), base_url="http://127.0.0.1")
    nonces = []
    for _ in range(2):
        r = client.get("/")
        csp = r.headers["content-security-policy"]
        nonce = re.search(r"'nonce-([^']+)'", csp).group(1)
        nonces.append(nonce)
        assert "script-src 'self' 'nonce-" in csp and "'unsafe-inline'" not in csp.split("script-src", 1)[1].split(";")[0]
        assert "object-src 'none'" in csp and "frame-ancestors 'self'" in csp
        if "__ABP_TOKEN__" in r.text:
            assert f'<script nonce="{nonce}">window.__ABP_TOKEN__=' in r.text
    assert nonces[0] != nonces[1]


def test_the_dashboard_script_guards_the_network():
    for js in (ROOT / "bot/dashboard/static/dashboard.js", ROOT / "desktop-app/ui/main.js"):
        text = js.read_text(encoding="utf-8")
        assert "_timedFetch(" in text and "API_TIMEOUT_MS" in text
        assert "Date.now() < _net.until" in text  # pollers wait out the backoff


def test_pollers_only_run_for_sections_on_screen():
    """The page is one long scroll; a poller feeding one section must not run while that section is off screen
    (measured: 58 requests per 10 s before, 13 after, with the page at the top)."""
    import re as _re

    for js in (ROOT / "bot/dashboard/static/dashboard.js", ROOT / "desktop-app/ui/main.js"):
        text = js.read_text(encoding="utf-8")
        assert "function pollWhenVisible(fn, ms, ...sections)" in text
        assert "new IntersectionObserver(" in text
        assert "pollWhenVisible(refreshAll, 5000)" not in text  # split into per-section pollers
        gated = _re.findall(r"pollWhenVisible\([^;]*?, \d+, '", text)
        assert len(gated) >= 25, f"{js.name}: only {len(gated)} section-gated pollers"
        # the top-bar status pills keep polling from anywhere on the page
        assert "pollWhenVisible(refreshOverview, 5000);" in text
