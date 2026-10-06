"""Dashboard routes: rules - the sandboxed rule language, over HTTP.

    GET    /api/rules/status                     is an engine installed, and what it enforces
    GET    /api/rules                            every rule, with whether it can run here
    GET    /api/rules/{id}                       one rule, plus what has happened to it
    POST   /api/rules                            {name, source, kind, target?, ...}
    PUT    /api/rules/{id}                       any of the same fields
    DELETE /api/rules/{id}
    POST   /api/rules/validate                   {source, kind} - compile it and say where not
    POST   /api/rules/test                       {source, kind, context} - run it on a sample
    POST   /api/rules/{id}/test                  {context} - run a saved rule on a sample
    POST   /api/rules/{id}/enabled               {enabled}

Everything a page needs is here and nothing here is a page: creating, editing, checking and trying a
rule are four separate calls, so an editor can validate as you type without ever saving, and a "try
it" button can run an unsaved rule against a context the page built. A later UI is a pure client of
this.

Reading follows the dashboard's normal auth (the desktop token or a paired device's key), so a phone
can see which rules exist. Everything else needs the dashboard token itself: a rule is
operator configuration that changes what runs unattended, which is the same trust boundary hooks and
routines already sit behind (see bot/agent_runtime/hooks.py's own note on why they are never
agent-creatable).

When no engine is installed every write is a 503 with the reason, and the reads still work - so the
configuration is not lost and a page can say what is missing rather than showing a broken editor.
"""
from __future__ import annotations

from typing import Optional

from fastapi import Body, Depends, FastAPI, HTTPException, Query

from bot import rules


def _body(payload: Optional[dict]) -> dict:
    return payload if isinstance(payload, dict) else {}


def _engine_required() -> None:
    """Refuse to store a rule when there is nothing that could ever run it. Silently accepting it
    and never enforcing it is the one outcome worse than an error."""
    if not rules.available():
        raise HTTPException(status_code=503, detail=rules.DISABLED_MESSAGE)


def register(app: FastAPI) -> None:
    from bot.dashboard.server import _require_token, _require_token_or_api_key

    read = [Depends(_require_token_or_api_key)]
    write = [Depends(_require_token)]

    def _audit(action: str, detail: str) -> None:
        from bot import db

        try:
            db.log_audit(actor="dashboard", action=action, detail=detail[:500])
        except Exception:  # noqa: BLE001 - the audit log must never be the reason a rule stops working
            pass

    def _created(rule) -> dict:
        _audit("rule_create", f"rule {rule.id} ({rule.name}, {rule.kind}, target {rule.target or '-'})")
        return {"rule": rules.store.describe(rule)}

    def _rule_or_404(rule_id: int):
        rule = rules.store.get(rule_id)
        if rule is None:
            raise HTTPException(status_code=404, detail=f"no rule with id {rule_id}")
        return rule

    def _bad(exc: Exception) -> HTTPException:
        # A rule that does not compile is a 422 with its position, not a 400 with a flat sentence:
        # the page puts the caret at line/column rather than making the person count characters.
        problem = getattr(exc, "problem", None)
        if problem is None and isinstance(exc, rules.RuleError):
            problem = exc.as_dict()
        if problem is not None:
            return HTTPException(status_code=422, detail=problem)
        return HTTPException(status_code=400, detail=str(exc))

    @app.get("/api/rules/status", dependencies=read)
    async def rules_status():
        """Is there a rule engine, which one, and exactly what does it refuse to let a rule do."""
        return rules.status()

    @app.get("/api/rules", dependencies=read)
    async def list_rules(kind: Optional[str] = Query(default=None, description="condition | filter | route"),
                         target: Optional[str] = Query(default=None, description="only rules bound to this target"),
                         instance_id: Optional[int] = Query(default=None, description="only this bot's rules")):
        """Every rule, ordered by name, each saying whether it can actually run here and why not."""
        try:
            rows = rules.store.listing(kind, target=target, instance_id=instance_id)
        except rules.RuleStoreError as exc:
            raise _bad(exc) from exc
        return {"rules": [rules.store.describe(r) for r in rows], "status": rules.status()}

    @app.get("/api/rules/{rule_id}", dependencies=read)
    async def get_rule(rule_id: int):
        """One rule, its source, what it is bound to, and the last thing it said."""
        return {"rule": rules.store.describe(_rule_or_404(rule_id))}

    @app.post("/api/rules", dependencies=write, status_code=201)
    async def create_rule(payload: dict = Body(...)):
        """Save a rule. The source is compiled first: a rule that does not compile is not saved."""
        _engine_required()
        body = _body(payload)
        try:
            rule = rules.store.create(str(body.get("name") or ""), str(body.get("source") or ""),
                                      str(body.get("kind") or "condition"),
                                      description=str(body.get("description") or ""),
                                      target=str(body.get("target") or ""),
                                      instance_id=body.get("instance_id"),
                                      enabled=bool(body.get("enabled", True)))
        except (rules.RuleStoreError, rules.RuleError) as exc:
            raise _bad(exc) from exc
        return _created(rule)

    @app.put("/api/rules/{rule_id}", dependencies=write)
    async def update_rule(rule_id: int, payload: dict = Body(...)):
        """Change a rule. Only the fields in the request change; everything else is left alone, and
        a source that would not compile leaves the stored rule exactly as it was."""
        _rule_or_404(rule_id)
        _engine_required()
        body = _body(payload)
        changes = {k: body[k] for k in ("name", "source", "kind", "description", "target", "instance_id", "enabled")
                   if k in body}
        try:
            rule = rules.store.update(rule_id, **changes)
        except (rules.RuleStoreError, rules.RuleError) as exc:
            raise _bad(exc) from exc
        _audit("rule_update", f"rule {rule_id} ({rule.name}), {sorted(changes)}")
        return {"rule": rules.store.describe(rule)}

    @app.post("/api/rules/{rule_id}/enabled", dependencies=write)
    async def set_rule_enabled(rule_id: int, payload: dict = Body(default={})):
        """Turn a rule on or off without deleting it - the same thing as a paused schedule."""
        _rule_or_404(rule_id)
        try:
            rule = rules.store.set_enabled(rule_id, bool(_body(payload).get("enabled", True)))
        except rules.RuleStoreError as exc:
            raise _bad(exc) from exc
        _audit("rule_enabled", f"rule {rule_id} ({rule.name}), enabled={rule.enabled}")
        return {"rule": rules.store.describe(rule)}

    @app.delete("/api/rules/{rule_id}", dependencies=write)
    async def delete_rule(rule_id: int):
        """Delete the rule. Nothing is kept; whatever was gated by it goes back to ungated."""
        rule = _rule_or_404(rule_id)
        try:
            rules.store.delete(rule_id)
        except rules.RuleStoreError as exc:
            raise _bad(exc) from exc
        _audit("rule_delete", f"rule {rule_id} ({rule.name})")
        return {"deleted": rule.name, "id": rule_id}

    @app.post("/api/rules/validate", dependencies=write)
    async def validate_rule(payload: dict = Body(...)):
        """Compile a rule without saving it. `{source, kind}` -> ok, or a 422 naming the line and
        column. This is what an editor calls as you type."""
        _engine_required()
        body = _body(payload)
        try:
            return rules.validate(str(body.get("source") or ""), str(body.get("kind") or "condition"))
        except rules.RuleError as exc:
            raise _bad(exc) from exc

    @app.post("/api/rules/test", dependencies=write)
    async def test_rule(payload: dict = Body(...)):
        """Run a rule against a sample context, saved or not. `{source, kind, context}` -> what it
        answered and how long it took. With no engine this is a 503 rather than a silent pass."""
        _engine_required()
        body = _body(payload)
        context = body.get("context") if isinstance(body.get("context"), dict) else {}
        try:
            return rules.test(str(body.get("source") or ""), str(body.get("kind") or "condition"), context)
        except rules.RuleError as exc:
            raise _bad(exc) from exc

    @app.post("/api/rules/{rule_id}/test", dependencies=write)
    async def test_saved_rule(rule_id: int, payload: dict = Body(default={})):
        """Run a saved rule against a sample context, filling in whatever the request left out with a
        context a person can recognise: the fields below are exactly what `bot.rules.context` puts in
        a real one, so a "try it" in a page is the same evaluation the scheduler will do."""
        rule = _rule_or_404(rule_id)
        _engine_required()
        body = _body(payload)
        context = body.get("context") if isinstance(body.get("context"), dict) else {}
        sample = rules.build_context(message=context.get("message", "the weekly digest"),
                                     sender=context.get("sender", "alex"),
                                     sender_id=context.get("sender_id", 7),
                                     platform=context.get("platform", "telegram"),
                                     action_type=context.get("action_type", "scheduled"),
                                     instance_id=context.get("instance_id", rule.instance_id),
                                     bot=context.get("bot", "assistant"),
                                     tags=context.get("tags") or [],
                                     extra=context.get("extra") or {})
        sample.update({k: v for k, v in context.items() if isinstance(k, str) and isinstance(v, (str, int, float, bool))})
        try:
            out = rules.test(rule.source, rule.kind, sample)
        except rules.RuleError as exc:
            raise _bad(exc) from exc
        out["rule"] = rule.name
        out["context"] = sample
        return out