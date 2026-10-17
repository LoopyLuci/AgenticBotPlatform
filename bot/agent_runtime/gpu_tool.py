"""`gpu_status` tool: measured VRAM (total / used / free), the adapter, and whether a given size fits (bot/gpu.py)."""
from __future__ import annotations

import asyncio
import json

from bot.agent_runtime import toolspec
from bot.agent_runtime.errors import ToolError


async def _gpu_status(inp: dict, *, workspace=None, instance_id=None, device_tier=None) -> str:
    from bot import gpu

    need = inp.get("need_gb")
    try:
        need = None if need in (None, "") else float(need)
        headroom = float(inp.get("headroom_gb", 1.0))
    except (TypeError, ValueError):
        raise ToolError("need_gb and headroom_gb must be numbers") from None
    return json.dumps(await asyncio.to_thread(gpu.fit_report, need, headroom))


toolspec.register(
    {"name": "gpu_status",
     "description": "Measured video memory right now: total, used and free GB per GPU (works on AMD, NVIDIA and Intel). Pass need_gb to learn "
                    "whether that size fits with headroom (`fits`: true, false, or null when it cannot be measured). Call it before taking "
                    "the GPU lock and loading a model.",
     "input_schema": {"type": "object", "properties": {"need_gb": {"type": "number", "description": "size you want to load, in GB"},
                                                       "headroom_gb": {"type": "number", "description": "spare to keep, default 1"}},
                      "required": []}},
    toolspec.ToolSpec("gpu_status", "read", read_only=True, concurrency_safe=True, origin="registered"), _gpu_status)
