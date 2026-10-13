"""The agent's read-only `context_recall` tool, and the "Relevant context" block added to LOCAL-model turns.

The block is never added for API turns: the vault's job there is to take context out of them, not to put more in.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

logger = logging.getLogger("bot.context_vault")


def format_block(items: list[dict]) -> str:
    lines = ["Relevant context from earlier work (local memory; may be out of date):"]
    for it in items:
        text = " ".join(str(it.get("text") or "").split())
        lines.append(f"- [{it.get('kind')}] {text}")
    return "\n".join(lines)


def injection_sync(query: str, transport: Any, project: str = "") -> str:
    from bot import context_vault
    from bot.context_vault import settings

    cfg = settings.load()
    if not cfg.get("enabled") or not cfg.get("inject_for_local"):
        return ""
    from bot.agent_runtime import turn_budget

    if not turn_budget.is_local(transport):
        return ""
    vault = context_vault.get_vault()
    if vault is None or not (query or "").strip():
        return ""
    items = vault.recall(query, project=project or None, k=int(cfg.get("inject_k") or 6),
                         budget_tokens=int(cfg.get("inject_budget_tokens") or 600))
    return format_block(items) if items else ""


async def injection_for_turn(query: str, transport: Any, project: str = "") -> str:
    """The block for this turn ("" for API turns, when disabled, or on any trouble). Runs off the event loop."""
    try:
        from bot.context_vault import settings

        if not settings.enabled():
            return ""
        return await asyncio.to_thread(injection_sync, query, transport, project)
    except Exception:  # noqa: BLE001 - never breaks a turn
        logger.debug("context vault: injection failed", exc_info=True)
        return ""


def register_tools() -> None:
    from bot.agent_runtime import toolspec

    async def recall(inp, **_):
        from bot import context_vault

        vault = context_vault.get_vault()
        if vault is None:
            return "The context vault is off (context_vault.enabled)."
        kinds = inp.get("kinds") or None
        items = await asyncio.to_thread(
            vault.recall, str(inp.get("query") or ""), inp.get("project") or None, kinds,
            max(1, min(int(inp.get("k") or 5), 20)), int(inp.get("budget_tokens") or 1500))
        if not items:
            return "Nothing relevant in the context vault."
        return json.dumps([{k: it[k] for k in ("id", "kind", "text", "source", "project", "importance", "tier")}
                           for it in items], indent=1)[:20000]

    name = "context_recall"
    toolspec.register(
        {"name": name,
         "description": "Search the local context vault: decisions, plans, file summaries, error fixes, preferences and rules "
                        "captured from earlier work. Returns the best matches that fit budget_tokens.",
         "input_schema": {"type": "object", "properties": {
             "query": {"type": "string"}, "project": {"type": "string"},
             "kinds": {"type": "array", "items": {"type": "string"}},
             "k": {"type": "integer"}, "budget_tokens": {"type": "integer"}}, "required": ["query"]}},
        toolspec.ToolSpec(name, "read", read_only=True, concurrency_safe=True, origin="registered"), recall)


register_tools()
