"""The agent's read-only `memory_relevant` tool: which tools, skills and earlier episodes fit a goal, and what to avoid."""
from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger("bot.memory_graph")


def register_tools() -> None:
    from bot.agent_runtime import toolspec

    async def relevant(inp, *, workspace=None, **_):
        from bot.memory_graph import recall, settings

        cfg = settings.load()
        if not cfg.get("enabled"):
            return "The memory graph is off (memory_graph.enabled)."
        files = inp.get("files") if isinstance(inp.get("files"), list) else None
        k = max(1, min(int(inp.get("k") or cfg["recall_k"]), 15))
        try:
            result = await asyncio.to_thread(recall.recall_for_task, str(inp.get("goal") or ""), str(inp.get("project") or ""),
                                             files, root=workspace, cfg=cfg, k=k)
        except Exception as exc:  # noqa: BLE001
            logger.debug("memory graph: recall failed", exc_info=True)
            return f"Memory graph unavailable: {type(exc).__name__}"
        return recall.format_text(result, int(cfg["max_output_chars"]))

    name = "memory_relevant"
    toolspec.register(
        {"name": name,
         "description": "Before starting a task: which tools and skills worked on similar tasks, similar earlier work, and what "
                        "failed (so you can avoid it). Built from past runs and their outcomes; short output.",
         "input_schema": {"type": "object", "properties": {
             "goal": {"type": "string"}, "project": {"type": "string"},
             "files": {"type": "array", "items": {"type": "string"}}, "k": {"type": "integer"}}, "required": ["goal"]}},
        toolspec.ToolSpec(name, "read", read_only=True, concurrency_safe=True, origin="registered"), relevant)


register_tools()
