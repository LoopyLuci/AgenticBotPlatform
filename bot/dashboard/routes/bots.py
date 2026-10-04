"""Dashboard routes: bots.

Moved verbatim out of bot/dashboard/server.py's build_app(); the route order inside is unchanged.
"""
from __future__ import annotations

from typing import Optional

from fastapi import Body, Depends, FastAPI, HTTPException

from bot import bot_instances, db, platform_supervisor
from bot import commands as bot_commands
from bot.router import router


def register(app: FastAPI) -> None:
    from bot.dashboard.server import (
        _identify_caller,
        _require_token,
        _require_token_or_api_key,
        _require_token_or_api_key_or_peer,
    )

    # DB-backed bot instances — replaces the fixed one-per-platform model.
    # Token-gated, no bootstrap exception (bot instance management is never
    # needed before DASHBOARD_TOKEN itself exists) — but unlike most write
    # routes, create/update/delete/restart accept a mobile device key too,
    # not just the desktop token: full parity with the desktop dashboard's
    # Bots tab, including submitting/editing platform bot tokens from the
    # phone. See _identify_caller()'s docstring for the tradeoff.

    @app.get("/api/platform-guides")
    async def api_platform_guides():
        from bot.platform_guides import PLATFORM_GUIDES

        return PLATFORM_GUIDES

    @app.post("/api/validate-field", dependencies=[Depends(_require_token_or_api_key)])
    async def api_validate_field(payload: dict = Body(...)):
        from bot.validators import validate_field

        ok, message = validate_field(payload.get("platform", ""), payload.get("field", ""), payload.get("value", ""))
        return {"ok": ok, "message": message}

    @app.get("/api/bots", dependencies=[Depends(_require_token_or_api_key_or_peer)])
    async def api_bots_list(caller: str = Depends(_identify_caller)):
        from bot.router import router as _router

        live = platform_supervisor.status()
        rows = bot_instances.list_instances()
        for row in rows:
            row["live_running"] = live.get(row["id"], {}).get("running", False)
            row["circuit"] = _router.circuit_status(row["id"])
            # "" unless a running Hermes gateway owns this instance's Telegram
            # token, in which case ABP is deliberately not polling it - see
            # bot/hermes_gateway.py. Resolved per row (not just from a running
            # task) so a freshly started dashboard already shows it.
            row["served_by"] = platform_supervisor.served_by(row["id"])
            row["live_status"] = live.get(row["id"], {}).get("status", "")
        if caller in ("peer", "integration"):
            rows = [bot_instances.redact_credentials(row) for row in rows]
        return rows

    @app.post("/api/bots/{instance_id}/circuit/reset", dependencies=[Depends(_require_token_or_api_key)])
    async def api_bots_circuit_reset(instance_id: int):
        from bot.router import router as _router

        _router.reset_circuit(instance_id)
        db.log_audit(actor="dashboard", action="circuit_breaker_reset", detail=f"instance {instance_id}")
        return {"ok": True}

    @app.get("/api/bots/backups", dependencies=[Depends(_require_token)])
    def api_bots_backups():
        return bot_instances.list_backups()

    @app.post("/api/bots/backups/{name}/restore", dependencies=[Depends(_require_token)])
    def api_bots_restore(name: str):
        try:
            bot_instances.restore_backup(name, actor="dashboard")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {"ok": True}

    @app.get("/api/bots/{instance_id}", dependencies=[Depends(_require_token_or_api_key)])
    def api_bots_get(instance_id: int, caller: str = Depends(_identify_caller)):
        row = bot_instances.get_instance(instance_id)
        if row is None:
            raise HTTPException(status_code=404, detail=f"bot instance {instance_id} not found")
        row["served_by"] = platform_supervisor.served_by(instance_id)
        return bot_instances.redact_credentials(row) if caller in ("peer", "integration") else row

    @app.get("/api/bots/{instance_id}/profile", dependencies=[Depends(_require_token_or_api_key)])
    def api_bots_profile(instance_id: int):
        """This instance's own identity/instructions as markdown — see
        bot.bot_instances.render_profile_markdown and the get_my_profile/
        get_agent_profile tools that read the exact same thing."""
        markdown = bot_instances.render_profile_markdown(instance_id)
        if markdown is None:
            raise HTTPException(status_code=404, detail=f"bot instance {instance_id} not found")
        return {"instance_id": instance_id, "markdown": markdown}

    @app.get("/api/bots/{instance_id}/model-picker", dependencies=[Depends(_require_token_or_api_key)])
    async def api_bots_model_picker(instance_id: int, provider: Optional[int] = None, page: int = 0):
        """The exact same two-level provider/model picker data Telegram's
        interactive /model command already renders as an inline keyboard
        (see bot/handlers.py's _model_providers_page/_model_page) — exposed
        over HTTP so any client that isn't Telegram (the Android app's own
        Chat screen) can build an equivalent native picker instead of only
        ever seeing bot.commands.cmd_model's plain-text global summary."""
        data = await bot_commands.instance_model_page(instance_id, provider, page)
        if data is None:
            raise HTTPException(status_code=404, detail=f"bot instance {instance_id} not found")
        return data

    @app.post("/api/bots/{instance_id}/model", dependencies=[Depends(_require_token_or_api_key)])
    async def api_bots_set_model(instance_id: int, payload: dict = Body(...), caller: str = Depends(_identify_caller)):
        model = (payload.get("model") or "").strip()
        if not model:
            raise HTTPException(status_code=400, detail="payload must be {model: <str>}")
        if bot_instances.get_instance(instance_id) is None:
            raise HTTPException(status_code=404, detail=f"bot instance {instance_id} not found")
        message = await bot_commands.apply_instance_model(instance_id, model, actor=caller)
        return {"ok": True, "message": message}

    @app.post("/api/bots", dependencies=[Depends(_require_token_or_api_key)])
    async def api_bots_create(payload: dict = Body(...)):
        try:
            instance_id = bot_instances.create_instance(
                name=payload.get("name", ""),
                platform=payload.get("platform", ""),
                backend=payload.get("backend", "cli"),
                credentials=payload.get("credentials") or {},
                allowed_user_ids=payload.get("allowed_user_ids") or [],
                admin_user_ids=payload.get("admin_user_ids") or [],
                action_overrides=payload.get("action_overrides") or {},
                can_target=payload.get("can_target") or [],
                enabled=bool(payload.get("enabled", True)),
                model=payload.get("model") or None,
                custom_instructions=payload.get("custom_instructions") or None,
                persona=payload.get("persona") or None,
                hermes_home=payload.get("hermes_home") or None,
                takeover_when_gateway_down=bool(payload.get("takeover_when_gateway_down", False)),
                actor="dashboard",
            )
        except bot_instances.ValidationError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        row = bot_instances.get_instance(instance_id)
        if row and row["enabled"]:
            # The row is saved either way — a bad token/connection issue
            # surfaces as a start failure recorded on the row itself
            # (bot_instances.last_error), not a lost bot.
            try:
                await platform_supervisor.start_instance(row)
            except Exception:
                pass
        return {"ok": True, "id": instance_id}

    @app.put("/api/bots/{instance_id}", dependencies=[Depends(_require_token_or_api_key)])
    async def api_bots_update(instance_id: int, payload: dict = Body(...)):
        fields = {
            k: v
            for k, v in payload.items()
            if k in ("name", "platform", "backend", "enabled", "credentials", "allowed_user_ids", "admin_user_ids", "action_overrides", "can_target", "model", "custom_instructions", "persona", "hermes_home", "desktop_project", "desktop_workspace_dir", "desktop_effort", "takeover_when_gateway_down")
        }
        before = bot_instances.get_instance(instance_id)
        try:
            bot_instances.update_instance(instance_id, actor="dashboard", **fields)
        except bot_instances.ValidationError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        # A hermes_gateway backend that changed model/hermes_home gets a
        # brand-new cache slot (see Router._get_backend) — the OLD backend
        # object, still holding its spawned `hermes serve` subprocess, is
        # otherwise orphaned forever under its now-unreachable old cache
        # key. Evict it so its subprocess actually gets terminated.
        if (
            before
            and before.get("backend") == "hermes_gateway"
            and ("model" in fields or "hermes_home" in fields)
            and (fields.get("model", before.get("model")) != before.get("model")
                 or fields.get("hermes_home", before.get("hermes_home")) != before.get("hermes_home"))
        ):
            await router.evict_backend("hermes_gateway", model_override=before.get("model"), hermes_home=before.get("hermes_home"))
        return {"ok": True}

    @app.delete("/api/bots/{instance_id}", dependencies=[Depends(_require_token_or_api_key)])
    async def api_bots_delete(instance_id: int):
        await platform_supervisor.stop_instance(instance_id)
        instance = bot_instances.get_instance(instance_id)
        try:
            bot_instances.delete_instance(instance_id, actor="dashboard")
        except bot_instances.ValidationError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        if instance and instance.get("backend") == "hermes_gateway":
            await router.evict_backend(
                "hermes_gateway", model_override=instance.get("model"), hermes_home=instance.get("hermes_home")
            )
        return {"ok": True}

    @app.post("/api/bots/{instance_id}/enable", dependencies=[Depends(_require_token_or_api_key_or_peer)])
    async def api_bots_enable(instance_id: int):
        try:
            bot_instances.enable_instance(instance_id, actor="dashboard")
        except bot_instances.ValidationError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        row = bot_instances.get_instance(instance_id)
        if row:
            await platform_supervisor.start_instance(row)
        return {"ok": True}

    @app.post("/api/bots/{instance_id}/disable", dependencies=[Depends(_require_token_or_api_key_or_peer)])
    async def api_bots_disable(instance_id: int):
        try:
            bot_instances.disable_instance(instance_id, actor="dashboard")
        except bot_instances.ValidationError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        await platform_supervisor.stop_instance(instance_id)
        return {"ok": True}

    @app.post("/api/bots/{instance_id}/start", dependencies=[Depends(_require_token_or_api_key_or_peer)])
    async def api_bots_start(instance_id: int):
        row = bot_instances.get_instance(instance_id)
        if row is None:
            raise HTTPException(status_code=404, detail=f"bot instance {instance_id} not found")
        try:
            await platform_supervisor.start_instance(row)
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"failed to start: {exc}") from exc
        return {"ok": True}

    @app.post("/api/bots/{instance_id}/stop", dependencies=[Depends(_require_token_or_api_key_or_peer)])
    async def api_bots_stop(instance_id: int):
        await platform_supervisor.stop_instance(instance_id)
        return {"ok": True}

    @app.post("/api/bots/{instance_id}/restart", dependencies=[Depends(_require_token_or_api_key_or_peer)])
    async def api_bots_restart(instance_id: int):
        await platform_supervisor.restart_instance(instance_id)
        return {"ok": True}

    @app.post("/api/bots/{instance_id}/session/new", dependencies=[Depends(_require_token_or_api_key)])
    async def api_bots_new_session(instance_id: int):
        # Opens a real new chat in Claude Desktop / Hermes for this instance
        # and links it — see router.create_session(). Only ui/hermes_gateway
        # backends support this; other backends 400.
        from bot.backends.base import BackendError
        from bot.router import router

        if bot_instances.get_instance(instance_id) is None:
            raise HTTPException(status_code=404, detail=f"bot instance {instance_id} not found")
        try:
            key = await router.create_session(instance_id)
        except BackendError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"ok": True, "desktop_session_key": key}
