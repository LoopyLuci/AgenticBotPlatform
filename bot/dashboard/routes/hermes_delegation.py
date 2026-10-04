"""Dashboard routes: Hermes delegation.

Moved verbatim out of bot/dashboard/server.py's build_app(); the route order inside is unchanged.

Also hosts the `/api/hermes/gateway/*` routes, which drive Hermes Agent's OWN
messaging gateway (bot/hermes_gateway.py) — the thing that keeps serving the
user's Telegram bot directly while AgenticBotPlatform runs its own channels
beside it. Registered first inside this area so the literal `/api/hermes/gateway/...`
and `/api/hermes/ask` paths are matched before the `{instance_id}` ones.
"""
from __future__ import annotations

import asyncio
import os

from fastapi import Body, Depends, FastAPI, HTTPException

from bot import bot_instances, db, envfile
from bot.config import config
from bot.router import router


def register(app: FastAPI) -> None:
    from bot.dashboard.server import _require_token_or_api_key

    # ---------------------------------------------- the user's own gateway ---
    # `hermes gateway status|list|start|stop|restart`, `logs`, and a one-shot
    # `hermes -z`. Every route below shells out to the real CLI windowless and
    # returns Hermes's own parsed output plus a fingerprint-only view of the
    # token that home owns — never the token itself.

    def _gateway_home(home: str = "") -> str:
        """Resolve the home to act on: the one named in the request, else this
        machine's default Hermes home. Rejecting an unreadable path here (rather
        than deep inside a subprocess) keeps the 400 honest."""
        from bot import hermes_gateway

        if not (home or "").strip():
            return str(hermes_gateway.default_home())
        return home.strip()

    def _gateway_error(exc: Exception) -> HTTPException:
        from bot.hermes_gateway import GatewayError

        status = 503 if isinstance(exc, GatewayError) else 500
        return HTTPException(status_code=status, detail=str(exc))

    @app.get("/api/hermes/gateway", dependencies=[Depends(_require_token_or_api_key)])
    async def api_hermes_gateway_overview(home: str = ""):
        """Everything one Hermes-gateway page needs: parsed status, the Telegram
        ownership picture for this home, the launcher/login item to reuse for a
        start, every configured home, and a log tail."""
        from bot import hermes_gateway

        target = _gateway_home(home)
        return {
            "overview": await asyncio.to_thread(hermes_gateway.overview, target),
            "status": await asyncio.to_thread(hermes_gateway.status, target),
            "profiles": await asyncio.to_thread(hermes_gateway.list_profiles),
            "served_instances": hermes_gateway.served_instances(),
            "logs": await asyncio.to_thread(hermes_gateway.logs, target, 80),
        }

    @app.get("/api/hermes/gateway/status", dependencies=[Depends(_require_token_or_api_key)])
    async def api_hermes_gateway_status(home: str = ""):
        from bot import hermes_gateway

        try:
            return await asyncio.to_thread(hermes_gateway.status, _gateway_home(home))
        except Exception as exc:
            raise _gateway_error(exc) from exc

    @app.get("/api/hermes/gateway/list", dependencies=[Depends(_require_token_or_api_key)])
    async def api_hermes_gateway_list():
        from bot import hermes_gateway

        try:
            return {"profiles": await asyncio.to_thread(hermes_gateway.list_profiles)}
        except Exception as exc:
            raise _gateway_error(exc) from exc

    @app.post("/api/hermes/gateway/{action}", dependencies=[Depends(_require_token_or_api_key)])
    async def api_hermes_gateway_action(action: str, payload: dict = Body(default={})):
        """start/stop/restart. start reuses Hermes's own login-item launcher so
        the gateway outlives both AgenticBotPlatform and any Windows Job Object
        wrapping it (see bot/hermes_gateway.start)."""
        from bot import hermes_gateway

        if action not in ("start", "stop", "restart"):
            raise HTTPException(status_code=404, detail=f"unknown gateway action {action!r}")
        try:
            result = await asyncio.to_thread(getattr(hermes_gateway, action), _gateway_home(payload.get("home", "")))
        except Exception as exc:
            raise _gateway_error(exc) from exc
        db.log_audit(actor="dashboard", action=f"hermes_gateway_{action}",
                     detail=str(result.get("home") or _gateway_home(payload.get("home", ""))))
        return result

    @app.get("/api/hermes/gateway/logs", dependencies=[Depends(_require_token_or_api_key)])
    async def api_hermes_gateway_logs(home: str = "", lines: int = 80):
        from bot import hermes_gateway

        return await asyncio.to_thread(hermes_gateway.logs, _gateway_home(home), lines)

    @app.post("/api/hermes/ask", dependencies=[Depends(_require_token_or_api_key)])
    async def api_hermes_ask(payload: dict = Body(...)):
        """One-shot `hermes -z` — how ABP's own channels reach Hermes while its
        gateway serves Telegram. A separate per-call process, so no token
        contention with the gateway's getUpdates poller."""
        from bot import hermes_gateway

        text = (payload.get("text") or "").strip()
        if not text:
            raise HTTPException(status_code=400, detail="text is required")
        home = (payload.get("hermes_home") or "").strip()
        session_id = (payload.get("session_id") or "").strip() or None
        if payload.get("instance_id") is not None:
            instance = bot_instances.get_instance(int(payload["instance_id"]))
            if instance is None:
                raise HTTPException(status_code=404, detail=f"bot instance {payload['instance_id']} not found")
            home = home or (instance.get("hermes_home") or "").strip()
            session_id = session_id or instance.get("desktop_session_key")
        try:
            return await hermes_gateway.ask(text, home=home or None, model=payload.get("model") or None, session_id=session_id)
        except Exception as exc:
            raise _gateway_error(exc) from exc

    # Configures and drives Hermes Agent's own delegate_task sub-agent
    # system (see bot/hermes_config.py's module docstring for exactly why
    # this can only ever set config + send a prompt, never invoke
    # delegate_task directly). Until per-instance HERMES_HOME isolation
    # lands, every hermes_gateway-backed instance shares one
    # ~/.hermes/config.yaml — a real, documented limitation, not hidden.

    def _require_hermes_gateway_instance(instance_id: int) -> dict:
        instance = bot_instances.get_instance(instance_id)
        if instance is None:
            raise HTTPException(status_code=404, detail=f"bot instance {instance_id} not found")
        if instance.get("backend") != "hermes_gateway":
            raise HTTPException(
                status_code=400,
                detail=f"instance {instance_id} is backed by {instance.get('backend')!r}, not hermes_gateway — "
                       "delegation configuration only applies to Hermes gateway-backed instances",
            )
        return instance

    def _require_hermes_backed_instance(instance_id: int) -> dict:
        """Looser than _require_hermes_gateway_instance — accepts both
        Hermes backends. MCP-server registration is a property of the
        underlying `hermes` install's own config.yaml, not of which
        subprocess-management strategy AgenticBotPlatform uses to talk to it, so
        registering agentic-bot-platform's MCP server works identically for
        hermes_cli (no gateway/eviction needed at all — hermes_cli spawns
        a fresh `hermes -z` process per call, which re-reads config.yaml
        fresh every time) and hermes_gateway (needs the eviction dance
        since its persistent process only reads config at spawn time)."""
        instance = bot_instances.get_instance(instance_id)
        if instance is None:
            raise HTTPException(status_code=404, detail=f"bot instance {instance_id} not found")
        if instance.get("backend") not in ("hermes_cli", "hermes_gateway"):
            raise HTTPException(
                status_code=400,
                detail=f"instance {instance_id} is backed by {instance.get('backend')!r}, not a Hermes backend "
                       "(hermes_cli/hermes_gateway)",
            )
        return instance

    def _require_native_agent_instance(instance_id: int) -> dict:
        instance = bot_instances.get_instance(instance_id)
        if instance is None:
            raise HTTPException(status_code=404, detail=f"bot instance {instance_id} not found")
        if instance.get("backend") != "native_agent":
            raise HTTPException(
                status_code=400,
                detail=f"instance {instance_id} is backed by {instance.get('backend')!r}, not native_agent — "
                       "dispatch_native_swarm_goal only applies to native_agent-backed instances",
            )
        return instance

    @app.get("/api/hermes/{instance_id}/delegation", dependencies=[Depends(_require_token_or_api_key)])
    async def api_hermes_delegation_get(instance_id: int):
        from bot import hermes_config

        instance = _require_hermes_gateway_instance(instance_id)
        return {"delegation": hermes_config.read_delegation_config(instance.get("hermes_home"))}

    @app.post("/api/hermes/{instance_id}/delegation", dependencies=[Depends(_require_token_or_api_key)])
    async def api_hermes_delegation_set(instance_id: int, payload: dict = Body(...)):
        from bot import hermes_config

        instance = _require_hermes_gateway_instance(instance_id)
        if payload.get("subagent_auto_approve") is True and not payload.get("confirm"):
            raise HTTPException(
                status_code=400,
                detail="subagent_auto_approve=true lets sub-agents run dangerous commands "
                       "(shell, file writes) with no human approval — pass confirm=true to acknowledge this.",
            )
        delegation = hermes_config.set_delegation_config(
            provider=payload.get("provider"),
            model=payload.get("model"),
            max_concurrent_children=payload.get("max_concurrent_children"),
            max_spawn_depth=payload.get("max_spawn_depth"),
            subagent_auto_approve=payload.get("subagent_auto_approve"),
            reasoning_effort=payload.get("reasoning_effort"),
            hermes_home=instance.get("hermes_home"),
            actor="dashboard",
        )
        return {"ok": True, "delegation": delegation}

    @app.get("/api/hermes/{instance_id}/agent-config", dependencies=[Depends(_require_token_or_api_key)])
    async def api_hermes_agent_config_get(instance_id: int):
        from bot import hermes_config

        instance = _require_hermes_gateway_instance(instance_id)
        return {"agent": hermes_config.read_agent_config(instance.get("hermes_home"))}

    @app.post("/api/hermes/{instance_id}/agent-config", dependencies=[Depends(_require_token_or_api_key)])
    async def api_hermes_agent_config_set(instance_id: int, payload: dict = Body(...)):
        from bot import hermes_config

        instance = _require_hermes_gateway_instance(instance_id)
        agent_cfg = hermes_config.set_agent_config(
            reasoning_effort=payload.get("reasoning_effort"),
            hermes_home=instance.get("hermes_home"),
            actor="dashboard",
        )
        return {"ok": True, "agent": agent_cfg}

    @app.post("/api/hermes/{instance_id}/dispatch", dependencies=[Depends(_require_token_or_api_key)])
    async def api_hermes_dispatch(instance_id: int, payload: dict = Body(...)):
        """Configures delegation defaults for this instance (if a
        worker_provider/worker_model was given, or one was auto-picked as
        the currently-cheapest free model) then sends a goal prompt asking
        Hermes's own agent to use delegate_task to fan it out — see
        bot/swarm/prompts.py's module docstring for why the prompt itself,
        not an external RPC, is the actual dispatch mechanism."""
        from bot import hermes_config, swarm_budget
        from bot.config import config
        from bot.models import hermes_models_with_pricing
        from bot.swarm.child_parser import parse_child_breakdown
        from bot.swarm.prompts import hermes_delegation_goal

        instance = _require_hermes_gateway_instance(instance_id)
        goal = (payload.get("goal") or "").strip()
        if not goal:
            raise HTTPException(status_code=400, detail="payload must include a non-empty 'goal'")

        worker_provider = payload.get("worker_provider")
        worker_model = payload.get("worker_model")
        priced, pricing_source = await hermes_models_with_pricing(instance_id)
        if not worker_provider or not worker_model:
            for provider_name, entries in sorted(priced.items()):
                free_entry = next((e for e in sorted(entries, key=lambda e: e["id"]) if e["free"]), None)
                if free_entry:
                    worker_provider, worker_model = provider_name, free_entry["id"]
                    break

        max_children = payload.get("max_children")
        pricing_row = next(
            (e for e in priced.get(worker_provider, []) if e["id"] == worker_model), None
        ) if worker_provider else None
        decision = swarm_budget.check_budget(
            pricing_row=pricing_row,
            max_children=max_children,
            confirm=bool(payload.get("confirm")),
            cfg=(config.current.get("swarm_budget") or {}),
        )
        if not decision.allowed:
            db.log_audit(
                actor="dashboard", action="swarm_dispatch_blocked",
                detail=f"instance {instance_id} ({worker_provider}/{worker_model}): {decision.reason}",
            )
            raise HTTPException(status_code=400, detail=decision.reason)

        if worker_provider and worker_model:
            hermes_config.set_delegation_config(
                provider=worker_provider, model=worker_model,
                max_concurrent_children=max_children, hermes_home=instance.get("hermes_home"),
                actor="dashboard",
            )

        prompt = hermes_delegation_goal(
            goal, worker_provider=worker_provider, worker_model=worker_model, max_children=max_children,
        )
        audit_id = db.log_audit(
            actor="dashboard", action="swarm_dispatch",
            detail=f"instance {instance_id} -> {worker_provider}/{worker_model}: {goal[:120]}"
            + (f" (est. ${decision.estimated_usd:.4f})" if decision.estimated_usd else ""),
        )
        try:
            result = await router.ask(prompt, action_type="swarm_dispatch", instance_id=instance_id)
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"dispatch failed: {exc}") from exc

        job_row = db.get_latest_job(instance_id, "swarm_dispatch")
        if job_row is not None:
            db.set_audit_log_job_id(audit_id, job_row["id"])
            children = parse_child_breakdown(result.text)
            if children is not None:
                db.set_job_children(job_row["id"], children)

        return {
            "ok": True,
            "result": result.text,
            "worker_provider": worker_provider,
            "worker_model": worker_model,
            "worker_model_source": pricing_source,
            "estimated_usd": decision.estimated_usd,
            "job_id": job_row["id"] if job_row is not None else None,
        }

    @app.post("/api/native-agent/{instance_id}/dispatch", dependencies=[Depends(_require_token_or_api_key)])
    async def api_native_agent_dispatch(instance_id: int, payload: dict = Body(...)):
        """The native_agent equivalent of api_hermes_dispatch — but with
        no goal-prompt-and-hope indirection: the caller already provides
        the decomposed task list (this route's own caller — an MCP tool
        call from Claude, or the dashboard operator — plays the role a
        goal-prompt template plays for an external Hermes instance,
        deciding the subtask breakdown up front), so this calls
        bot.agent_runtime.subagents.run_batch() directly. Results land in
        job_children/job_tool_events exactly like a Hermes-external
        dispatch, so the dashboard's Delegation Activity panel works
        unmodified for either kind of fan-out."""
        from bot import swarm_budget
        from bot.agent_runtime import subagents
        from bot.backends.base import BackendError
        from bot.models import custom_models_with_pricing

        _require_native_agent_instance(instance_id)
        tasks = payload.get("tasks")
        if not isinstance(tasks, list) or not tasks:
            raise HTTPException(status_code=400, detail="payload must include a non-empty 'tasks' array")

        worker_provider = payload.get("worker_provider")
        worker_model = payload.get("worker_model")
        priced, pricing_source = await custom_models_with_pricing()
        if not worker_provider or not worker_model:
            for provider_name, entries in sorted(priced.items()):
                free_entry = next((e for e in sorted(entries, key=lambda e: e["id"]) if e["free"]), None)
                if free_entry:
                    worker_provider, worker_model = provider_name, free_entry["id"]
                    break

        max_children = payload.get("max_children")
        pricing_row = next(
            (e for e in priced.get(worker_provider, []) if e["id"] == worker_model), None
        ) if worker_provider else None
        decision = swarm_budget.check_budget(
            pricing_row=pricing_row,
            max_children=max_children or len(tasks),
            confirm=bool(payload.get("confirm")),
            cfg=(config.current.get("swarm_budget") or {}),
        )
        if not decision.allowed:
            db.log_audit(
                actor="dashboard", action="swarm_dispatch_blocked",
                detail=f"instance {instance_id} native ({worker_provider}/{worker_model}): {decision.reason}",
            )
            raise HTTPException(status_code=400, detail=decision.reason)

        role = payload.get("role", "leaf")
        job_id = db.create_job(
            action_type="swarm_dispatch", backend="native_agent", user_id=0,
            prompt=f"native dispatch: {len(tasks)} task(s)", instance_id=instance_id,
        )
        db.mark_job_running(job_id, backend="native_agent")
        audit_id = db.log_audit(
            actor="dashboard", action="swarm_dispatch",
            detail=f"instance {instance_id} native -> {worker_provider}/{worker_model}: {len(tasks)} task(s)"
            + (f" (est. ${decision.estimated_usd:.4f})" if decision.estimated_usd else ""),
        )
        db.set_audit_log_job_id(audit_id, job_id)

        try:
            dispatch_result = await subagents.run_batch(
                tasks, role=role, provider=worker_provider, model=worker_model,
                effort=payload.get("worker_effort"),
                max_children=max_children, parent_instance_id=instance_id,
            )
        except BackendError as exc:
            db.mark_job_done(job_id, status="failed", error=str(exc))
            raise HTTPException(status_code=502, detail=f"dispatch failed: {exc}") from exc

        results = dispatch_result["children"]
        db.set_job_children(job_id, results)
        final_text = "\n\n".join(f"[{r['status']}] {r['goal']}: {r['result_excerpt']}" for r in results)
        db.mark_job_done(job_id, status="success", result=final_text)

        return {
            "ok": True,
            "result": final_text,
            "children": results,
            "dispatch_id": dispatch_result["dispatch_id"],
            "worker_provider": worker_provider,
            "worker_model": worker_model,
            "worker_model_source": pricing_source,
            "estimated_usd": decision.estimated_usd,
            "job_id": job_id,
        }

    @app.post("/api/hermes/{instance_id}/enable-swarm-tools", dependencies=[Depends(_require_token_or_api_key)])
    async def api_hermes_enable_swarm_tools(instance_id: int):
        """Gives this Hermes instance's own agent the same cross-instance
        organizing ability Claude gets via this MCP server and api-backend
        agents get via delegate_to_instance: registers agentic-bot-platform's own
        MCP server into the instance's mcp_servers config. For
        hermes_gateway this also evicts the cached backend so the NEXT
        call spawns a fresh gateway process that actually loads it
        (mcp_servers are read at gateway startup, never hot-reloaded);
        hermes_cli needs no eviction at all — it spawns a fresh `hermes
        -z` process per call, which re-reads config.yaml fresh every
        time, so the change is already live on the very next message."""
        from bot import hermes_config

        instance = _require_hermes_backed_instance(instance_id)
        token = os.environ.get("DASHBOARD_TOKEN") or envfile.get_var("DASHBOARD_TOKEN")
        registration = hermes_config.register_agenticbotplatform_mcp_server(
            hermes_home=instance.get("hermes_home"), dashboard_token=token, actor="dashboard",
        )
        note = "takes effect on this instance's next message"
        if instance.get("backend") == "hermes_gateway":
            await router.evict_backend(
                "hermes_gateway", model_override=instance.get("model"), hermes_home=instance.get("hermes_home")
            )
            note += " (fresh gateway spawn)"
        return {"ok": True, "registration": registration, "note": note}

    @app.post("/api/hermes/{instance_id}/disable-swarm-tools", dependencies=[Depends(_require_token_or_api_key)])
    async def api_hermes_disable_swarm_tools(instance_id: int):
        from bot import hermes_config

        instance = _require_hermes_backed_instance(instance_id)
        removed = hermes_config.unregister_agenticbotplatform_mcp_server(hermes_home=instance.get("hermes_home"), actor="dashboard")
        if removed and instance.get("backend") == "hermes_gateway":
            await router.evict_backend(
                "hermes_gateway", model_override=instance.get("model"), hermes_home=instance.get("hermes_home")
            )
        return {"ok": True, "removed": removed}

    @app.get("/api/hermes/swarm-tools-status", dependencies=[Depends(_require_token_or_api_key)])
    def api_hermes_swarm_tools_status():
        """For the dashboard's swarm-tools panel: every Hermes-backed
        instance (hermes_cli or hermes_gateway) with whether it currently
        has agentic-bot-platform's MCP server registered in its own config (see
        hermes_config.is_agenticbotplatform_mcp_registered) — a config-file read,
        not a live "is the running gateway actually connected to it"
        check, since that would require spawning/probing every
        instance's gateway just to render a panel."""
        from bot import hermes_config

        rows = []
        for instance in bot_instances.list_instances():
            if instance.get("backend") not in ("hermes_cli", "hermes_gateway"):
                continue
            rows.append({
                "id": instance["id"],
                "name": instance["name"],
                "backend": instance["backend"],
                "hermes_home": instance.get("hermes_home"),
                "swarm_tools_enabled": hermes_config.is_agenticbotplatform_mcp_registered(instance.get("hermes_home")),
            })
        return {"instances": rows}
