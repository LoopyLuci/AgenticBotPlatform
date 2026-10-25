"""Factory mode in Local Chat (PLAN-local-chat.md P4): a goal becomes a conductor plan you can read, and only the owner's Run
starts it - with every worker confined.

    preview(sid, goal)            plan the goal with the conductor (local model), store the plan as a message with its tasks and
                                  proofs; nothing runs
    start(sid, plan_id, ...)      the owner's Run: the conductor's real worker (ABP's agent on the session's local model) with
                                  `run_shell` only inside a Windows AppContainer with no network (shell="sandbox") - refused,
                                  never degraded, when that cannot be provided; the first run in a repository needs an explicit
                                  confirmation; workers appear as linked worker sessions; review, integration, and the green
                                  branch waits in the push queue for approval (nothing merges or pushes from here)
    approved(repo)                whether the owner has confirmed a Factory run in that repository before

One Factory run at a time per session; the GPU is shared through the conductor's own lock (one agent turn or proof at a time).
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Any, Awaitable, Callable

from bot.localchat import store

logger = logging.getLogger("bot.localchat.factory")

Broadcast = Callable[[dict], Awaitable[None]]
_runs: dict[str, asyncio.Task] = {}
_models: dict[str, str] = {}          # session -> the model its running Factory run works on (GPU sharing with chat)


def busy_models() -> set[str]:
    """The models running Factory runs are using: a chat turn must not unload them."""
    return {m for sid, m in list(_models.items()) if m and running(sid)}


class FactoryRefused(RuntimeError):
    """The Factory run did not start; the message says why (confinement, confirmation, no repository...)."""


class NeedsConfirmation(FactoryRefused):
    """The first Factory run in this repository: the owner must confirm it."""


def _approvals_path() -> Path:
    from bot.agent_runtime.state import state_dir

    return state_dir("localchat") / "factory_approved.json"


def _approvals() -> dict:
    try:
        data = json.loads(_approvals_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def approved(repo: str) -> bool:
    return str(Path(repo).resolve()).lower() in _approvals()


def _approve(repo: str, by: str) -> None:
    data = _approvals()
    data[str(Path(repo).resolve()).lower()] = {"by": by, "at": time.time()}
    p = _approvals_path()
    p.write_text(json.dumps(data, indent=1), encoding="utf-8")


def _target(session: dict) -> tuple[str, str]:
    """(repository, base branch) a Factory run works on: the session's worktree branch when it has one (the run builds on the
    session's work), else the workspace repository's current branch."""
    from bot.localchat import worktrees

    wt = session.get("worktree")
    if wt and wt.get("repo") and not wt.get("worker"):
        return wt["repo"], wt["branch"]
    repo = worktrees.repo_of(session.get("workspace") or "")
    if repo is None:
        raise FactoryRefused("Factory mode needs the session's workspace to be a git repository")
    head = worktrees._out(repo, "rev-parse", "--abbrev-ref", "HEAD")
    return str(repo), head


class _NoWorker:
    """Planning needs no worker; running never uses this one."""

    async def __call__(self, spec: dict, feedback: str) -> dict:   # pragma: no cover - never called
        raise FactoryRefused("planning only")


def make_worker(repo: str, model: str) -> Any:
    """The conductor's real worker, confined. Raises FactoryRefused when the AppContainer sandbox is not available: a Factory
    worker never runs unconfined."""
    from bot.conductor import confine, run

    try:
        # isolate: this runs inside ABP, and a worker's environment is process-global (bot/conductor/isolated.py)
        local = run.AgentLoopWorker(repo, model=model, shell="sandbox", isolate=True)
    except confine.SandboxUnavailable as e:
        raise FactoryRefused(f"the confined sandbox is not available, so no Factory worker runs: {e}") from e
    from bot.conductor import grok_worker

    mode = str(grok_worker.settings().get("mode") or "off")
    if mode == "off" or (mode == "auto" and not grok_worker.installed()):     # auto is the default: no Grok here, no Grok
        return local
    try:
        return grok_worker.GrokWorker(local)       # Grok as advisor or confined worker (conductor.grok)
    except grok_worker.GrokUnavailable as e:
        raise FactoryRefused(str(e)) from e


def _worker_kind(worker: Any) -> str:
    """How the run's workers are confined, as the start message says it."""
    from bot.conductor import grok_worker

    if isinstance(worker, grok_worker.GrokWorker):
        if worker.mode == "advisor":
            return "Grok plans with no tools; the local worker edits in an AppContainer with no network"
        return ("Grok in a throw-away Linux VM with internet only" if worker.box == "wsl_vm"
                else "Grok in its own AppContainer with internet only")
    return "AppContainer, no network"


def _conductor(repo: str, base: str, worker: Any, model: str, on_event: Any = None) -> Any:
    from bot.conductor import run

    return run.Conductor(repo, worker, base=base, model=model, on_event=on_event)


def _plan_text(plan: dict) -> str:
    lines = [f"Factory plan {plan['id']} for: {plan['goal']}", f"Planned by {plan.get('planner') or 'the conductor'}; "
             f"{len(plan.get('tasks') or [])} task(s). Nothing runs until you press Run.", ""]
    for t in plan.get("tasks") or []:
        proof = (t.get("proof") or {}).get("command") or "no proof command"
        deps = f" (after {', '.join(t['deps'])})" if t.get("deps") else ""
        files = f" - files: {', '.join(t['files'])}" if t.get("files") else ""
        lines.append(f"- **{t['title']}**{deps}{files}  \n  proof: `{proof}`")
    return "\n".join(lines)


async def preview(sid: str, goal: str, *, model: str = "") -> dict:
    goal = (goal or "").strip()
    if not goal:
        raise ValueError("say what the Factory should build")
    session = store.get(sid)
    if session is None:
        raise KeyError(sid)
    repo, base = _target(session)
    from bot.localchat import chat

    planner_model = model or (chat.model_ref(session["model"]) if session["model"] else "")
    cond = _conductor(repo, base, _NoWorker(), planner_model)
    plan = (await cond.plan(goal)).to_dict()
    turn = store.start_turn(sid)
    store.add_message(sid, "user", goal, turn_id=turn, model=session["model"])
    store.add_message(sid, "assistant", _plan_text(plan), turn_id=turn, model=session["model"],
                      meta={"factory_plan": plan, "repo": repo, "base": base, "first_run": not approved(repo)})
    store.finish_turn(turn, "done")
    return {"plan": plan, "repo": repo, "base": base, "needs_confirmation": not approved(repo)}


def running(sid: str) -> bool:
    t = _runs.get(sid)
    return t is not None and not t.done()


async def start(sid: str, plan_id: str, broadcast: Broadcast, *, confirm: bool = False, by: str = "owner (Local Chat)",
                integrate: bool = True) -> dict:
    """The owner's Run. Returns at once; progress arrives as linked worker sessions and messages in this session."""
    from bot.conductor import planner
    from bot.conductor import store as cstore
    from bot.localchat import chat, links

    session = store.get(sid)
    if session is None:
        raise KeyError(sid)
    if running(sid):
        raise FactoryRefused("a Factory run is already going in this session")
    raw = cstore.get_plan(plan_id)
    if raw is None:
        raise LookupError(f"no plan {plan_id}")
    repo, base = _target(session)
    first = not approved(repo)
    # A plan's id comes from its goal, so planning the same goal again is the same plan, and a task an earlier run escalated
    # would stop this run at once ("0 of 3 accepted" in seconds, 2026-10-20). Said up front; confirming gives it a fresh attempt.
    waiting = [e for e in cstore.list_escalations(True, 500) if e.get("plan_id") == plan_id]
    if (first or waiting) and not confirm:
        why = []
        if first:
            why.append(f"this is the first Factory run in {Path(repo).name}: confirm it (workers run confined, nothing merges or "
                       "pushes without your approval in the push queue)")
        if waiting:
            names = ", ".join(sorted({str(e["task_key"]).rsplit(":", 1)[-1] for e in waiting}))
            why.append(f"an earlier run of this plan is waiting on your decision for {names} (Coordination -> escalations): "
                       "confirm to give it a fresh attempt")
        raise NeedsConfirmation("; ".join(why))
    if first:
        _approve(repo, by)
    model = chat.model_ref(session["model"]) if session["model"] else ""
    worker = make_worker(repo, model)                                  # refuses when the sandbox is unavailable
    plan = planner.Plan.from_dict(raw)
    cond = _conductor(repo, base, worker, model, links.factory_observer(sid, broadcast, repo=repo, base=base, model=session["model"]))
    for e in waiting:
        await asyncio.to_thread(cond.unblock, plan_id, e["task_key"], by, "a fresh attempt, confirmed in Local Chat")
    if waiting:
        store.add_message(sid, "system", f"{len(waiting)} task(s) an earlier run escalated get a fresh attempt.",
                          meta={"factory_retry": [e["task_key"] for e in waiting]})
    store.add_message(sid, "system", f"Factory run of plan {plan_id} started: {len(plan.tasks)} task(s), workers confined "
                      f"({_worker_kind(worker)}). Its workers appear as linked sessions.", meta={"factory_run": plan_id})
    await chat._status(broadcast, sid, "working", factory=plan_id)
    _models[sid] = session["model"]
    _runs[sid] = asyncio.create_task(_run(sid, cond, plan, broadcast, integrate), name=f"localchat-factory-{sid}")
    return {"started": plan_id, "tasks": len(plan.tasks), "repo": repo, "base": base}


async def _run(sid: str, cond: Any, plan: Any, broadcast: Broadcast, integrate: bool) -> None:
    from bot.localchat import chat

    state = "done"
    try:
        report = (await cond.run(plan, integrate=integrate)).to_dict()
        ok = [t for t, r in report["tasks"].items() if r.get("status") == "success"]
        lines = [f"Factory run of plan {plan.id} finished: {len(ok)} of {len(report['tasks'])} task(s) accepted."]
        if report.get("escalated"):
            lines.append("Waiting on a decision: " + ", ".join(report["escalated"]) + " (see Coordination -> escalations).")
        if report.get("not_run"):
            lines.append("Not started (a task they depend on did not finish): " + ", ".join(report["not_run"]) + ".")
        integ = report.get("integration") or {}
        meta: dict[str, Any] = {"factory_report": {k: report.get(k) for k in ("plan_id", "accepted", "escalated", "not_run")}}
        if integ:
            lines.append(f"Integration {integ.get('branch')}: {integ.get('status')}"
                         + (f", queued as {integ['queue_id']}: approve it to push (nothing pushes by itself)." if integ.get("queue_id") else "."))
            meta["queue_id"] = integ.get("queue_id")
            meta["integration"] = {k: integ.get(k) for k in ("branch", "status", "queue_id", "merged", "failed", "note")}
        store.add_message(sid, "assistant", "\n".join(lines), meta=meta)
        if report.get("escalated"):
            state = "needs_you"
    except asyncio.CancelledError:
        state = "idle"
        store.add_message(sid, "system", "The Factory run was stopped.")
    except Exception as e:  # noqa: BLE001 - say what went wrong in the session
        state = "error"
        logger.warning("local chat factory run failed: %s", e)
        store.add_message(sid, "error", f"The Factory run failed: {type(e).__name__}: {e}"[:2000])
    finally:
        _runs.pop(sid, None)
        _models.pop(sid, None)
        await chat._status(broadcast, sid, state, unread=True)
        await chat._safe(broadcast, {"type": "local_chat_turn", "session_id": sid, "turn_id": "", "state": state, "factory": True})


async def stop(sid: str) -> bool:
    t = _runs.get(sid)
    if t is None or t.done():
        return False
    t.cancel()
    try:
        await asyncio.wait_for(asyncio.shield(t), timeout=15)
    except (asyncio.CancelledError, asyncio.TimeoutError, Exception):  # noqa: BLE001
        pass
    return True


def board(sid: str) -> dict:
    """This session's Factory board: its worker sessions and the push-queue entries its runs produced."""
    from bot.conductor import store as cstore
    from bot.localchat import links

    workers = [c for c in links.children(sid) if c["link"] == "worker"]
    qids = {(m.get("meta") or {}).get("queue_id") for m in store.messages(sid)} - {None, ""}
    queue = [q for q in (cstore.get_queue_entry(q) for q in qids) if q]
    return {"running": running(sid), "workers": workers,
            "queue": [{k: q.get(k) for k in ("id", "branch", "status", "head_sha", "decided_by", "base")} for q in queue]}
