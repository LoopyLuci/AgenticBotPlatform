"""How work queues follow the capacity state: ABP's swarm board, the conductor's board queue, the confined dev swarm.

Board tasks carry an optional **capacity hint** as one of their `needs` (the board treats needs as opaque strings, so this works on
ABP's local board and on AgentSentinel's alike):

    (none)           any worker, any model - the router decides from the state (local-only -> a local model)
    capacity:local   cheap, routine work: always safe on a local model
    capacity:cloud   work that wants a cloud model (hard reasoning, long context): picked up only while cloud capacity exists

A worker lists the capabilities it can serve with `worker_capabilities(base)`: always `capacity:local`, and `capacity:cloud` unless
the state is local-only. So in local-only a cloud-hinted task simply stays open (it is not failed, dropped or run on a local model
by surprise) and every other task is picked up and run locally; when capacity returns the next `next` call sees it again. Nothing
that is already running is touched: the state is read only when a worker asks for new work, and `worker_capabilities` is re-read
on every such ask.

`placement(task)` says which kind of model a worker should use for a task it holds (the dev swarm runner and the conductor call it
to choose between its local models and a cloud one); `policy()` is the same information for a whole worker, over the API
(`GET /api/capacity/policy`) and the CLI (`abp capacity policy`).
"""
from __future__ import annotations

from typing import Any, Iterable, Optional

from bot.capacity import classify, governor, routing

PREFIX = "capacity:"
LOCAL, CLOUD = "local", "cloud"
HINTS = ("", LOCAL, CLOUD)
MAX_REQUEUES = 3            # a step that hit a capacity error is handed back this many times, then fails as usual


def need_for(hint: str) -> list[str]:
    """The `needs` entry for a hint ("local" / "cloud"); [] for none."""
    hint = str(hint or "").strip().lower().removeprefix(PREFIX)
    if hint not in HINTS:
        raise ValueError("capacity hint must be 'local', 'cloud' or empty")
    return [PREFIX + hint] if hint else []


def tag(needs: Optional[Iterable[str]], hint: str) -> list[str]:
    """`needs` with the capacity hint set (replacing any earlier one)."""
    kept = [n for n in (needs or []) if not str(n).startswith(PREFIX)]
    return kept + need_for(hint)


def hint_of(task: Optional[dict]) -> str:
    for n in (task or {}).get("needs") or []:
        if str(n).startswith(PREFIX):
            return str(n)[len(PREFIX):]
    return ""


def cloud_allowed(state: Optional[str] = None) -> bool:
    return (state or governor.effective_state()) != governor.LOCAL_ONLY


def worker_capabilities(base: Optional[Iterable[str]] = None, *, state: Optional[str] = None) -> list[str]:
    """`base` plus what this worker can serve right now. Re-read it on every ask for work (the state changes)."""
    caps = [c for c in (base or []) if not str(c).startswith(PREFIX)]
    caps.append(PREFIX + LOCAL)
    try:
        if cloud_allowed(state):
            caps.append(PREFIX + CLOUD)
    except Exception:  # noqa: BLE001 - a governor fault must not stop a worker from taking work
        caps.append(PREFIX + CLOUD)
    return caps


def placement(task: Optional[dict] = None, *, text: str = "", state: Optional[str] = None) -> dict:
    """Where a worker should run a task: {"state", "where": "local" | "cloud" | "any", "models": [local refs], "why"}.
    The hint decides first; else the state (local-only -> local; hybrid -> local unless the task class is a cloud class)."""
    try:
        state = state or governor.effective_state()
    except Exception:  # noqa: BLE001
        state = governor.CLOUD_LED
    hint = hint_of(task)
    body = text or " ".join(str((task or {}).get(k) or "") for k in ("title", "detail"))
    if hint == LOCAL:
        where, why = LOCAL, "the task is marked local"
    elif state == governor.LOCAL_ONLY:
        where, why = LOCAL, "capacity is local-only"
    elif hint == CLOUD:
        where, why = CLOUD, "the task is marked cloud"
    elif state == governor.HYBRID:
        leave = routing.leave_reason(_task_class(body), state=state)
        where, why = (LOCAL, leave) if leave else (CLOUD, "capacity is hybrid and this is hard work")
    else:
        where, why = "any", "capacity is cloud-led"
    out: dict[str, Any] = {"state": state, "where": where, "why": why or "", "hint": hint}
    out["models"] = routing.local_candidates() if where == LOCAL else []
    return out


def _task_class(text: str) -> str:
    try:
        from bot import model_router

        return model_router.classify(text).task_class if text.strip() else ""
    except Exception:  # noqa: BLE001
        return ""


def policy(base: Optional[Iterable[str]] = None) -> dict:
    """What a worker should do now: the state, the capabilities to advertise, and the local models to use."""
    try:
        state = governor.effective_state()
    except Exception:  # noqa: BLE001
        state = governor.CLOUD_LED
    return {"state": state, "cloud_allowed": cloud_allowed(state), "capabilities": worker_capabilities(base, state=state),
            "local_models": routing.local_candidates() if state != governor.CLOUD_LED else [],
            "note": "tasks marked capacity:cloud wait while the state is local-only; nothing running is interrupted"}


def is_capacity_error(exc: BaseException) -> bool:
    """Did this failure come from running out of cloud capacity (so a retry on a local model is the right answer)?"""
    if getattr(exc, "capacity_seen", False):
        return True
    try:
        return classify.classify(getattr(exc, "status_code", None), str(exc)) is not None
    except Exception:  # noqa: BLE001
        return False
