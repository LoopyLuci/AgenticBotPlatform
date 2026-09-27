"""Dashboard routes: mutations.

Moved verbatim out of bot/dashboard/server.py's build_app(); the route order inside is unchanged.
"""
from __future__ import annotations

from typing import Optional

from fastapi import Body, Depends, FastAPI, HTTPException

from bot import db, desktop
from bot.config import config
from bot.router import VALID_BACKENDS
from bot.support_bot import hybrid as support_bot_hybrid
from bot.support_bot import training_data
from bot.support_bot.engine import support_bot


def register(app: FastAPI) -> None:
    from bot.dashboard.server import _caller_device_id, _require_token, _require_token_or_api_key


    @app.post("/api/desktop/start", dependencies=[Depends(_require_token)])
    async def api_desktop_start():
        try:
            desktop.start()
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {"ok": True}

    @app.post("/api/desktop/stop", dependencies=[Depends(_require_token)])
    async def api_desktop_stop():
        desktop.stop()
        return {"ok": True}

    @app.post("/api/desktop/restart", dependencies=[Depends(_require_token)])
    async def api_desktop_restart():
        desktop.restart()
        return {"ok": True}

    @app.post("/api/config/reload", dependencies=[Depends(_require_token)])
    async def api_config_reload():
        changed, summary = config.reload(actor="dashboard")
        return {"changed": changed, "summary": summary, "version": config.version}

    @app.post("/api/config/set", dependencies=[Depends(_require_token_or_api_key)])
    async def api_config_set(payload: dict = Body(...)):
        path = payload.get("path")
        value = payload.get("value")
        if not path or not isinstance(path, list):
            raise HTTPException(status_code=400, detail="payload must be {path: [...], value: ...}")
        config.set_value(path, value, actor="dashboard")
        return {"ok": True, "version": config.version}

    @app.get("/api/snapshots", dependencies=[Depends(_require_token)])
    def api_snapshots_list():
        from bot import snapshots

        return {"snapshots": snapshots.list_snapshots()}

    @app.post("/api/snapshots", dependencies=[Depends(_require_token)])
    def api_snapshots_create(payload: dict = Body(default={})):
        from bot import snapshots

        manifest = snapshots.create_snapshot(label=(payload or {}).get("label") or None)
        db.log_audit(actor="dashboard", action="snapshot_create", detail=manifest["name"])
        return manifest

    @app.post("/api/snapshots/{name}/restore", dependencies=[Depends(_require_token)])
    def api_snapshots_restore(name: str):
        from bot import snapshots

        try:
            snapshots.restore_snapshot(name)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        db.log_audit(actor="dashboard", action="snapshot_restore", detail=name)
        return {"ok": True}

    @app.delete("/api/snapshots/{name}", dependencies=[Depends(_require_token)])
    def api_snapshots_delete(name: str):
        from bot import snapshots

        if not snapshots.delete_snapshot(name):
            raise HTTPException(status_code=404, detail=f"no snapshot named {name!r}")
        return {"ok": True}

    @app.post("/api/ui-customize/generate", dependencies=[Depends(_require_token)])
    async def api_ui_customize_generate(payload: dict = Body(...)):
        from bot import ui_customize

        target = payload.get("target")
        instruction = payload.get("instruction")
        if not target or not instruction:
            raise HTTPException(status_code=400, detail="payload must be {target, instruction}")
        try:
            return await ui_customize.generate_change(target, instruction)
        except ui_customize.UiCustomizeError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/ui-customize/apply", dependencies=[Depends(_require_token)])
    def api_ui_customize_apply(payload: dict = Body(...)):
        from bot import ui_customize

        change_id = payload.get("change_id")
        if not change_id:
            raise HTTPException(status_code=400, detail="payload must be {change_id}")
        try:
            entry = ui_customize.apply_change(change_id, actor="dashboard")
        except ui_customize.UiCustomizeError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        db.log_audit(actor="dashboard", action="ui_customize_apply", detail=f"{entry['target']}: {entry['instruction']}")
        return entry

    @app.get("/api/ui-customize/history", dependencies=[Depends(_require_token)])
    async def api_ui_customize_history(target: Optional[str] = None):
        from bot import ui_customize

        return {"history": ui_customize.list_history(target)}

    @app.post("/api/ui-customize/revert", dependencies=[Depends(_require_token)])
    def api_ui_customize_revert(payload: dict = Body(...)):
        from bot import ui_customize

        entry_id = payload.get("entry_id")
        if not entry_id:
            raise HTTPException(status_code=400, detail="payload must be {entry_id}")
        try:
            entry = ui_customize.revert_change(entry_id, actor="dashboard")
        except ui_customize.UiCustomizeError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        db.log_audit(actor="dashboard", action="ui_customize_revert", detail=f"{entry['target']}")
        return entry

    @app.post("/api/backend/{action_or_default}/{backend}", dependencies=[Depends(_require_token_or_api_key)])
    async def api_set_backend(action_or_default: str, backend: str):
        if backend not in VALID_BACKENDS:
            raise HTTPException(status_code=400, detail=f"unknown backend {backend!r}")
        if action_or_default == "default":
            # Delegates to bot.router.set_default_backend — shared with the
            # admin_set_default_backend agent-runtime tool so there's
            # exactly one implementation of "Claude and Hermes each keep
            # their own default-backend slot."
            from bot.router import set_default_backend

            return set_default_backend(backend, actor="dashboard")
        config.set_value(["action_overrides", action_or_default, "backend"], backend, actor="dashboard")
        return {"ok": True, "version": config.version}

    # ---------------------------------------------------------- support bot
    # The local, dependency-free management assistant (bot/support_bot/) —
    # same auth tier as /api/bots and /api/config/set since it can trigger
    # the same actions those routes do, just via natural language.

    @app.post("/api/support-bot/ask", dependencies=[Depends(_require_token_or_api_key)])
    async def api_support_bot_ask(payload: dict = Body(...), device_id: Optional[int] = Depends(_caller_device_id)):
        text = (payload.get("text") or "").strip()
        if not text:
            raise HTTPException(status_code=400, detail="payload must be {text: ...}")
        # Optional local-classification fast-path hint (Phase 6 of the
        # Support Bot NLU upgrade plan) — see SupportBot.handle()'s own
        # docstring for why this can never bypass real server-side
        # validation/gating.
        client_intent = (payload.get("client_intent") or "").strip() or None
        # Admin control surface plan, Section 4 — the calling device's
        # own permission_tier gates the new admin-shaped intents
        # (training_data.ADMIN_ONLY_INTENTS); the desktop dashboard
        # token itself is the unconditional top authority, same as
        # every other admin-surface enforcement point in this plan.
        from bot import server_chat_admin

        device_tier = "unrestricted" if device_id is None else server_chat_admin.resolve_device_tier(device_id)
        reply = await support_bot.handle(text, actor="support-bot", client_intent=client_intent, device_tier=device_tier)
        return {
            "text": reply.text,
            "intent": reply.intent,
            "needs_confirm": reply.needs_confirm,
            "confirm_token": reply.confirm_token,
            "applied": reply.applied,
        }

    @app.post("/api/support-bot/confirm", dependencies=[Depends(_require_token_or_api_key)])
    async def api_support_bot_confirm(payload: dict = Body(...)):
        token = (payload.get("token") or "").strip()
        if not token:
            raise HTTPException(status_code=400, detail="payload must be {token: ...}")
        reply = await support_bot.confirm(token, actor="support-bot")
        return {
            "text": reply.text,
            "intent": reply.intent,
            "needs_confirm": reply.needs_confirm,
            "confirm_token": reply.confirm_token,
            "applied": reply.applied,
        }

    # Training tab — user-added phrases for the Support Bot's hybrid
    # classifier (TF-IDF centroid model + trained neural network, see
    # bot/support_bot/hybrid.py), layered on top of training_data.py's
    # hand-authored baseline. Every mutation retrains both sub-models in
    # place so it takes effect immediately, no restart.
    @app.get("/api/support-bot/training", dependencies=[Depends(_require_token_or_api_key)])
    def api_support_bot_training_list():
        return {
            "phrases": [dict(r) for r in db.list_support_bot_phrases()],
            "intents": sorted({intent for _, intent in training_data.EXAMPLES}),
        }

    @app.post("/api/support-bot/training", dependencies=[Depends(_require_token_or_api_key)])
    def api_support_bot_training_add(payload: dict = Body(...)):
        # Accepts either a single {phrase, intent} (unchanged, existing
        # behavior) or a bulk {phrases: [{phrase, intent}, ...]} import —
        # Phase 3 of the Support Bot NLU upgrade plan. Either way,
        # retraining happens once at the end, not once per phrase.
        bulk = payload.get("phrases")
        items = bulk if isinstance(bulk, list) else [payload]
        # Validate every item BEFORE inserting any of them — a bulk
        # import must never partially apply just because a later item in
        # the batch turned out malformed.
        cleaned: list[tuple[str, str]] = []
        for item in items:
            phrase = (item.get("phrase") or "").strip()
            intent = (item.get("intent") or "").strip()
            if not phrase or not intent:
                raise HTTPException(status_code=400, detail="every item must be {phrase: str, intent: str}")
            cleaned.append((phrase, intent))
        added_ids = [db.add_support_bot_phrase(phrase, intent) for phrase, intent in cleaned]
        counts = support_bot_hybrid.retrain_all()
        db.log_audit(actor="dashboard", action="support_bot_phrase_add", detail=f"{len(added_ids)} phrase(s)")
        if bulk is not None:
            return {"ok": True, "ids": added_ids, "trained_on": counts}
        return {"ok": True, "id": added_ids[0], "trained_on": counts}

    @app.delete("/api/support-bot/training/{phrase_id}", dependencies=[Depends(_require_token_or_api_key)])
    def api_support_bot_training_delete(phrase_id: int):
        db.delete_support_bot_phrase(phrase_id)
        counts = support_bot_hybrid.retrain_all()
        db.log_audit(actor="dashboard", action="support_bot_phrase_delete", detail=f"id {phrase_id}")
        return {"ok": True, "trained_on": counts}

    # Explicit retrain-and-evaluate action (Phase 2/3 of the Support Bot
    # NLU upgrade plan) — distinct from the implicit retrain that already
    # happens on every add/delete above (which never gates on accuracy,
    # matching that existing behavior exactly). This one always runs the
    # held-out-accuracy regression gate, defaulting to a 2-point-accuracy
    # tolerance an operator can override.
    @app.post("/api/support-bot/retrain", dependencies=[Depends(_require_token_or_api_key)])
    def api_support_bot_retrain(payload: dict = Body(default={})):
        tolerance = payload.get("accept_if_regression_under", 0.02)
        result = support_bot_hybrid.retrain_all(accept_if_regression_under=tolerance)
        db.log_audit(
            actor="dashboard", action="support_bot_retrain",
            detail=f"accepted={result['accepted']}" + (f" reason={result.get('reason')}" if not result["accepted"] else ""),
        )
        return result

    # Synthetic training-data generation swarm (Phase 4 of the Support
    # Bot NLU upgrade plan) — free-model-only by hard constraint (see
    # bot/support_bot/synthetic_gen.py's own docstring). Results land in
    # a pending review queue, never directly in the live training set.
    @app.post("/api/support-bot/generate", dependencies=[Depends(_require_token_or_api_key)])
    async def api_support_bot_generate():
        from bot.support_bot import synthetic_gen

        result = await synthetic_gen.generate_synthetic_batch()
        db.log_audit(
            actor="dashboard", action="support_bot_generate",
            detail=f"dispatched={result['dispatched']} pending_added={result['pending_added']}",
        )
        return result

    # The scaling driver (next-generation modular hybrid plan, Phase 8) —
    # loops generate_synthetic_batch() until every targeted intent
    # reaches `target_per_intent` examples, `max_batches` is hit, or a
    # batch reports a budget refusal. `module_id` scopes a run to one
    # Knowledge Module at a time — resumable, since re-running later
    # picks up wherever counts currently stand.
    @app.post("/api/support-bot/generate/run", dependencies=[Depends(_require_token_or_api_key)])
    async def api_support_bot_generate_run(payload: dict = Body(default={})):
        from bot.support_bot import synthetic_gen

        target_per_intent = payload.get("target_per_intent", 20)
        module_id = payload.get("module_id")
        max_batches = payload.get("max_batches", synthetic_gen.DEFAULT_MAX_BATCHES)
        result = await synthetic_gen.run_until_target(
            target_per_intent, module_id=module_id, max_batches=max_batches,
        )
        db.log_audit(
            actor="dashboard", action="support_bot_generate_run",
            detail=(
                f"module={module_id!r} target={target_per_intent} batches={result['batches_run']} "
                f"pending_added={result['total_pending_added']} auto_approved={result['total_auto_approved']} "
                f"stopped={result['stopped_reason']!r}"
            ),
        )
        return result

    @app.get("/api/support-bot/pending", dependencies=[Depends(_require_token_or_api_key)])
    def api_support_bot_pending_list(status: str = "pending"):
        return [dict(r) for r in db.list_support_bot_pending_examples(status=status)]

    @app.post("/api/support-bot/pending/{pending_id}/approve", dependencies=[Depends(_require_token_or_api_key)])
    def api_support_bot_pending_approve(pending_id: int):
        row = db.get_support_bot_pending_example(pending_id)
        if row is None or row["status"] != "pending":
            raise HTTPException(status_code=404, detail="no such pending example")
        phrase_id = db.add_support_bot_phrase(row["phrase"], row["intent"])
        db.resolve_support_bot_pending_example(pending_id, "approved", resulting_phrase_id=phrase_id)
        counts = support_bot_hybrid.retrain_all()
        db.log_audit(actor="dashboard", action="support_bot_pending_approve", detail=f"id {pending_id}: {row['phrase']!r} -> {row['intent']}")
        return {"ok": True, "trained_on": counts}

    @app.post("/api/support-bot/pending/{pending_id}/reject", dependencies=[Depends(_require_token_or_api_key)])
    def api_support_bot_pending_reject(pending_id: int):
        row = db.get_support_bot_pending_example(pending_id)
        if row is None or row["status"] != "pending":
            raise HTTPException(status_code=404, detail="no such pending example")
        db.resolve_support_bot_pending_example(pending_id, "rejected")
        db.log_audit(actor="dashboard", action="support_bot_pending_reject", detail=f"id {pending_id}")
        return {"ok": True}

    # Undoes a previously-approved pending example (human OR auto-approved
    # — see synthetic_gen.py's 2-model-agreement rule, Phase 7 of the
    # next-generation modular hybrid plan) — deletes the exact live
    # phrase it created and retrains, so an operator can always walk back
    # an approval that turned out wrong, auto-approved or not.
    @app.post("/api/support-bot/pending/{pending_id}/revert", dependencies=[Depends(_require_token_or_api_key)])
    def api_support_bot_pending_revert(pending_id: int):
        row = db.get_support_bot_pending_example(pending_id)
        if row is None or row["status"] != "approved":
            raise HTTPException(status_code=404, detail="no such approved pending example")
        deleted_phrase_id = db.revert_support_bot_pending_example(pending_id)
        counts = support_bot_hybrid.retrain_all()
        db.log_audit(actor="dashboard", action="support_bot_pending_revert", detail=f"id {pending_id}: deleted phrase {deleted_phrase_id}")
        return {"ok": True, "trained_on": counts}

    # Active-learning review (Phase 5 of the Support Bot NLU upgrade
    # plan) — real classifications the hybrid model disagreed on or
    # couldn't decide, surfaced for an operator to assign the correct
    # intent directly (distinct from "add a phrase from scratch": this
    # starts from real, already-seen user text). Shares db.get_recent_misses()
    # with synthetic_gen.py's own targeting signal — one query, two consumers.
    @app.get("/api/support-bot/misses", dependencies=[Depends(_require_token_or_api_key)])
    def api_support_bot_misses():
        return [dict(r) for r in db.get_recent_misses(unreviewed_only=True)]

    @app.post("/api/support-bot/misses/{classification_id}/label", dependencies=[Depends(_require_token_or_api_key)])
    def api_support_bot_misses_label(classification_id: int, payload: dict = Body(...)):
        intent = (payload.get("intent") or "").strip()
        if not intent:
            raise HTTPException(status_code=400, detail="payload must be {intent: str}")
        row = db.get_conn().execute(
            "SELECT text FROM support_bot_classifications WHERE id=?", (classification_id,)
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="no such classification")
        phrase_id = db.add_support_bot_phrase(row["text"], intent)
        db.mark_support_bot_classification_reviewed(classification_id)
        counts = support_bot_hybrid.retrain_all()
        db.log_audit(actor="dashboard", action="support_bot_miss_label", detail=f"id {classification_id}: {row['text']!r} -> {intent}")
        return {"ok": True, "phrase_id": phrase_id, "trained_on": counts}

    # Self-monitoring: the hybrid classifier's own logged behavior over
    # real traffic — agreement rate between its two sub-models, unknown
    # rate, confidence trends — plus the currently-active model's own
    # recorded held-out eval (accuracy/per-intent P&R), if any retrain has
    # ever run through the eval-gated path. See bot/support_bot/hybrid.py's
    # health() and bot/support_bot/model_io.py's file schema.
    # The portable model file a Kotlin engine on Android loads for local,
    # on-device classification (Phase 6 of the Support Bot NLU upgrade
    # plan) — see bot/support_bot/hybrid.py's export_current_model() and
    # model_io.py's file schema. training_data_hash lets the app skip a
    # re-download when nothing has actually changed.
    @app.get("/api/support-bot/model", dependencies=[Depends(_require_token_or_api_key)])
    async def api_support_bot_model(module: Optional[str] = None):
        # `module` (next-generation modular hybrid plan, Phase 4) is
        # optional and additive: omitted, this route is byte-for-byte
        # what it's always been — the single global, unpartitioned
        # model. Given a real Knowledge Module id, returns that module's
        # OWN persisted model_io-shaped JSON instead, falling back to a
        # freshly-built export (same "never error, train on the spot"
        # contract as hybrid.export_current_model()'s own fallback) if
        # that module has never been retrain_module()'d yet.
        if module is None:
            return support_bot_hybrid.export_current_model()

        from bot.support_bot import cascade, knowledge_modules, model_io, module_manifest

        if knowledge_modules.MODULE_REGISTRY.get(module) is None:
            raise HTTPException(status_code=404, detail=f"no such Knowledge Module: {module!r}")
        persisted = model_io.load_model(path=module_manifest.module_path(module))
        if persisted is not None:
            return persisted
        pair = cascade.get_module_classifiers(module)
        module_examples = cascade._examples_for_module(module)
        nn_state = pair.nn.export_state() if hasattr(pair.nn, "export_state") else {}
        return {
            "format_version": model_io.FORMAT_VERSION,
            "training_data_hash": model_io.compute_training_hash(module_examples),
            "intents": sorted({intent for _, intent in module_examples}),
            "tfidf": pair.tfidf.export_state(),
            "nn": nn_state,
            "eval": {},
            "calibration": {},
        }

    # Knowledge Module enable/disable/retrain (next-generation modular
    # hybrid plan, Phase 9's reusable-surfaces) — the MCP tools proxy
    # these same three routes.
    @app.post("/api/support-bot/modules/{module_id}/enabled", dependencies=[Depends(_require_token_or_api_key)])
    def api_support_bot_module_set_enabled(module_id: str, payload: dict = Body(...)):
        from bot.support_bot import module_manifest

        enabled = payload.get("enabled")
        if not isinstance(enabled, bool):
            raise HTTPException(status_code=400, detail="payload must be {enabled: bool}")
        try:
            module_manifest.set_enabled(module_id, enabled)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        db.log_audit(actor="dashboard", action="support_bot_module_set_enabled", detail=f"{module_id}: {enabled}")
        return {"ok": True}

    @app.post("/api/support-bot/modules/{module_id}/retrain", dependencies=[Depends(_require_token_or_api_key)])
    def api_support_bot_module_retrain(module_id: str, payload: dict = Body(default={})):
        from bot.support_bot import cascade

        tolerance = payload.get("accept_if_regression_under")
        try:
            result = cascade.retrain_module(module_id, tolerance)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        db.log_audit(
            actor="dashboard", action="support_bot_module_retrain",
            detail=f"{module_id}: accepted={result['accepted']}" + (f" reason={result.get('reason')}" if not result["accepted"] else ""),
        )
        return result

    # The Knowledge Module list — every registered module's id, display
    # name, intents, and current runtime state (enabled/version) — so a
    # caller (the Android app deciding which per-module models to fetch,
    # or a future desktop admin panel) never has to hardcode the module
    # registry itself. Next-generation modular hybrid plan, Phase 4.
    @app.get("/api/support-bot/manifest", dependencies=[Depends(_require_token_or_api_key)])
    async def api_support_bot_manifest():
        from bot.support_bot import knowledge_modules, module_manifest

        manifest = module_manifest.load_manifest()
        return {
            module_id: {
                "display_name": spec.display_name,
                "description": spec.description,
                "intents": list(spec.intents),
                "unloadable": spec.unloadable,
                **manifest[module_id],
            }
            for module_id, spec in knowledge_modules.MODULE_REGISTRY.items()
        }

    # Tier 1 of the Support Bot NLU cascade (next-generation modular
    # hybrid plan) — a pure classify-only endpoint, distinct from
    # /api/support-bot/ask (which classifies AND executes). Always runs
    # the single global, unpartitioned hybrid.classify() — the same
    # always-freshest-trained model /api/support-bot/model without a
    # `module` param serves, which is exactly what makes this tier
    # useful to a caller whose own on-device Tier 0 module partition
    # came back "unknown": a full-corpus model sees every intent at
    # once, so it can resolve cases a module-partitioned client can't.
    # Deliberately does NOT accept or trust a caller-supplied intent —
    # unlike /ask's client_intent fast-path, this endpoint's entire job
    # is to classify, so it always computes its own answer.
    @app.post("/api/support-bot/classify", dependencies=[Depends(_require_token_or_api_key)])
    async def api_support_bot_classify(payload: dict = Body(...)):
        text = (payload.get("text") or "").strip()
        if not text:
            raise HTTPException(status_code=400, detail="payload must be {text: ...}")
        from bot.support_bot import cascade

        intent, confidence, source = await cascade.classify_full_cascade(text)
        current = support_bot_hybrid.export_current_model()
        return {
            "intent": intent,
            "confidence": confidence,
            "source": source,
            "server_model_version": current.get("training_data_hash"),
        }

    @app.get("/api/support-bot/health", dependencies=[Depends(_require_token_or_api_key)])
    async def api_support_bot_health():
        from bot.support_bot import model_io

        health = support_bot_hybrid.health()
        current = model_io.load_model(path=model_io.CURRENT_PATH)
        health["eval"] = (current or {}).get("eval") or {}
        return health

    @app.post("/api/mcp/{name}/enable", dependencies=[Depends(_require_token)])
    async def api_mcp_enable(name: str):
        try:
            desktop.enable_mcp(name)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {"ok": True}

    @app.post("/api/mcp/{name}/disable", dependencies=[Depends(_require_token)])
    async def api_mcp_disable(name: str):
        try:
            desktop.disable_mcp(name)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {"ok": True}

    @app.post("/api/mcp/self-register", dependencies=[Depends(_require_token)])
    async def api_mcp_self_register():
        return {"ok": True, **desktop.register_self_mcp(actor="dashboard")}

    @app.post("/api/security/allowed-users/{telegram_id}", dependencies=[Depends(_require_token)])
    def api_add_allowed_user(telegram_id: int, name: str = ""):
        db.add_allowed_user(telegram_id, name)
        db.log_audit(actor="dashboard", action="add_allowed_user", detail=str(telegram_id))
        return {"ok": True}

    @app.delete("/api/security/allowed-users/{telegram_id}", dependencies=[Depends(_require_token)])
    def api_remove_allowed_user(telegram_id: int):
        db.remove_allowed_user(telegram_id)
        db.log_audit(actor="dashboard", action="remove_allowed_user", detail=str(telegram_id))
        return {"ok": True}

    @app.post("/api/database/vacuum", dependencies=[Depends(_require_token)])
    def api_vacuum():
        db.vacuum()
        return {"ok": True}
