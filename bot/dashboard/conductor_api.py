"""Conductor API: the local conductor (bot/conductor, docs/local-conductor.md).

    GET  /api/conductor/status              capacity policy, plans, open escalations, integrations, the push queue
    POST /api/conductor/plan                {"goal", "replan"} plan a goal (free text, or FILE:LINE of a roadmap/TODO row) with a local model
    GET  /api/conductor/plans               the stored plans
    GET  /api/conductor/plans/{id}          one plan
    POST /api/conductor/review              {"repo", "base", "head", "goal", "model"} review a diff against the standing rules (static;
                                            "model": true adds a local model review). The report is also machine-readable.
    GET  /api/conductor/reviews?plan=&task= stored review reports
    GET  /api/conductor/queue?status=       the push queue (default: all, newest first)
    POST /api/conductor/queue/{id}/approve  {"by", "note"} record the owner's (or Claude's) approval; it does NOT push
    POST /api/conductor/queue/{id}/reject   {"by", "note"}
    POST /api/conductor/queue/{id}/push     {"by"} push an APPROVED entry (exactly its approved commit; needs conductor.pusher.enabled;
                                            refuses when unapproved, changed, stale or leaking; never forces, never deploys)
    GET  /api/conductor/queue/{id}/audit    what happened to an entry: approval, push attempts, refusals
    GET  /api/conductor/escalations         tasks waiting for a decision (two review rejections, ...)
    GET  /api/conductor/knowledge           LF7: lesson counts, failing review rules and the conductor.knowledge switches
    GET  /api/conductor/knowledge/lessons?repo=&outcome=   the recorded lessons (newest first)
    GET  /api/conductor/knowledge/recall?repo=&text=&files=&goal=   what a worker (text) and the planner (goal) would be given
    PUT  /api/conductor/knowledge/settings  {"enabled"?, "for_planner"?, "for_workers"?, "owner_rules"?, "to_memoryfabric"?}
                                            switch each part on or off (audited; the next run reads it)
    POST /api/conductor/knowledge/export    {"repo"?} rejected attempts as JSONL for later fine-tunes (agent state folder)

Reading uses the dashboard's normal auth; planning, reviewing and deciding need the dashboard token.
"""
from __future__ import annotations

from typing import Callable

from fastapi import Body, Depends, FastAPI, HTTPException, Query


def register(app: FastAPI, read_auth: Callable, write_auth: Callable) -> None:
    read = [Depends(read_auth)]
    write = [Depends(write_auth)]

    @app.get("/api/conductor/status", dependencies=read)
    async def conductor_status():
        from bot.conductor import run

        return run.status()

    @app.post("/api/conductor/plan", dependencies=write)
    async def conductor_plan(body: dict = Body(...)):
        import asyncio

        from bot import conductor
        from bot.conductor import planner
        from bot.envfile import PROJECT_ROOT

        cfg = conductor.settings()
        try:
            from bot.conductor import knowledge

            goal = str(body.get("goal") or "")
            learned = await asyncio.to_thread(knowledge.planner_block, "abp", goal, cfg) if goal else ""
            plan = await planner.make_plan(goal, root=PROJECT_ROOT, model=cfg["model"], context=learned,
                                           max_tasks=int(cfg["max_tasks"]), replan=bool(body.get("replan")))
        except planner.PlanError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return plan.to_dict()

    @app.get("/api/conductor/plans", dependencies=read)
    async def conductor_plans(limit: int = Query(30, ge=1, le=200)):
        from bot.conductor import store

        return {"plans": store.list_plans(limit)}

    @app.get("/api/conductor/plans/{plan_id}", dependencies=read)
    async def conductor_plan_get(plan_id: str):
        from bot.conductor import store

        plan = store.get_plan(plan_id)
        if plan is None:
            raise HTTPException(status_code=404, detail="no such plan")
        return plan

    @app.post("/api/conductor/review", dependencies=write)
    async def conductor_review(body: dict = Body(...)):
        import asyncio
        import os

        from bot import conductor
        from bot.conductor import reviewer

        cfg = conductor.settings()
        repo, base, head = str(body.get("repo") or ""), str(body.get("base") or cfg["base_branch"]), str(body.get("head") or "")
        if not repo or not head:
            raise HTTPException(status_code=400, detail="repo and head are required")
        try:
            rep = await asyncio.to_thread(
                reviewer.review, repo, base, head, created_files=body.get("created") or [], allow_delete=body.get("allow_delete") or [],
                public=bool(cfg["public_repo"]), private_dir=os.environ.get("ABP_PRIVATE_DIR") or None, run_ruff=bool(cfg["check_ruff"]),
                check_reload=bool(cfg["check_hotreload"]))
            if body.get("model") and rep.accepted:
                await reviewer.model_review(rep, repo, goal=str(body.get("goal") or ""), model=cfg["review_model"], avoid=cfg["model"],
                                            diff_cap=int(cfg["review_diff_cap"]))
        except (RuntimeError, OSError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {**rep.to_dict(), "text": rep.to_text()}

    @app.get("/api/conductor/reviews", dependencies=read)
    async def conductor_reviews(plan: str = Query(""), task: str = Query(""), limit: int = Query(50, ge=1, le=200)):
        from bot.conductor import store

        return {"reviews": store.list_reviews(plan, task, limit)}

    @app.get("/api/conductor/queue", dependencies=read)
    async def conductor_queue(status: str = Query("")):
        from bot.conductor import store

        return {"queue": store.list_queue(status)}

    def _decide(queue_id: str, body: dict, reject: bool) -> dict:
        from bot.conductor import run

        try:
            return run.approve(queue_id, str(body.get("by") or ""), str(body.get("note") or ""), reject=reject)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="no such queue entry") from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/conductor/queue/{queue_id}/approve", dependencies=write)
    async def conductor_approve(queue_id: str, body: dict = Body(...)):
        return _decide(queue_id, body, False)

    @app.post("/api/conductor/queue/{queue_id}/reject", dependencies=write)
    async def conductor_reject(queue_id: str, body: dict = Body(...)):
        return _decide(queue_id, body, True)

    @app.post("/api/conductor/queue/{queue_id}/push", dependencies=write)
    async def conductor_push(queue_id: str, body: dict = Body(...)):
        import asyncio

        from bot.conductor import pusher, run

        try:
            return await asyncio.to_thread(run.push, queue_id, str(body.get("by") or ""))
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="no such queue entry") from exc
        except pusher.PushStale as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except pusher.PushRefused as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc

    @app.get("/api/conductor/queue/{queue_id}/audit", dependencies=read)
    async def conductor_queue_audit(queue_id: str):
        from bot.conductor import store

        if store.get_queue_entry(queue_id) is None:
            raise HTTPException(status_code=404, detail="no such queue entry")
        return {"audit": store.list_audit(queue_id)}

    @app.get("/api/conductor/escalations", dependencies=read)
    async def conductor_escalations(all: bool = Query(False)):
        from bot.conductor import store

        return {"escalations": store.list_escalations(not all)}

    # ------------------------------------------------------------- LF7 knowledge
    def _knowledge_doc() -> dict:
        from bot.conductor import knowledge

        return {"settings": knowledge.settings(), "switches": list(knowledge.SWITCHES), **knowledge.stats()}

    @app.get("/api/conductor/knowledge", dependencies=read)
    async def conductor_knowledge():
        import asyncio

        return await asyncio.to_thread(_knowledge_doc)

    @app.get("/api/conductor/knowledge/lessons", dependencies=read)
    async def conductor_knowledge_lessons(repo: str = Query(""), outcome: str = Query(""), limit: int = Query(100, ge=1, le=1000)):
        from bot.conductor import knowledge

        return {"lessons": knowledge.lessons(repo, limit, outcome)}

    @app.get("/api/conductor/knowledge/recall", dependencies=read)
    async def conductor_knowledge_recall(repo: str = Query("abp"), text: str = Query(""), files: str = Query(""), goal: str = Query("")):
        from bot.conductor import knowledge

        fl = [f.strip() for f in files.split(",") if f.strip()]
        return {"task": knowledge.task_block(repo, text, "", fl) if text or fl else "",
                "planner": knowledge.planner_block(repo, goal) if goal else ""}

    @app.put("/api/conductor/knowledge/settings", dependencies=write)
    def conductor_knowledge_settings(body: dict = Body(...)):
        from bot import db
        from bot.conductor import knowledge
        from bot.config import config

        changes: dict = {}
        for k in knowledge.SWITCHES:
            if k in body:
                if not isinstance(body[k], bool):
                    raise HTTPException(status_code=400, detail=f"{k} must be true or false")
                changes[("conductor", "knowledge", k)] = body[k]
        if not changes:
            raise HTTPException(status_code=400, detail=f"send one or more of: {', '.join(knowledge.SWITCHES)}")
        config.set_values(changes, actor="dashboard")
        db.log_audit(actor="dashboard", action="conductor_knowledge_set", detail=", ".join(f"{k[-1]}={v}" for k, v in changes.items()))
        return _knowledge_doc()

    @app.post("/api/conductor/knowledge/export", dependencies=write)
    def conductor_knowledge_export(body: dict = Body(default={})):
        from bot import db
        from bot.agent_runtime.state import state_dir
        from bot.conductor import knowledge

        out = knowledge.export_dataset(state_dir("conductor") / "datasets" / "factory-lessons.jsonl", str((body or {}).get("repo") or ""))
        db.log_audit(actor="dashboard", action="conductor_knowledge_export", detail=f"{out['rows']} rows")
        return out
