"""Screenshots of a page as it is and as a variant has it (a real browser: Edge or Chrome through Playwright), and what
visibly changed between them (bot/vision's compare: similarity, changed share, the changed regions outlined)."""
from __future__ import annotations

import os
import shutil
from typing import Optional

from bot.studio import log, variants


def browser_channel() -> Optional[str]:
    for ch, paths, cmd in (("msedge", [r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
                                       r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"], "microsoft-edge"),
                           ("chrome", [r"C:\Program Files\Google\Chrome\Application\chrome.exe"], "google-chrome")):
        if any(os.path.exists(p) for p in paths) or shutil.which(cmd):
            return ch
    return None


def base_url() -> str:
    return f"http://127.0.0.1:{os.environ.get('DASHBOARD_PORT') or 8787}"


def shoot(urls: dict[str, str], *, width: int = 1400, height: int = 900, wait_ms: int = 1500) -> dict[str, str]:
    """{name: url} -> {name: png path}. Pages are loaded like the dashboard itself (a local page load gets the token)."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as e:
        raise variants.StudioError(f"screenshots need Playwright (requirements-browser.txt): {e}") from None
    ch = browser_channel()
    if not ch:
        raise variants.StudioError("screenshots need Microsoft Edge or Google Chrome")
    from bot.vision import images
    out: dict[str, str] = {}
    with sync_playwright() as pw:
        br = pw.chromium.launch(channel=ch, headless=True)
        try:
            page = br.new_page(viewport={"width": width, "height": height})
            for name, url in urls.items():
                page.goto(url, wait_until="networkidle", timeout=60_000)
                page.wait_for_timeout(wait_ms)
                path = images.out_dir() / f"studio-{name}.png"
                page.screenshot(path=str(path))
                out[name] = str(path)
        finally:
            br.close()
    return out


def compare(vid: str, section: str = "", *, width: int = 1400, height: int = 900) -> dict:
    """The page (at #section) live and in the variant, and what visibly changed."""
    if section and not all(c.isalnum() or c in "-_" for c in section):
        raise variants.StudioError("section: an element id")
    frag = f"#{section}" if section else ""
    shots = shoot({f"{vid}-live": f"{base_url()}/{frag}", f"{vid}-variant": f"{base_url()}/studio/v/{vid}/{frag}"},
                  width=width, height=height)
    from bot.vision import service
    res = service.compare(shots[f"{vid}-live"], shots[f"{vid}-variant"])
    rec = {"variant": vid, "section": section or None, "live": shots[f"{vid}-live"],
           "variant_shot": shots[f"{vid}-variant"], "similarity": res["similarity"],
           "changed_share": res["changed_share"], "regions": res["regions"][:30], "annotated": res["annotated"]}
    log.event("shot", vid=vid, section=section or None, similarity=res["similarity"],
              changed_share=res["changed_share"], regions=len(res["regions"]))
    return rec
