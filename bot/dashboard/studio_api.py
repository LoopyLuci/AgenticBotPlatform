"""Studio: generative code and GUI with a real-time preview (bot/studio).

Preview (pages, like the dashboard itself: a local page load gets the token):
    GET /studio/v/{vid}/                       the dashboard with the variant's files swapped in; it reloads itself when
                                               one of them changes
    GET /studio/v/{vid}/static/{path}          the variant's copy of a UI file, else the live one
    GET /studio/v/{vid}/components?theme=      the component gallery (buttons, chips, cards, chat, forms...) in the
                                               variant's styles

API (the dashboard token):
    GET  /api/studio                           variants, UI files, backups
    POST /api/studio/variants                  {name?, note?, base?}
    GET  /api/studio/variants/{vid}            its files and diff against the live UI
    GET  /api/studio/variants/{vid}/file?path= / PUT {path, content} / DELETE ?path=
    GET  /api/studio/variants/{vid}/version    what the preview polls
    GET  /api/studio/variants/{vid}/tokens?page=  /  POST {mode, values}     theme tokens
    POST /api/studio/variants/{vid}/validate | /apply | /discard | /rate {rating, why?} | /shot {section?}
    POST /api/studio/generate                  {instruction, files, variants?, models?, focus?, title?, base?} -> a job
    GET  /api/studio/jobs/{id}
    POST /api/studio/revert                    {backup}
    GET  /api/studio/log?limit=&kind=          POST /api/studio/datasets
"""
from __future__ import annotations

import asyncio
import mimetypes
import re
import time
import uuid
from typing import Callable

from fastapi import Body, Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse

from bot.studio import generate, log, shots, tokens, variants
from bot.studio.variants import StudioError

PAGE = "bot/dashboard/static/dashboard.html"
_jobs: dict[str, dict] = {}

_RELOAD = """(function(){var vid=%s,seen=null;function tok(){return window.__ABP_TOKEN__||'';}
function poll(){fetch('/api/studio/variants/'+vid+'/version',{headers:{'X-Dashboard-Token':tok()}}).then(function(r){
return r.ok?r.json():null}).then(function(d){if(!d)return;if(seen!==null&&d.version!==seen){try{sessionStorage.setItem(
'studio.scroll',String(window.scrollY))}catch(e){}location.reload();}seen=d.version;}).catch(function(){});}
setInterval(poll,1000);poll();window.addEventListener('load',function(){try{var y=sessionStorage.getItem('studio.scroll');
if(y!==null){window.scrollTo(0,+y);sessionStorage.removeItem('studio.scroll');}}catch(e){}});})();"""


def _page_html(vid: str) -> str:
    html = variants.read(vid, PAGE)
    base = f"/studio/v/{vid}/static/"
    return re.sub(r'(src|href)="/static/', lambda m: f'{m.group(1)}="{base}', html)


GALLERY = """<!doctype html><html lang="en"{theme}><head><meta charset="utf-8"><title>Components</title>
<style>{styles}</style><style>body{{overflow:auto;height:auto;padding:24px}} .g{{display:grid;gap:18px;max-width:980px}}
.g h3{{margin:18px 0 4px;font-family:var(--font-display)}} .row{{display:flex;gap:8px;flex-wrap:wrap;align-items:center}}</style>
</head><body><div class="g">
<h3>Buttons</h3><div class="row"><button class="btn">Default</button><button class="btn primary">Primary</button>
<button class="btn ghost">Ghost</button><button class="btn danger">Danger</button><button class="btn" disabled>Disabled</button></div>
<h3>Chips and pills</h3><div class="row"><span class="chip good">running</span><span class="chip warning">slow</span>
<span class="chip serious">degraded</span><span class="chip critical">down</span><span class="pill"><span class="dot" style="background:var(--good)"></span>Online<span class="pill-sub">3 bots</span></span></div>
<h3>Toggles and choices</h3><div class="row"><div class="toggle on"></div><div class="toggle"></div>
<div class="segmented"><button class="active">Day</button><button>Week</button><button>Month</button></div></div>
<h3>Card</h3><div class="card"><div class="sec-head"><h2>Section title</h2><span class="desc">A description of what this section does.</span></div>
<p class="cardnote">A note in a card, as panels show while they load.</p><div class="row"><input placeholder="A field"><select><option>An option</option></select></div></div>
<h3>Chat</h3><div class="chat-window"><div class="chat-row in"><div class="chat-bubble">How many modules are running?<span class="chat-meta">you · 12:01</span></div></div>
<div class="chat-row out"><div class="chat-bubble">Three hubs are running: opencv-zoo, kotmoe and cacheit.<span class="chat-meta">ABP · 12:01</span></div></div></div>
<h3>Table</h3><div class="tablewrap"><table><tr><th>Module</th><th>State</th><th>Operations</th></tr>
<tr><td>OpenCV model zoo</td><td><span class="chip good">hub running</span></td><td class="num">8</td></tr>
<tr><td>COOL Benchmark</td><td><span class="chip">stopped</span></td><td class="num">3</td></tr></table></div>
</div></body></html>"""


def _gallery(vid: str, theme: str) -> str:
    html = variants.read(vid, PAGE)
    head = html[:html.find("</head>")]
    styles = "\n".join(re.findall(r"<style[^>]*>(.*?)</style>", head, re.S))
    attr = f' data-theme="{theme}"' if theme in ("light", "dark") else ""
    return GALLERY.format(theme=attr, styles=styles)


def _reload_open_windows(files: list[str]) -> None:
    """The same broadcast bot/ui_customize.py sends: open dashboard tabs and desktop windows reload themselves."""
    try:
        from bot.dashboard.server import _broadcast_soon
        if any(f.startswith("bot/dashboard/static/") for f in files):
            _broadcast_soon({"type": "static_file_changed", "target": "dashboard"})
        if any(f.startswith("desktop-app/ui/") for f in files):
            _broadcast_soon({"type": "static_file_changed", "target": "desktop_html"})
    except Exception:  # noqa: BLE001 - a reload hint never breaks an apply
        pass


async def _s(fn, *args, **kwargs):
    try:
        return await asyncio.to_thread(fn, *args, **kwargs)
    except StudioError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


def register(app: FastAPI, require_token: Callable) -> None:
    from bot.dashboard.server import html_page
    dep = [Depends(require_token)]

    # ---- preview pages (like "/": no token needed to load; a local load is handed it) ----------------------------
    @app.get("/studio/v/{vid}/", response_class=HTMLResponse)
    async def studio_preview(vid: str, request: Request):
        try:
            html = await asyncio.to_thread(_page_html, vid)
        except StudioError as e:
            raise HTTPException(status_code=404, detail=str(e)) from e
        return html_page(html, request, head_script=_RELOAD % repr(vid))

    @app.get("/studio/v/{vid}/components", response_class=HTMLResponse)
    async def studio_components(vid: str, request: Request, theme: str = Query("")):
        try:
            html = await asyncio.to_thread(_gallery, vid, theme)
        except StudioError as e:
            raise HTTPException(status_code=404, detail=str(e)) from e
        return html_page(html, request, head_script=_RELOAD % repr(vid))

    @app.get("/studio/v/{vid}/static/{path:path}")
    async def studio_static(vid: str, path: str):
        try:
            p = await asyncio.to_thread(variants.resolve, vid, "bot/dashboard/static/" + path)
        except StudioError as e:
            raise HTTPException(status_code=404, detail=str(e)) from e
        if p is None:
            raise HTTPException(status_code=404, detail="no such file")
        return FileResponse(p, media_type=mimetypes.guess_type(p.name)[0] or "application/octet-stream",
                            headers={"Cache-Control": "no-store"})

    # ---- the API ------------------------------------------------------------------------------------------------
    @app.get("/api/studio", dependencies=dep)
    async def studio_overview():
        def go():
            ui = [str(p.relative_to(variants.code_root())).replace("\\", "/") for root in variants.UI_ROOTS
                  for p in sorted((variants.code_root() / root).rglob("*")) if p.is_file() and p.suffix in variants.UI_EXTS
                  and "node_modules" not in p.parts and "assets" not in p.parts]
            return {"variants": variants.listing(), "ui_files": ui, "backups": variants.backups()[:30]}
        return await _s(go)

    @app.post("/api/studio/variants", dependencies=dep)
    async def studio_create(body: dict = Body(default={})):
        return await _s(variants.create, str(body.get("name") or ""), note=str(body.get("note") or ""),
                        base=str(body.get("base") or ""))

    @app.get("/api/studio/variants/{vid}", dependencies=dep)
    async def studio_variant(vid: str):
        return await _s(lambda: {**variants.meta(vid), "files": variants.files(vid), "diff": variants.diff(vid)})

    @app.get("/api/studio/variants/{vid}/file", dependencies=dep)
    async def studio_read(vid: str, path: str = Query(...)):
        return await _s(lambda: {"path": variants.check_rel(path), "content": variants.read(vid, path),
                                 "own": variants.check_rel(path) in variants.files(vid)})

    @app.put("/api/studio/variants/{vid}/file", dependencies=dep)
    async def studio_write(vid: str, body: dict = Body(...)):
        return await _s(variants.write, vid, str(body.get("path") or ""), str(body.get("content") or ""))

    @app.delete("/api/studio/variants/{vid}/file", dependencies=dep)
    async def studio_reset(vid: str, path: str = Query(...)):
        return await _s(variants.reset_file, vid, path)

    @app.get("/api/studio/variants/{vid}/version", dependencies=dep)
    async def studio_version(vid: str):
        return await _s(lambda: {"version": variants.version(vid)})

    @app.get("/api/studio/variants/{vid}/tokens", dependencies=dep)
    async def studio_tokens(vid: str, page: str = Query(tokens.PAGES[0])):
        return await _s(tokens.read, vid, page)

    @app.post("/api/studio/variants/{vid}/tokens", dependencies=dep)
    async def studio_set_tokens(vid: str, body: dict = Body(...)):
        return await _s(tokens.set_tokens, vid, str(body.get("mode") or "light"), dict(body.get("values") or {}))

    @app.post("/api/studio/variants/{vid}/validate", dependencies=dep)
    async def studio_validate(vid: str):
        return await _s(lambda: {"problems": generate.validate(vid)})

    @app.post("/api/studio/variants/{vid}/apply", dependencies=dep)
    async def studio_apply(vid: str):
        def go():
            problems = generate.validate(vid)
            if problems:
                raise StudioError("not applied, it has problems: " + "; ".join(problems[:5]))
            res = variants.apply(vid)
            _reload_open_windows(res["files"])
            return res
        return await _s(go)

    @app.post("/api/studio/variants/{vid}/discard", dependencies=dep)
    async def studio_discard(vid: str):
        return await _s(variants.discard, vid)

    @app.post("/api/studio/variants/{vid}/rate", dependencies=dep)
    async def studio_rate(vid: str, body: dict = Body(...)):
        return await _s(variants.rate, vid, int(body.get("rating") or 0), str(body.get("why") or ""))

    @app.post("/api/studio/variants/{vid}/shot", dependencies=dep)
    async def studio_shot(vid: str, body: dict = Body(default={})):
        return await _s(shots.compare, vid, str(body.get("section") or ""))

    @app.post("/api/studio/generate", dependencies=dep)
    async def studio_generate(body: dict = Body(...)):
        jid = uuid.uuid4().hex[:10]
        job = _jobs[jid] = {"id": jid, "state": "running", "started": time.time()}

        async def go():
            try:
                job["result"] = await generate.propose(
                    str(body.get("instruction") or ""), list(body.get("files") or []),
                    variants_wanted=int(body.get("variants") or 3), models=body.get("models") or None,
                    focus=str(body.get("focus") or ""), title=str(body.get("title") or ""),
                    base=str(body.get("base") or ""))
                job["state"] = "done"
            except Exception as e:  # noqa: BLE001
                job.update(state="failed", error=str(e)[:1000])
            job["finished"] = time.time()
        asyncio.get_running_loop().create_task(go())
        for old in sorted(_jobs.values(), key=lambda j: j["started"])[:-50]:
            _jobs.pop(old["id"], None)
        return job

    @app.get("/api/studio/jobs/{jid}", dependencies=dep)
    async def studio_job(jid: str):
        job = _jobs.get(jid)
        if job is None:
            raise HTTPException(status_code=404, detail="no such job")
        return job

    @app.post("/api/studio/revert", dependencies=dep)
    async def studio_revert(body: dict = Body(...)):
        res = await _s(variants.revert, str(body.get("backup") or ""))
        _reload_open_windows(res["files"])
        return res

    @app.get("/api/studio/log", dependencies=dep)
    async def studio_log(limit: int = Query(200), kind: str = Query("")):
        return await _s(lambda: {"events": [{k: v for k, v in e.items() if k != "raw"} for e in log.read(limit, kind)]})

    @app.post("/api/studio/datasets", dependencies=dep)
    async def studio_datasets():
        return await _s(log.datasets)
