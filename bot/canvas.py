"""A canvas: a small live page the agent draws and updates while it works (roadmap P7).

The agent calls `canvas_update` with a name and a piece of HTML (a table of results, a chart drawn in SVG, a checklist, a
form-less dashboard). A person opens `/canvas/<name>/view` and watches it change as the agent updates it. Each update is a new
version; the viewer polls for the version number and reloads the frame when it changes.

The HTML is written by a model that may have read hostile text, so it is never given the dashboard's privileges. It is
served from its own URL with a Content-Security-Policy of `sandbox allow-scripts` (an opaque origin: no cookies, no
storage, no dashboard token, no way to call the dashboard's API), `connect-src 'none'` (no network requests from the page)
and `form-action 'none'`; the viewer embeds it in an `<iframe sandbox="allow-scripts">`. Scripts may run, so a chart can be
animated, but they can go nowhere and read nothing. Links inside the page cannot navigate the viewer away.

Access: the viewer page needs the dashboard token; the frame's address carries a short-lived HMAC signature so an iframe
(which cannot send headers) can load it without putting the token in a URL. Canvases live in the agent state folder, at most
`canvas.max_bytes` (500 000) each, at most 50 of them.

Nothing here is shown in the desktop or Android apps yet: it is a web page you open in a browser.
"""
from __future__ import annotations

import hashlib
import hmac
import html
import json
import os
import re
import secrets
import time
from pathlib import Path
from typing import Optional

from bot.agent_runtime.state import state_dir

MAX_BYTES = 500_000
MAX_CANVASES = 50
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,47}$")
SIG_TTL_S = 900
CSP = ("sandbox allow-scripts; default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data: blob:; "
       "font-src data:; connect-src 'none'; form-action 'none'; frame-src 'none'; base-uri 'none'")


class CanvasError(Exception):
    pass


def _dir() -> Path:
    return state_dir("canvas")


def _secret() -> bytes:
    """A per-install secret for signing frame addresses, kept beside the canvases."""
    path = _dir() / ".secret"
    if not path.exists():
        path.write_bytes(secrets.token_bytes(32))
        try:
            path.chmod(0o600)
        except OSError:
            pass
    return path.read_bytes()


def _check_name(name: str) -> str:
    name = (name or "").strip().lower()
    if not NAME_RE.match(name):
        raise CanvasError("a canvas name is lowercase letters, digits, - and _ (at most 48)")
    return name


def _paths(name: str) -> tuple[Path, Path]:
    return _dir() / f"{name}.html", _dir() / f"{name}.json"


def update(name: str, content: str, *, title: str = "") -> dict:
    name = _check_name(name)
    content = content or ""
    if not content.strip():
        raise CanvasError("the canvas content is empty")
    if len(content.encode("utf-8")) > int(_cfg().get("max_bytes", MAX_BYTES)):
        raise CanvasError(f"a canvas is limited to {int(_cfg().get('max_bytes', MAX_BYTES)):,} bytes")
    page, meta = _paths(name)
    if not page.exists() and len(list(_dir().glob("*.html"))) >= MAX_CANVASES:
        raise CanvasError(f"at most {MAX_CANVASES} canvases; remove one first")
    before = json.loads(meta.read_text(encoding="utf-8")) if meta.exists() else {}
    version = before.get("version", 0) + 1
    from bot.agent_runtime import secrets_guard

    page.write_text(secrets_guard.redact(content), encoding="utf-8")            # a secret the agent pasted in is removed, not shown
    meta.write_text(json.dumps({"version": version, "title": (title or before.get("title") or name)[:80], "updated": time.time()}), encoding="utf-8")
    return {"name": name, "version": version}


def _cfg() -> dict:
    try:
        from bot.config import config

        return (config.current.get("canvas")) or {}
    except Exception:  # noqa: BLE001
        return {}


def info(name: str) -> Optional[dict]:
    name = _check_name(name)
    page, meta = _paths(name)
    if not page.exists() or not meta.exists():
        return None
    return {"name": name, **json.loads(meta.read_text(encoding="utf-8"))}


def listing() -> list[dict]:
    out = [info(p.stem) for p in sorted(_dir().glob("*.html")) if NAME_RE.match(p.stem)]
    return [i for i in out if i]


def read(name: str) -> str:
    page, _ = _paths(_check_name(name))
    if not page.exists():
        raise CanvasError("no such canvas")
    return page.read_text(encoding="utf-8")


def remove(name: str) -> bool:
    page, meta = _paths(_check_name(name))
    existed = page.exists()
    page.unlink(missing_ok=True)
    meta.unlink(missing_ok=True)
    return existed


# ---- signed frame addresses ---------------------------------------------------------------------------------------
def sign(name: str, *, now: Optional[float] = None) -> str:
    expires = int((time.time() if now is None else now) + SIG_TTL_S)
    mac = hmac.new(_secret(), f"{name}:{expires}".encode(), hashlib.sha256).hexdigest()[:32]
    return f"{expires}.{mac}"


def verify(name: str, signature: str, *, now: Optional[float] = None) -> bool:
    try:
        expires_s, mac = signature.split(".", 1)
        expires = int(expires_s)
    except ValueError:
        return False
    if expires < (time.time() if now is None else now):
        return False
    good = hmac.new(_secret(), f"{name}:{expires}".encode(), hashlib.sha256).hexdigest()[:32]
    return hmac.compare_digest(good, mac)


def viewer_page(name: str) -> str:
    """The page a person opens: a frame around the canvas that reloads when the agent updates it."""
    name = _check_name(name)
    meta = info(name)
    if meta is None:
        raise CanvasError("no such canvas")
    src = f"/canvas/{name}?sig={sign(name)}"
    return f"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(meta['title'])}</title>
<style>html,body{{margin:0;height:100%;background:#111;color:#ddd;font:14px system-ui}}
header{{padding:6px 12px;background:#1b1b1b;display:flex;justify-content:space-between}}iframe{{border:0;width:100%;height:calc(100% - 34px);background:#fff}}</style></head>
<body><header><span>{html.escape(meta['title'])}</span><span id="v">v{meta['version']}</span></header>
<iframe id="f" sandbox="allow-scripts" referrerpolicy="no-referrer" src="{src}"></iframe>
<script>
let version = {meta['version']};
let sig = new URLSearchParams(location.search).get('sig');      // each answer carries a fresh one, so the view outlives the first 15 minutes
async function tick() {{
  try {{
    const r = await fetch(location.pathname.replace(/\\/view$/, '/version') + '?sig=' + encodeURIComponent(sig));
    if (r.ok) {{ const d = await r.json(); sig = d.sig;
      if (d.version !== version) {{ version = d.version; document.getElementById('v').textContent = 'v' + version;
        const f = document.getElementById('f'); f.src = d.src; }} }}
  }} catch (e) {{}}
  setTimeout(tick, 2000);
}}
setTimeout(tick, 2000);
</script></body></html>"""


def register_tools() -> None:
    from bot.agent_runtime import toolspec
    from bot.agent_runtime.errors import ToolError

    async def _update(inp, *, workspace=None, instance_id=None, device_tier=None) -> str:
        try:
            result = update(str(inp.get("name") or ""), str(inp.get("html") or ""), title=str(inp.get("title") or ""))
        except CanvasError as exc:
            raise ToolError(str(exc))
        return f"Canvas {result['name']!r} is now version {result['version']}. A person watches it at /canvas/{result['name']}/view."

    async def _remove(inp, *, workspace=None, instance_id=None, device_tier=None) -> str:
        try:
            return "Removed." if remove(str(inp.get("name") or "")) else "There was no such canvas."
        except CanvasError as exc:
            raise ToolError(str(exc))

    toolspec.register(
        {"name": "canvas_update",
         "description": "Draw or update a live page a person watches beside the chat: a results table, a chart in SVG, a checklist, a status board. "
                        "Give it a name and the full HTML (inline CSS and JavaScript allowed; it cannot load anything from the network or "
                        "reach anything else). Each call replaces the page and shows the change live.",
         "input_schema": {"type": "object", "properties": {"name": {"type": "string"}, "html": {"type": "string"}, "title": {"type": "string"}},
                          "required": ["name", "html"]}},
        toolspec.ToolSpec("canvas_update", "write", needs_approval=False, origin="registered"), _update)
    toolspec.register(
        {"name": "canvas_remove", "description": "Delete a canvas page.",
         "input_schema": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}},
        toolspec.ToolSpec("canvas_remove", "write", needs_approval=False, origin="registered"), _remove)


register_tools()
