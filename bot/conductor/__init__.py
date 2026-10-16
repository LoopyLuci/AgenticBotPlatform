"""The local conductor (LF4): plan, work, review and integrate without Claude. See docs/local-conductor.md.

    planner.py     a goal -> small board tasks (stable keys, deps, resource scopes, a proof each); a local model, strict JSON,
                   validated and repaired, with a deterministic fallback
    reviewer.py    a diff against the owner's standing rules before any merge (no feature removed first); optional local model review
    integrator.py  green task branches -> an integ/agents-N branch, the full suite, flake vs failure, the push queue
    run.py         orchestration: plan -> board -> workers (the swarm board runner) -> review -> integrate; status, queue, approval
    store.py       plans, reviews, integrations, the push queue and escalations (SQLite in the agent state folder)
    llm.py         the one place a model is called: local models only (ModelMistress first, then Ollama), never a cloud model

Nothing here pushes or deploys. A green integration branch waits in the push queue for an approval.
"""
from __future__ import annotations

import sys
from typing import Any

DEFAULTS: dict[str, Any] = {
    "model": "",                       # a local model ref (provider/model); "" = the first local model the router offers
    "review_model": "",                # same for the model review; "" = a local model other than the planner's when there is one
    "max_tasks": 8,                    # tasks one plan may have
    "max_rejections": 2,               # review rejections of one task before it is escalated
    "model_review": False,             # add a local-model review after the static checks pass
    "review_diff_cap": 12000,          # characters of diff a model review is shown
    "public_repo": True,               # apply the public-repo leak rules to every diff
    "check_hotreload": True,
    "check_ruff": True,
    "suite_command": [],               # [] = python -m pytest tests -n 8 -q -p no:cacheprovider -rf
    "suite_timeout_s": 3600,
    "base_branch": "main",
}


def settings() -> dict[str, Any]:
    cfg: dict[str, Any] = {}
    try:
        from bot.config import config

        cfg = dict(config.current.get("conductor") or {})
    except Exception:  # noqa: BLE001 - config trouble must not stop the conductor
        pass
    out = {**DEFAULTS, **{k: v for k, v in cfg.items() if k in DEFAULTS}}
    if not out["suite_command"]:
        out["suite_command"] = [sys.executable, "-m", "pytest", "tests", "-n", "8", "-q", "-p", "no:cacheprovider", "-rf"]
    return out
