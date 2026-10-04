"""The memory fabric over HTTP (/api/memory; bot/memoryfabric): memories shared by every bot and model and each bot's
own, their review, recall by meaning, the conversation threads every backend writes to, and the block a client puts
in front of a prompt (what Kestrion, mesh-llm, openhuman, 9router or any other program uses to share ABP's memory).

    GET    /api/memory                          settings, the embedder in use, counts
    PUT    /api/memory/settings                 shared_approval, recall_k, recall_min, block_chars, handoff_*, inject, auto_extract
    GET    /api/memory/entries?scope=shared|<instance id>&status=approved|pending|rejected
    POST   /api/memory/entries                  {content, kind?, shared?, instance_id?}
    POST   /api/memory/entries/{id}/approve | /reject       DELETE /api/memory/entries/{id}?scope=...
    GET    /api/memory/search?q=&instance_id=&limit=
    GET    /api/memory/context?q=&instance_id=&thread=&backend=     the block for a prompt
    GET    /api/memory/threads?instance_id=     GET /api/memory/threads/{thread}?after=
    POST   /api/memory/threads/{thread}         {role, text, backend?, model?, instance_id?}  (another program's turn)
  The knowledge base (bot/memoryfabric/knowledge.py, sources.py, vault.py, diff.py):
    GET/POST /api/memory/sources   PATCH/DELETE /api/memory/sources/{id}   POST /api/memory/sources/{id}/sync
    POST   /api/memory/tree        {mode: walk|search_entities|neighbors|query_source|drill_down|cover_window|fetch_leaves, ...}
                                   POST /api/memory/tree/ingest {title, text, source_id?}   GET /api/memory/tree/stats
    GET    /api/memory/diff?source_id=&checkpoint=&since_read=&commit=&text=     POST /api/memory/diff/checkpoint {name}
    GET    /api/memory/vault  (its folder and an obsidian:// link)                POST /api/memory/vault/sync
"""
from __future__ import annotations

import asyncio
from typing import Callable, Optional

from fastapi import Body, Depends, FastAPI, HTTPException, Query

from bot import db, memory
from bot.memoryfabric import store


async def _t(fn, *a, **kw):
    try:
        return await asyncio.to_thread(fn, *a, **kw)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


def _scope(scope: str) -> int:
    if scope in ("shared", "", "0"):
        return store.SHARED
    try:
        return int(scope)
    except ValueError:
        raise HTTPException(400, "scope is shared or a bot instance id") from None


def register(app: FastAPI, require_token: Callable) -> None:
    dep = [Depends(require_token)]
    M = "/api/memory"

    @app.get(M, dependencies=dep)
    async def mem_overview():
        def go():
            c = db.get_conn()
            counts = {f"{'shared' if iid == store.SHARED else 'bots'}:{st}": n for iid, st, n in c.execute(
                "SELECT CASE WHEN instance_id=0 THEN 0 ELSE 1 END, status, COUNT(*) FROM memory_entries GROUP BY 1, 2")}
            return {"settings": store.settings(), "embedder": store.embedder()[0], "counts": counts,
                    "threads": len(store.threads(limit=10_000))}
        return await _t(go)

    @app.put(f"{M}/settings", dependencies=dep)
    async def mem_settings(body: dict = Body(...)):
        return await _t(store.set_settings, body)

    @app.get(f"{M}/entries", dependencies=dep)
    async def mem_entries(scope: str = "shared", status: Optional[str] = None):
        rows = await _t(db.list_memory_entries, _scope(scope), status)
        return [dict(r) for r in rows]

    @app.post(f"{M}/entries", dependencies=dep)
    async def mem_add(body: dict = Body(...)):
        content = str(body.get("content") or "").strip()
        if not content:
            raise HTTPException(400, "content is required")
        iid = body.get("instance_id")
        return await _t(store.remember, content, instance_id=int(iid) if iid not in (None, "") else None,
                        shared=bool(body.get("shared", iid in (None, ""))), source=str(body.get("source") or "user"),
                        kind=str(body.get("kind") or "fact"))

    @app.post(f"{M}/entries/{{entry_id}}/{{action}}", dependencies=dep)
    async def mem_review(entry_id: int, action: str):
        if action not in ("approve", "reject"):
            raise HTTPException(404, "approve or reject")
        row = await _t(memory.approve if action == "approve" else memory.reject, entry_id)
        if row is None:
            raise HTTPException(404, f"no pending memory #{entry_id}")
        return {"id": entry_id, "status": "approved" if action == "approve" else "rejected"}

    @app.delete(f"{M}/entries/{{entry_id}}", dependencies=dep)
    async def mem_forget(entry_id: int, scope: str = "shared"):
        if not await _t(memory.forget, _scope(scope), entry_id):
            raise HTTPException(404, f"no memory #{entry_id} in {scope}")
        return {"deleted": entry_id}

    @app.get(f"{M}/search", dependencies=dep)
    async def mem_search(q: str = Query(...), instance_id: Optional[int] = None, limit: int = 8):
        return await _t(store.recall, q, instance_id, limit)

    @app.get(f"{M}/context", dependencies=dep)
    async def mem_context(q: str = "", instance_id: Optional[int] = None, thread: str = "", backend: str = "external"):
        def go():
            if thread:
                return store.context_block(instance_id, q, thread, backend)
            return store.memory_block(instance_id, q)
        return {"block": await _t(go)}

    @app.get(f"{M}/threads", dependencies=dep)
    async def mem_threads(instance_id: Optional[int] = None, limit: int = 50):
        return await _t(store.threads, instance_id, limit)

    @app.get(f"{M}/threads/{{thread}}", dependencies=dep)
    async def mem_thread(thread: str, after: int = 0, limit: int = 200):
        return await _t(store.turns, thread, after, limit)

    # ---- the knowledge base: sources, the tree, the vault, the diff ledger ----
    from bot.memoryfabric import diff as kdiff
    from bot.memoryfabric import knowledge, sources, vault

    @app.get(f"{M}/sources", dependencies=dep)
    async def mem_sources():
        return await _t(sources.status_list)

    @app.post(f"{M}/sources", dependencies=dep)
    async def mem_source_add(body: dict = Body(...)):
        b = dict(body)
        return await _t(sources.add, str(b.pop("kind", "")), str(b.pop("label", "")), **b)

    @app.patch(f"{M}/sources/{{source_id}}", dependencies=dep)
    async def mem_source_update(source_id: str, body: dict = Body(...)):
        return await _t(sources.update, source_id, **body)

    @app.delete(f"{M}/sources/{{source_id}}", dependencies=dep)
    async def mem_source_remove(source_id: str):
        return {"removed": await _t(sources.remove, source_id)}

    @app.post(f"{M}/sources/{{source_id}}/sync", dependencies=dep)
    async def mem_source_sync(source_id: str):
        return await _t(sources.sync, source_id)

    @app.post(f"{M}/tree", dependencies=dep)
    async def mem_tree(body: dict = Body(...)):
        b = dict(body)
        mode = str(b.pop("mode", "walk"))
        if mode == "ingest_document":            # a write: its own route, so read-only integration keys cannot do it
            raise HTTPException(400, "documents are added with POST /api/memory/tree/ingest")
        try:
            return await asyncio.to_thread(knowledge.query, mode, **b)
        except (KeyError, ValueError) as e:
            raise HTTPException(400, f"{type(e).__name__}: {e}") from e

    @app.post(f"{M}/tree/ingest", dependencies=dep)
    async def mem_tree_ingest(body: dict = Body(...)):
        if not str(body.get("text") or "").strip():
            raise HTTPException(400, "text is required")
        return await _t(knowledge.query, "ingest_document", **body)

    @app.get(f"{M}/tree/stats", dependencies=dep)
    async def mem_tree_stats():
        return await _t(knowledge.stats)

    @app.get(f"{M}/diff", dependencies=dep)
    async def mem_diff(source_id: str = "", checkpoint: str = "", since_read: bool = True, commit: bool = True, text: bool = False):
        try:
            return await asyncio.to_thread(kdiff.diff, source_id, checkpoint_name=checkpoint, since_read=since_read,
                                           commit=commit, include_text=text)
        except (ValueError, RuntimeError) as e:
            raise HTTPException(400, str(e)) from e

    @app.post(f"{M}/diff/checkpoint", dependencies=dep)
    async def mem_checkpoint(body: dict = Body(...)):
        try:
            return {"checkpoint": await asyncio.to_thread(kdiff.checkpoint, str(body.get("name") or ""))}
        except (ValueError, RuntimeError) as e:
            raise HTTPException(400, str(e)) from e

    from bot.memoryfabric import rules

    @app.get(f"{M}/tool-rules", dependencies=dep)
    async def mem_tool_rules(tool: str = ""):
        return await _t(rules.list_rules, tool)

    @app.post(f"{M}/tool-rules", dependencies=dep)
    async def mem_tool_rule_put(body: dict = Body(...)):
        return await _t(rules.put_rule, str(body.get("tool") or ""), str(body.get("rule") or ""),
                        str(body.get("priority") or "normal"), "user_explicit", list(body.get("tags") or []), str(body.get("id") or ""))

    @app.delete(f"{M}/tool-rules/{{rule_id}}", dependencies=dep)
    async def mem_tool_rule_delete(rule_id: str):
        if not await _t(rules.delete_rule, rule_id):
            raise HTTPException(404, f"no tool rule {rule_id}")
        return {"deleted": rule_id}

    @app.get(f"{M}/goals", dependencies=dep)
    async def mem_goals(all: bool = False):  # noqa: A002 - the query parameter's name
        return await _t(rules.goals, all)

    @app.post(f"{M}/goals", dependencies=dep)
    async def mem_goal_put(body: dict = Body(...)):
        g = await _t(rules.put_goal, str(body.get("text") or ""), str(body.get("status") or "active"), str(body.get("id") or ""))
        await _t(rules.write_goals_file)
        return g

    @app.delete(f"{M}/goals/{{goal_id}}", dependencies=dep)
    async def mem_goal_delete(goal_id: str):
        if not await _t(rules.delete_goal, goal_id):
            raise HTTPException(404, f"no goal {goal_id}")
        await _t(rules.write_goals_file)
        return {"deleted": goal_id}

    @app.get(f"{M}/vault", dependencies=dep)
    async def mem_vault():
        return {"path": str(await _t(vault.root)), "obsidian": vault.obsidian_link()}

    @app.post(f"{M}/vault/sync", dependencies=dep)
    async def mem_vault_sync():
        return await _t(vault.write_all)

    @app.post(f"{M}/threads/{{thread}}", dependencies=dep)
    async def mem_thread_add(thread: str, body: dict = Body(...)):
        role = body.get("role")
        if role not in ("user", "assistant"):
            raise HTTPException(400, "role is user or assistant")
        iid = body.get("instance_id")
        tid = await _t(store.record, thread, role, str(body.get("text") or ""), instance_id=int(iid) if iid not in (None, "") else None,
                       backend=str(body.get("backend") or "external"), model=str(body.get("model") or ""))
        return {"id": tid}
