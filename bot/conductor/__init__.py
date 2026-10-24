"""The local conductor (LF4): plan, work, review and integrate without Claude. See docs/local-conductor.md.

    planner.py     a goal -> small board tasks (stable keys, deps, resource scopes, a proof each); a local model, strict JSON,
                   validated and repaired, with a deterministic fallback
    reviewer.py    a diff against the owner's standing rules before any merge (no feature removed first); optional local model review
    integrator.py  green task branches -> an integ/agents-N branch, SDK regeneration, the sharded suite, flake vs failure, the push queue
    shards.py      the machine-wide suite lock, a worker count sized from free CPU/RAM, test files split into shards
    pusher.py      pushes an approved queue entry (only that exact sha, only when fresh, leak-checked); off by default
    run.py         orchestration: plan -> board -> workers (the swarm board runner) -> review -> integrate; status, queue, approval
    store.py       plans, reviews, integrations, the push queue and escalations (SQLite in the agent state folder)
    llm.py         the one place a model is called: local models only (ModelMistress first, then Ollama), never a cloud model

Nothing here deploys. A green integration branch waits in the push queue for an approval; only the pusher (off by default) pushes one.
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
    "autofix_lint": True,              # before review: apply ruff's SAFE fixes to the branch's changed .py files, as their own commit
    "min_context": 16384,              # a ModelMistress model served with less context per request than this is refused up front
    "suite_command": [],               # [] = python -m pytest tests -n 8 -q -p no:cacheprovider -rf
    "suite_timeout_s": 3600,
    "base_branch": "main",
    "regen_sdk": True,                 # integrator: regenerate the OpenAPI document and SDK when routes changed, commit it on the integ branch
    "suite_shards": 1,                 # test files split into this many shards, run one after another (1 = the suite as one run)
    "suite_workers": "auto",           # pytest -n: "auto" = sized from the idle CPUs and free RAM (at most max_workers), or a number
    "max_workers": 8,
    "suite_lock_wait_s": 1800,         # how long a run waits for the machine-wide suite lock before giving up
    "gate_command": [],                # optional final gate after a green suite, e.g. [python, scripts/local_pipeline.py, --no-deploy]
    "gate_timeout_s": 7200,
    "integrator": {"selective_first": True},   # run the tests the merged diff affects first; the full suite is still the final gate
    "cache": {"enabled": True},        # CacheIt-backed result cache for proofs and suite runs (a missing hub just means no caching)
    "review_scope": {"fail": False},   # reviewer: callers outside the task's declared files are a warning (true = a rejection)
    "context": {"use_codeintel": True,    # LF-INT: pre-seed prompts with codeintel slices, offer the navigation tools, give the planner a map
                "use_vault": True,        # recall/capture through the context vault (earlier findings survive a retry)
                "pack_tokens": 2500, "plan_pack_tokens": 3000, "repo_map_tokens": 1500, "plan_context_chars": 9000,
                "vault_tokens": 500,
                # adaptive: size the pre-seeded pack by the task (pack_tokens is the cap); false = always pre-seed, as before
                "adaptive": True, "min_files_for_pack": 3, "min_bytes_for_pack": 20000, "long_detail_chars": 800,
                "small_repo_symbols": 150, "min_pack_tokens": 400},
    # Grok Build as a Factory worker (bot/conductor/grok_worker.py): mode off | advisor (Grok plans with no tools, a confined
    # local model works) | confined (Grok works in its own AppContainer with internet; needs network_approved) | auto (the
    # default: confined when approved, else advisor; skipped where Grok is not installed). The Workspace switches both.
    "grok": {"mode": "auto", "model": "", "network_approved": False, "timeout_s": 900,
             "confine_backend": "wsl_vm"},   # confined Grok runs in: wsl_vm (throw-away Linux VM) | appcontainer
    "pusher": {"enabled": False,       # the pusher acts only on a recorded approval, and only when this is true
               "remote": "origin", "target": ""},   # target "" = the integration's base branch
}


def settings() -> dict[str, Any]:
    cfg: dict[str, Any] = {}
    try:
        from bot.config import config

        cfg = dict(config.current.get("conductor") or {})
    except Exception:  # noqa: BLE001 - config trouble must not stop the conductor
        pass
    out = {**DEFAULTS, **{k: v for k, v in cfg.items() if k in DEFAULTS}}
    out["pusher"] = {**DEFAULTS["pusher"], **(cfg.get("pusher") if isinstance(cfg.get("pusher"), dict) else {})}
    for k in ("integrator", "cache", "review_scope", "grok"):
        out[k] = {**DEFAULTS[k], **(cfg.get(k) if isinstance(cfg.get(k), dict) else {})}
    out["context"] = {**DEFAULTS["context"], **(cfg.get("context") if isinstance(cfg.get("context"), dict) else {})}
    if not out["suite_command"]:
        out["suite_command"] = [sys.executable, "-m", "pytest", "tests", "-n", "8", "-q", "-p", "no:cacheprovider", "-rf"]
    return out
