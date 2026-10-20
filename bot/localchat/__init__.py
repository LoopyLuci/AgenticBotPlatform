"""Local Chat (PLAN-local-chat.md P1): ABP's first screen - a chat with local models on ModelMistress, with ABP's agent.

    store.py    sessions and their messages (SQLite in the agent state folder): title, model, mode, autonomy, workspace,
                cloud assist; recoverable after a restart
    catalog.py  every model the hub can run, one row each, every figure labelled with where it came from: metadata (GGUF),
                planned (the hub's model.plan against this machine's GPU), measured (bench / real replies), estimated
    speed.py    tokens per second: measured replies and benchmarks per model, and a labelled estimate for the rest
    chat.py     a turn: ABP's native agent on the session's model and workspace, through the internal "Local Chat" app
                instance (so approvals, traces, memory and the e-stop work exactly as everywhere else), streamed live to
                the dashboard sockets; cancel stops it; steer / queue while it runs; rewind a turn's file changes
    autonomy.py   autonomy 0-5 as a permission mode plus run-level rules (destructive commands always ask; no push)
    factory.py    Factory mode: plan preview, the owner's Run, confined conductor workers as linked sessions, push queue
    links.py      linked sessions: fork, task, hand off, notes to the parent, Factory workers
    worktrees.py  a git worktree per session (S1): create, status, diff, sync with the base, commit, discard (asks when work
                would be lost), merge only through the conductor's push queue, prune

Nothing here needs a cloud backend or the network: with a model on the hub, Local Chat works offline.
"""
from __future__ import annotations

from typing import Any

MODES = ("chat", "build", "plan", "review", "factory")   # chat: no tools; build: edits; plan: proposes first; review: reads only;
                                                          # factory: a goal -> conductor plan -> owner's Run -> confined workers
CLOUD_ASSIST = ("off", "ask", "auto")

DEFAULTS: dict[str, Any] = {
    "instance_name": "Local Chat",   # the internal app instance every Local Chat turn runs through
    "default_model": "",             # "" = the hub's loaded model, else the best local coder in the catalog
    "default_mode": "build",
    "default_autonomy": 2,           # 0 Locked .. 5 Sovereign; applied to permissions in a later phase
    "cloud_assist": "ask",           # off | ask | auto (owner default 2026-10-19: ask)
    "catalog_ttl_s": 60,
    "worktrees": "auto",             # auto: a new session on a git workspace gets its own worktree (not in Chat mode) | off
    "worktree_root": "",             # "" = a short folder in the agent state folder
    "worktree_prefix": "chat",       # branches are <prefix>/<slug>-<id>
}


def settings() -> dict[str, Any]:
    out = dict(DEFAULTS)
    try:
        from bot.config import config

        got = config.current.get("local_chat") or {}
    except Exception:  # noqa: BLE001 - config trouble must not stop the chat
        got = {}
    if isinstance(got, dict):
        out.update({k: v for k, v in got.items() if k in DEFAULTS})
    return out
