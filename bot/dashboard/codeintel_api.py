"""Code-intelligence API (read-only apart from refreshing its own index):

    GET  /api/codeintel/stats?root=                      index size, parsers, languages, per-tool token counters
    POST /api/codeintel/index        {root?, force?}     bring the index up to date, return timings
    GET  /api/codeintel/outline?path=&root=&max_tokens=  signatures and line ranges of a file or folder
    GET  /api/codeintel/find?q=&kind=&limit=&root=       find symbols by name or words
    GET  /api/codeintel/read?target=&context=&root=      exact body of a symbol (qualified name or path:line)
    GET  /api/codeintel/refs?symbol=&root=               where a name is used
    GET  /api/codeintel/callers?symbol=&root=            who calls it
    GET  /api/codeintel/tests?target=&root=              tests for a file or symbol
    POST /api/codeintel/impact       {changes, root?}    symbols, callers and tests touched by a diff or paths
    POST /api/codeintel/pack         {task, budget_tokens?, root?}   a budgeted context bundle

`root` defaults to ABP's code folder; other folders must be listed under native_agent.codeintel.roots. Every answer is
{"text", "tokens", "truncated", "data"}.
"""
from __future__ import annotations

import asyncio
from typing import Any, Callable, Optional

from fastapi import Body, Depends, FastAPI, HTTPException, Query


def register(app: FastAPI, auth: Callable) -> None:
    from bot.codeintel import indexer, roots, tools

    dep = [Depends(auth)]

    def root_of(root: Optional[str]):
        try:
            return roots.resolve_root(root)
        except roots.RootNotAllowed as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc

    async def run(fn, *args, **kwargs) -> dict[str, Any]:
        try:
            res = await asyncio.to_thread(fn, *args, **kwargs)
        except tools.CodeIntelError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"text": res.text, "tokens": res.tokens, "truncated": res.truncated, "data": res.data}

    @app.get("/api/codeintel/stats", dependencies=dep)
    async def codeintel_stats(root: Optional[str] = None):
        return await asyncio.to_thread(tools.stats, root_of(root))

    @app.post("/api/codeintel/index", dependencies=dep)
    async def codeintel_index(body: dict = Body(default={})):
        r = root_of(body.get("root"))
        out = await asyncio.to_thread(indexer.update, r, force=bool(body.get("force")), chunks=bool(body.get("chunks")))
        tools.forget_freshness(r)
        return out

    @app.get("/api/codeintel/outline", dependencies=dep)
    async def codeintel_outline(path: str = Query("."), root: Optional[str] = None, max_tokens: int = 1500):
        return await run(tools.outline, root_of(root), path, max_tokens=max_tokens)

    @app.get("/api/codeintel/find", dependencies=dep)
    async def codeintel_find(q: str, kind: str = "", limit: int = 15, root: Optional[str] = None, max_tokens: int = 900):
        return await run(tools.find_symbol, root_of(root), q, kind=kind, limit=limit, max_tokens=max_tokens)

    @app.get("/api/codeintel/read", dependencies=dep)
    async def codeintel_read(target: str, context: int = 0, root: Optional[str] = None, max_tokens: int = 3000):
        return await run(tools.read_symbol, root_of(root), target, context_lines=context, max_tokens=max_tokens)

    @app.get("/api/codeintel/refs", dependencies=dep)
    async def codeintel_refs(symbol: str, root: Optional[str] = None, limit: int = 60, max_tokens: int = 1400):
        return await run(tools.references, root_of(root), symbol, limit=limit, max_tokens=max_tokens)

    @app.get("/api/codeintel/callers", dependencies=dep)
    async def codeintel_callers(symbol: str, root: Optional[str] = None, limit: int = 40, max_tokens: int = 1200):
        return await run(tools.callers, root_of(root), symbol, limit=limit, max_tokens=max_tokens)

    @app.get("/api/codeintel/tests", dependencies=dep)
    async def codeintel_tests(target: str, root: Optional[str] = None, max_tokens: int = 700):
        return await run(tools.related_tests, root_of(root), target, max_tokens=max_tokens)

    @app.post("/api/codeintel/impact", dependencies=dep)
    async def codeintel_impact(body: dict = Body(...)):
        return await run(tools.impact, root_of(body.get("root")), str(body.get("changes") or ""),
                         max_tokens=int(body.get("max_tokens") or 2200))

    @app.post("/api/codeintel/pack", dependencies=dep)
    async def codeintel_pack(body: dict = Body(...)):
        return await run(tools.context_pack, root_of(body.get("root")), str(body.get("task") or ""),
                         budget_tokens=int(body.get("budget_tokens") or 4000))
