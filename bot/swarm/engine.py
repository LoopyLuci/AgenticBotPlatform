"""Dispatches a swarm run to its configured strategy and records the
result. Each individual member call already lands in the `jobs` table
tagged with `swarm_run_id` (via bot.swarm.base.run_member ->
router.ask(..., swarm_run_id=...)), so the Jobs tab needs no swarm-aware
code path — this module only owns the `swarm_runs` row's lifecycle.

A run can take minutes (multiple sequential/parallel backend calls), so
the dashboard's POST /api/swarms/{id}/run doesn't await it synchronously
like /api/chat/send does — start_swarm_run() creates the DB row and
schedules the real work as a detached asyncio.Task, returning the
swarm_run_id immediately so the caller can poll GET
/api/swarms/runs/{swarm_run_id} for progress. _active_tasks tracks those
detached tasks so a run can be cancelled (best-effort).
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from typing import Any

from bot import db
from bot.swarm.base import SwarmRunResult, SwarmStrategyError
from bot.swarm.strategies import STRATEGIES

logger = logging.getLogger("bot.swarm.engine")

_active_tasks: dict[str, asyncio.Task] = {}


def _row_to_dict(row) -> dict[str, Any]:
    d = dict(row)
    d["config"] = json.loads(d["config"])
    d["enabled"] = bool(d["enabled"])
    return d


def start_swarm_run(swarm_id: int, prompt: str, user_id: int = 0, requested_by: str = "dashboard") -> str:
    """Synchronous — validates the swarm exists, creates the swarm_runs
    row, and schedules the actual run as a background task. Returns the
    new swarm_run_id immediately."""
    row = db.get_swarm(swarm_id)
    if row is None:
        raise ValueError(f"swarm {swarm_id} not found")
    swarm = _row_to_dict(row)
    if swarm["strategy"] not in STRATEGIES:
        raise ValueError(f"unknown swarm strategy {swarm['strategy']!r}")

    swarm_run_id = uuid.uuid4().hex
    db.create_swarm_run(swarm_id=swarm_id, swarm_run_id=swarm_run_id, prompt=prompt, requested_by=requested_by)
    db.log_audit(actor=requested_by, action="swarm_run_start", detail=f"swarm={swarm['name']!r} run={swarm_run_id}")

    task = asyncio.create_task(_execute(swarm, swarm_run_id, prompt, user_id))
    task.add_done_callback(lambda _t: _active_tasks.pop(swarm_run_id, None))
    _active_tasks[swarm_run_id] = task
    return swarm_run_id


async def _execute(swarm: dict[str, Any], swarm_run_id: str, prompt: str, user_id: int) -> None:
    strategy_cls = STRATEGIES[swarm["strategy"]]
    try:
        result: SwarmRunResult = await strategy_cls().run(swarm, prompt, swarm_run_id=swarm_run_id, user_id=user_id)
        db.update_swarm_run(
            swarm_run_id, status=result.status, result=result.result, error=result.error,
            steps_json=json.dumps(result.steps), finished=True,
        )
    except asyncio.CancelledError:
        db.update_swarm_run(swarm_run_id, status="cancelled", error="cancelled", finished=True)
        raise
    except SwarmStrategyError as exc:
        db.update_swarm_run(swarm_run_id, status="failed", error=str(exc), steps_json=json.dumps([]), finished=True)
    except Exception as exc:
        # Anything unexpected still has to end in a terminal row — a
        # detached task's exception otherwise vanishes silently.
        logger.exception("swarm run %s crashed", swarm_run_id)
        db.update_swarm_run(swarm_run_id, status="failed", error=f"internal error: {exc}", finished=True)


def cancel_run(swarm_run_id: str) -> bool:
    """Best-effort — cancels the asyncio.Task if it's still running.
    Individual in-flight router.ask() calls (a subprocess, an HTTP
    request) may not stop instantly."""
    task = _active_tasks.get(swarm_run_id)
    if task is None or task.done():
        return False
    task.cancel()
    return True


def swarm_run_to_dict(row) -> dict[str, Any]:
    d = dict(row)
    d["steps"] = json.loads(d["steps"]) if d.get("steps") else []
    return d
