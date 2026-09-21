"""Tools that let the agent know its own model and choose others (roadmap PM).

`model_info`   what a model can do and how much of its allowance is left (default: the one running now)
`find_models`  catalogued models that fit a need (free, big enough context, tools, vision...), each with
               its current headroom, so a delegated task can be given a model that is not used up
"""
from __future__ import annotations

import json

from bot.agent_runtime import toolspec
from bot.agent_runtime.errors import ToolError


def _target(inp: dict) -> tuple[str, str]:
    from bot.agent_runtime import usage_limits

    ref = str(inp.get("model") or "").strip()
    if ref:
        if "/" not in ref:
            raise ToolError("give the model as provider/model, for example openrouter/qwen/qwen3.8-27b:free")
        provider, _, model = ref.partition("/")
        return provider, model
    current = usage_limits.current_model.get()
    if not current:
        raise ToolError("no model is running in this context; pass model as provider/model")
    return current


def quota(provider: str, model: str) -> dict:
    """What is left of an allowance, in plain fields."""
    from bot.agent_runtime import usage_limits

    snap = usage_limits.snapshot(provider, model)
    out = {"used_last_24h": {"calls": snap["calls_24h"], "tokens": snap["tokens_24h"], "rate_limited": snap["rate_limited_24h"]},
           "windows": [{"limit": w["name"], "used": w["used"], "of": w["limit"], "left": w["remaining"],
                        "frees_up_at": usage_limits.show_time(w["resets_at"]), "in": usage_limits.show_span(w["resets_in_s"])}
                       for w in snap["windows"]],
           "headroom": None if snap["headroom"] is None else round(snap["headroom"], 2)}
    if snap["blocked_until"]:
        out["blocked_until"] = usage_limits.show_time(snap["blocked_until"])
    if snap["reported"]:
        out["provider_reported"] = {k: v for k, v in snap["reported"].items() if k != "seen_at"}
    return out


async def _model_info(inp: dict, *, workspace=None, instance_id=None, device_tier=None) -> str:
    from bot import model_catalog

    provider, model = _target(inp)
    info = model_catalog.lookup(provider, model)
    body = info.to_dict()
    body.pop("description", None)
    body["quota"] = quota(provider, model)
    return json.dumps(body, default=str)


async def _find_models(inp: dict, *, workspace=None, instance_id=None, device_tier=None) -> str:
    from bot import model_catalog
    from bot.agent_runtime import usage_limits

    needs = tuple(str(n) for n in (inp.get("needs") or []) if str(n) in ("tools", "reasoning", "vision", "structured"))
    rows = model_catalog.search(provider=str(inp.get("provider") or ""), free_only=bool(inp.get("free_only")),
                                min_context=int(inp.get("min_context") or 0), needs=needs, query=str(inp.get("query") or ""),
                                limit=int(inp.get("limit") or 15))
    for r in rows:
        r["headroom"] = usage_limits.headroom(r["provider"], r["model"])
    rows.sort(key=lambda r: (r["headroom"] is not None and r["headroom"] <= 0))    # used-up models last, order kept otherwise
    return json.dumps(rows) if rows else "No catalogued model matches. (The catalog may not be downloaded yet: /modelinfo refresh.)"


def register_all() -> None:
    toolspec.register(
        {"name": "model_info",
         "description": "What a model can do (context window, output limit, tools, vision, reasoning, price, knowledge cutoff) and how much of its "
                        "free-tier allowance is left (requests and tokens per minute and per day, when it frees up). Defaults to the model you "
                        "are running on; pass model as provider/model for another. Check this before a big fan-out on a free model.",
         "input_schema": {"type": "object", "properties": {"model": {"type": "string", "description": "provider/model; omit for yours"}}, "required": []}},
        toolspec.ToolSpec("model_info", "read", read_only=True, concurrency_safe=True, origin="registered"), _model_info)
    toolspec.register(
        {"name": "find_models",
         "description": "Find catalogued models that fit a need, largest context first, each with its current headroom (None = no known limit; "
                        "0 = used up right now). Use it to pick a model for a delegated task.",
         "input_schema": {"type": "object", "properties": {
             "provider": {"type": "string"}, "query": {"type": "string", "description": "part of the model id or name"},
             "free_only": {"type": "boolean"}, "min_context": {"type": "integer"},
             "needs": {"type": "array", "items": {"type": "string", "enum": ["tools", "reasoning", "vision", "structured"]}},
             "limit": {"type": "integer"}}, "required": []}},
        toolspec.ToolSpec("find_models", "read", read_only=True, concurrency_safe=True, origin="registered"), _find_models)


register_all()
