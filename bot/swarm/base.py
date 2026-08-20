"""Shared shape for every swarm strategy — mirrors bot/backends/base.py's
Backend/BackendResult pattern: one small interface, one result dataclass,
so bot/swarm/engine.py can dispatch to any strategy without special-casing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from bot import db
from bot.backends.base import BackendError
from bot.router import router


@dataclass
class SwarmRunResult:
    status: str  # 'success' | 'failed' | 'partial'
    result: Optional[str]
    steps: list[dict[str, Any]] = field(default_factory=list)
    error: Optional[str] = None


class SwarmStrategyError(Exception):
    pass


class SwarmStrategy:
    name: str = "base"

    async def run(self, swarm_row: dict[str, Any], prompt: str, *, swarm_run_id: str, user_id: int = 0) -> SwarmRunResult:
        raise NotImplementedError


async def run_member(
    instance_id: int,
    prompt: str,
    *,
    swarm_run_id: str,
    action_type: str = "quick_question",
    user_id: int = 0,
) -> str:
    """Thin wrapper every strategy calls instead of router.ask() directly —
    keeps the swarm_run_id/action_type plumbing in one place. Raises
    BackendError on failure (same as router.ask()); callers decide whether
    one member failing aborts the whole run or is tolerated."""
    result = await router.ask(
        prompt,
        action_type=action_type,
        user_id=user_id,
        instance_id=instance_id,
        swarm_run_id=swarm_run_id,
    )
    return result.text


def instance_label(instance_id: int) -> str:
    from bot import bot_instances

    inst = bot_instances.get_instance(instance_id)
    return inst["name"] if inst else f"instance {instance_id}"
