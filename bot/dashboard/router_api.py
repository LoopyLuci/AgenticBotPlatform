"""Router API: see how the model router thinks and acts, teach it, and change how it decides.

    GET  /api/router/overview?hours=24          counts, success rate, a timeline, model x task-class outcomes, resting models
    GET  /api/router/models                     what it has learned about each model (reliability with its range, speed,
                                                feedback, rest, share of picks)
    GET  /api/router/decisions?limit=&mode=&status=&model=&task_class=&before=
    GET  /api/router/decisions/{id}             one decision with every candidate's reasoning, and the decisions around it
    POST /api/router/decisions/{id}/feedback    {"rating": 1|-1|0, "note", "correct_class", "preferred_model"}
    POST /api/router/simulate                   {"task", "images", "context_tokens", "candidates"} -> what it would do now
    GET  /api/router/events?limit=&kind=&model= the learning log
    GET  /api/router/policy                     the policy in force, its version, the defaults and the allowed values
    PUT  /api/router/policy                     {"policy": {...}, "note": "..."} -> a new version (refused with a reason if malformed)
    GET  /api/router/policy/history             every version with what changed
    POST /api/router/policy/rollback            {"version": N} (0 = the defaults)
    GET  /api/router/examples                   training examples
    POST /api/router/examples                   {"text", "task_class", "preferred_model"}
    DELETE /api/router/examples/{id}
    POST /api/router/models/rest                {"model": "provider/model" or "provider/*", "seconds": N, "reason"} rest it by hand
    POST /api/router/models/release             {"model"} end a rest early
    POST /api/router/models/forget              {"model"} forget everything learned about it
    POST /api/router/reset                      {"keep_examples": true, "keep_policy": true} start learning from scratch

Reading uses the dashboard's normal auth; anything that changes what the router does needs the desktop dashboard token.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any, Callable, Optional

from fastapi import Body, Depends, FastAPI, HTTPException, Query


def _decision_row(row: dict[str, Any], *, full: bool = False) -> dict[str, Any]:
    detail = json.loads(row.pop("detail") or "{}")
    if full:
        row["detail"] = detail
    else:
        cands = detail.get("candidates") or []
        row["candidates"] = len(cands)
        row["score"] = detail.get("chosen_score")
        row["runner_up"] = next((c["model"] for c in cands if c["model"] != row.get("chosen")), None)
        row["skipped"] = len(detail.get("skipped") or [])
    return row


def register(app: FastAPI, read_auth: Callable, write_auth: Callable) -> None:
    from bot import db, model_router
    from bot.router_brain import learn, policy, store

    read = [Depends(read_auth)]
    write = [Depends(write_auth)]

    @app.get("/api/router/overview", dependencies=read)
    async def router_overview(hours: int = Query(24, ge=1, le=24 * 90)):
        return await asyncio.to_thread(learn.overview, hours)

    @app.get("/api/router/models", dependencies=read)
    async def router_models():
        board = await asyncio.to_thread(learn.model_board)
        try:
            candidates = await asyncio.to_thread(model_router.candidate_models)
        except Exception:  # noqa: BLE001
            candidates = []
        seen = {m["model"] for m in board}
        return {"models": board, "candidates": candidates, "never_used": [c for c in candidates if c not in seen]}

    @app.get("/api/router/decisions", dependencies=read)
    def router_decisions(limit: int = Query(100, ge=1, le=1000), mode: Optional[str] = None, status: Optional[str] = None,
                         model: Optional[str] = None, task_class: Optional[str] = None, before: Optional[int] = None,
                         instance_id: Optional[int] = None):
        sql, params = "SELECT * FROM decisions WHERE 1=1", []
        for col, val in (("mode", mode), ("status", status), ("chosen", model), ("task_class", task_class), ("instance_id", instance_id)):
            if val not in (None, ""):
                sql += f" AND {col} = ?"
                params.append(val)
        if before:
            sql += " AND id < ?"
            params.append(before)
        rows = store.rows(sql + " ORDER BY id DESC LIMIT ?", (*params, limit))
        return {"decisions": [_decision_row(r) for r in rows]}

    @app.get("/api/router/decisions/{decision_id}", dependencies=read)
    def router_decision(decision_id: int):
        row = store.one("SELECT * FROM decisions WHERE id = ?", (decision_id,))
        if not row:
            raise HTTPException(status_code=404, detail="no such decision")
        out = _decision_row(row, full=True)
        out["children"] = [_decision_row(r) for r in store.rows("SELECT * FROM decisions WHERE parent_id = ? ORDER BY id", (decision_id,))]
        parent = store.one("SELECT * FROM decisions WHERE id = ?", (row["parent_id"],)) if row.get("parent_id") else None
        out["parent"] = _decision_row(parent) if parent else None
        out["events"] = store.rows("SELECT * FROM events WHERE decision_id = ? ORDER BY id", (decision_id,))
        return out

    @app.post("/api/router/decisions/{decision_id}/feedback", dependencies=write)
    def router_feedback(decision_id: int, payload: dict = Body(...)):
        rating = payload.get("rating")
        try:
            result = learn.feedback(decision_id, rating=int(rating) if rating not in (None, "") else None, note=str(payload.get("note") or ""),
                                    correct_class=payload.get("correct_class") or None, preferred_model=payload.get("preferred_model") or None)
        except KeyError:
            raise HTTPException(status_code=404, detail="no such decision") from None
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        db.log_audit(actor="dashboard", action="router_feedback", detail=f"#{decision_id}: {'; '.join(result['learned'])}"[:500])
        return result

    @app.post("/api/router/simulate", dependencies=read)
    async def router_simulate(payload: dict = Body(...)):
        task = str(payload.get("task") or "").strip()
        if not task:
            raise HTTPException(status_code=400, detail="describe a task to route")
        cands = payload.get("candidates")
        cands = [str(c) for c in cands] if isinstance(cands, list) and cands else None

        def run():
            from dataclasses import asdict

            cls, ranked, skipped = model_router.recommend(task, candidates=cands, images=bool(payload.get("images")),
                                                          context_tokens=int(payload.get("context_tokens") or 0), limit=50)
            return {"classification": asdict(cls), "candidates": [asdict(r) for r in ranked], "skipped": skipped,
                    "weights": policy.current()["weights"][cls.task_class], "text": model_router.describe(cls, ranked[:5], skipped)}
        return await asyncio.to_thread(run)

    @app.get("/api/router/events", dependencies=read)
    def router_events(limit: int = Query(200, ge=1, le=2000), kind: Optional[str] = None, model: Optional[str] = None):
        return {"events": learn.events(limit, kind=kind, model=model)}

    @app.get("/api/router/policy", dependencies=read)
    def router_policy():
        return {"policy": policy.current(), "version": policy.version(), "defaults": policy.DEFAULT,
                "task_classes": list(policy.TASK_CLASSES), "components": list(policy.COMPONENTS), "rule_kinds": list(policy.RULE_KINDS),
                "error_kinds": list(policy.ERROR_KINDS)}

    @app.put("/api/router/policy", dependencies=write)
    def router_policy_save(payload: dict = Body(...)):
        try:
            result = policy.save(payload.get("policy"), actor="dashboard", note=str(payload.get("note") or ""))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if result["changes"]:
            db.log_audit(actor="dashboard", action="router_policy", detail=f"v{result['version']}: " + "; ".join(result["changes"])[:450])
        return result

    @app.get("/api/router/policy/history", dependencies=read)
    def router_policy_history(limit: int = Query(50, ge=1, le=500)):
        return {"versions": policy.history(limit), "current": policy.version()}

    @app.post("/api/router/policy/rollback", dependencies=write)
    def router_policy_rollback(payload: dict = Body(...)):
        try:
            result = policy.rollback(int(payload.get("version", -1)), actor="dashboard")
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        db.log_audit(actor="dashboard", action="router_policy_rollback", detail=f"to v{payload.get('version')}")
        return result

    @app.get("/api/router/examples", dependencies=read)
    def router_examples(limit: int = Query(500, ge=1, le=5000)):
        return {"examples": learn.examples(limit)}

    @app.post("/api/router/examples", dependencies=write)
    def router_example_add(payload: dict = Body(...)):
        try:
            eid = learn.add_example(str(payload.get("text") or ""), task_class=payload.get("task_class") or None,
                                    preferred_model=payload.get("preferred_model") or None)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"id": eid}

    @app.delete("/api/router/examples/{example_id}", dependencies=write)
    def router_example_delete(example_id: int):
        if not learn.delete_example(example_id):
            raise HTTPException(status_code=404, detail="no such example")
        return {"ok": True}

    def _model(payload: dict) -> str:
        ref = str(payload.get("model") or "").strip()
        if "/" not in ref:
            raise HTTPException(status_code=400, detail="model must be provider/model (or provider/* for a whole provider)")
        return ref

    @app.post("/api/router/models/rest", dependencies=write)
    def router_model_rest(payload: dict = Body(...)):
        ref = _model(payload)
        try:
            seconds = float(payload.get("seconds", 3600))
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="seconds must be a number") from None
        if not 0 < seconds <= 365 * 86400:
            raise HTTPException(status_code=400, detail="seconds must be between 1 and a year")
        reason = str(payload.get("reason") or "rested by hand")[:300]
        result = learn.set_cooldown(ref, seconds, kind="manual", reason=reason, manual=True)
        store.event("cooldown", f"{ref} rested by hand for {learn._human(seconds)}: {reason}", model=ref, manual=True)
        db.log_audit(actor="dashboard", action="router_rest", detail=f"{ref} {int(seconds)}s")
        return result

    @app.post("/api/router/models/release", dependencies=write)
    def router_model_release(payload: dict = Body(...)):
        return {"released": learn.clear_cooldown(_model(payload))}

    @app.post("/api/router/models/forget", dependencies=write)
    def router_model_forget(payload: dict = Body(...)):
        ref = _model(payload)
        learn.reset_model(ref)
        db.log_audit(actor="dashboard", action="router_forget", detail=ref)
        return {"ok": True}

    @app.post("/api/router/reset", dependencies=write)
    def router_reset(payload: dict = Body(default={})):
        store.reset(keep_policy=bool(payload.get("keep_policy", True)), keep_examples=bool(payload.get("keep_examples", True)))
        from bot.router_brain import policy as pol

        pol.invalidate()
        store.event("reset", "the router's learning was reset from the dashboard")
        db.log_audit(actor="dashboard", action="router_reset", detail=json.dumps(payload)[:200])
        return {"ok": True}
