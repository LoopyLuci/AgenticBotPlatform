"""The dashboard's ABP Agents page: settings, an overview with readiness checks, and the tool inventory.

Settings come from bot/agent_runtime/settings_schema.py, which describes every setting once; the page renders its forms
from `GET /api/agent/config/schema` and saves through `POST /api/agent/config`, which validates every change and writes
nothing unless all of them are valid. Saves keep config/backends.yaml's comments (Config.set_values).

Permissions, skills, per-bot agent settings and swarms already have their own routes; this module only adds what the
page needs that did not exist: the whole `native_agent` configuration, one overview, and a tool list.
"""
from __future__ import annotations

from typing import Any, Callable

from fastapi import Body, Depends, FastAPI, HTTPException

# Backends whose bots run ABP's own tool-using agent loop (bot/agent_runtime). native_agent is the full one.
AGENT_BACKENDS = {
    "native_agent": "ABP Agent",
    "api": "Claude API (ABP agent loop)",
    "custom_model": "Custom model (ABP agent loop)",
}


def _first_sentence(text: str, limit: int = 220) -> str:
    text = " ".join((text or "").split())
    cut = text.find(". ")
    text = text[: cut + 1] if 0 < cut < limit else text[:limit]
    return text


def register(app: FastAPI, read_auth: Callable, write_auth: Callable) -> None:
    read = [Depends(read_auth)]
    write = [Depends(write_auth)]

    from bot.agent_runtime import settings_schema

    @app.get("/api/agent/config/schema", dependencies=read)
    async def config_schema():
        return settings_schema.describe()

    @app.get("/api/agent/config", dependencies=read)
    async def get_config():
        from bot.config import config

        cfg = config.current
        return {"values": settings_schema.current_values(cfg), "configured": settings_schema.configured_ids(cfg),
                "version": config.version}

    @app.post("/api/agent/config", dependencies=write)
    async def set_config(payload: dict = Body(...)):
        changes = payload.get("changes")
        if not isinstance(changes, dict) or not changes:
            raise HTTPException(status_code=400, detail="send {changes: {<setting id>: <value>, ...}}")
        values, errors = settings_schema.apply(changes, actor="dashboard")
        if errors:
            raise HTTPException(status_code=422, detail={"errors": errors})
        from bot import db
        from bot.config import config

        db.log_audit(actor="dashboard", action="agent_config_set", detail=", ".join(sorted(changes))[:500])
        return {"values": values, "configured": settings_schema.configured_ids(config.current), "version": config.version}

    @app.post("/api/agent/config/reset", dependencies=write)
    async def reset_config(payload: dict = Body(...)):
        ids = payload.get("ids")
        if not isinstance(ids, list) or not ids:
            raise HTTPException(status_code=400, detail="send {ids: [<setting id>, ...]}")
        from bot import db
        from bot.config import config

        values = settings_schema.reset([str(i) for i in ids], actor="dashboard")
        db.log_audit(actor="dashboard", action="agent_config_reset", detail=", ".join(str(i) for i in ids)[:500])
        return {"values": values, "configured": settings_schema.configured_ids(config.current), "version": config.version}

    @app.get("/api/agent/router/recommend", dependencies=read)
    async def router_recommend(task: str = ""):
        from bot import model_router

        cls, ranked, skipped = model_router.recommend(task or "a general coding and agent task")
        return {
            "task_class": cls.task_class, "reasons": cls.reasons,
            "recommendations": [
                {"model": r.model, "score": r.score, "quality": r.quality, "quality_source": r.quality_source,
                 "economy": r.economy, "headroom": r.headroom, "price_per_mtok": r.price_per_mtok,
                 "context": r.context, "reasons": r.reasons}
                for r in ranked
            ],
            "skipped": skipped,
        }

    @app.get("/api/agent/tools", dependencies=read)
    async def tool_inventory():
        from bot.agent_runtime import toolspec, tools

        out = []
        for schema in tools.all_tool_schemas():
            name = schema.get("name", "")
            spec = toolspec.spec_for(name)
            asks = spec.needs_approval if spec.needs_approval is not None else not spec.read_only
            out.append({"name": name, "description": _first_sentence(schema.get("description", "")),
                        "permission": spec.permission, "read_only": spec.read_only, "asks_first": bool(asks),
                        "origin": spec.origin})
        out.sort(key=lambda t: (t["permission"], t["name"]))
        return {"tools": out}

    @app.get("/api/agent/overview", dependencies=read)
    async def overview():
        from bot import agent_settings, bot_instances, db, providers, skill_install, skill_packs
        from bot.agent_runtime import permissions, skill_learning, tools
        from bot.config import config

        cfg = (config.current.get("native_agent") or {})
        bots: list[dict[str, Any]] = []
        for inst in bot_instances.list_instances():
            if inst.get("backend") not in AGENT_BACKENDS:
                continue
            mode, rules, allow_bypass = permissions.effective(inst["id"])
            settings = agent_settings.get(inst["id"])
            bots.append({
                "id": inst["id"], "name": inst["name"], "platform": inst["platform"], "enabled": bool(inst.get("enabled")),
                "backend": inst["backend"], "backend_label": AGENT_BACKENDS[inst["backend"]], "model": inst.get("model"),
                "permission_mode": mode, "permission_mode_own": permissions.instance_settings(inst["id"])["mode"],
                "permission_rules": len(rules),
                "max_concurrent_children": settings["max_concurrent_children"], "worker_model": settings["worker_model"],
                "worker_effort": settings["worker_effort"], "manager_effort": settings["manager_effort"],
                "plan_approval": bool(settings["require_plan_approval"]), "is_admin": bool(settings["is_admin_instance"]),
            })
        provider_count = len(providers.list_providers())
        swarm_rows = db.list_swarms()
        packs = skill_packs.discover(None)
        quarantined = skill_install.list_quarantine()
        drafts = skill_learning.list_drafts()
        schemas = tools.all_tool_schemas()
        budget = config.current.get("swarm_budget") or {}
        default_mode = ((cfg.get("permissions") or {}).get("mode")) or "default"
        sandbox_backend = ((cfg.get("sandbox") or {}).get("backend")) or "local"

        checks = [
            {"id": "provider", "ok": provider_count > 0, "title": "A model provider is set up",
             "detail": f"{provider_count} provider{'s' if provider_count != 1 else ''} configured." if provider_count
             else "Add a provider (an API key or a local server) on the Models page so an agent has a model to use.",
             "go": "models"},
            {"id": "bot", "ok": bool(bots), "title": "A bot uses an ABP Agent",
             "detail": f"{len(bots)} bot{'s' if len(bots) != 1 else ''} run an ABP agent." if bots
             else "Create a bot on the Bots page and choose the ABP Agent backend.", "go": "bots"},
            {"id": "permissions", "ok": default_mode != "bypass", "title": "Approvals are on",
             "detail": ("Bypass mode is the default: agents run every tool without asking." if default_mode == "bypass"
                        else f"Default permission mode is '{default_mode}'."), "go": "agents:safety"},
            {"id": "sandbox", "ok": True, "title": f"Commands run {'in Docker' if sandbox_backend == 'docker' else 'locally'}",
             "detail": ("Shell commands run in a container." if sandbox_backend == "docker"
                        else "Shell commands run on this computer with its own access. Docker isolates them (Safety tab)."),
             "go": "agents:safety", "info": True},
            {"id": "budget", "ok": bool(budget.get("enabled", True)), "title": "Swarm spending guard",
             "detail": "Swarms are checked against a cost limit before they start." if budget.get("enabled", True)
             else "The guard is off: a swarm can spend without a limit.", "go": "agents:subagents"},
            {"id": "review", "ok": not (quarantined or drafts), "title": "Nothing waiting for your review",
             "detail": (f"{len(quarantined)} skill pack{'s' if len(quarantined) != 1 else ''} and {len(drafts)} skill "
                        f"draft{'s' if len(drafts) != 1 else ''} are waiting." if (quarantined or drafts) else "No skills to review."),
             "go": "agents:skills"},
        ]
        return {
            "bots": bots, "checks": checks,
            "counts": {"providers": provider_count, "tools": len(schemas), "swarms": len(swarm_rows),
                       "skill_packs": len(packs), "skills_to_review": len(quarantined) + len(drafts)},
            "permission_mode": default_mode, "sandbox": sandbox_backend,
            "backends": [{"id": k, "label": v} for k, v in AGENT_BACKENDS.items()],
        }
