"""What the capacity state means for choosing a model.

    cloud-led  nothing changes.
    hybrid     cloud models only for the task classes in capacity.hybrid.cloud_classes (default hard_reasoning, long_context);
               everything else goes to local models. If no local model fits, the cloud list is kept (hybrid prefers, never blocks).
    local-only only local models. If the configured list has none, the local models ABP can see are used (ModelMistress first,
               Ollama after). If there are none at all, nothing is offered - the caller says so instead of calling a cloud model.

Used by bot/model_router.recommend() (so chat/agent routing, failover hops and swarm members on `auto` all follow it), by
reroute_reason() (a sticky cloud pick is re-chosen) and by the native backend for bots with a fixed cloud model. Claude is never
added: the local list is built from local providers only.
"""
from __future__ import annotations

from typing import Iterable, Optional
from urllib.parse import urlparse

from bot.capacity import governor

_LOOPBACK = ("127.0.0.1", "localhost", "::1", "[::1]")


def _provider_cfg(name: str) -> dict:
    try:
        from bot import providers as registry

        return registry.get_provider(name) or {}
    except Exception:  # noqa: BLE001
        return {}


def is_local_host(base_url: str) -> bool:
    return (urlparse(str(base_url or "")).hostname or "") in _LOOPBACK


def is_local_provider(name: str) -> bool:
    """A provider on this machine: a configured local-server name (ModelMistress, Ollama...) or a loopback base URL."""
    low = str(name or "").strip().lower()
    if low in {str(p).lower() for p in governor.settings().get("local_providers") or []}:
        return True
    return is_local_host(_provider_cfg(name).get("base_url", ""))


def is_local_ref(ref: str) -> bool:
    return is_local_provider(str(ref).partition("/")[0])


def is_local_transport(transport) -> bool:
    """True for a transport that talks to this machine (ModelMistress, Ollama...). Unknown transports count as cloud."""
    base = getattr(transport, "base_url", None)
    if base and is_local_host(base):
        return True
    return str(getattr(transport, "router_provider", "") or "").lower() in {str(p).lower() for p in governor.settings().get("local_providers") or []}


def local_candidates() -> list[str]:
    """Local model refs ABP can see: the router's own candidate list, local ones only (ModelMistress first)."""
    try:
        from bot import model_router

        refs = [r for r in model_router.candidate_models() if is_local_ref(r)]
    except Exception:  # noqa: BLE001
        refs = []
    return sorted(dict.fromkeys(refs), key=lambda r: (not r.lower().startswith("modelmistress/"), r))


def cloud_classes() -> set[str]:
    return {str(c) for c in (governor.settings().get("hybrid") or {}).get("cloud_classes") or []}


def apply(refs: Iterable[str], task_class: str, *, state: Optional[str] = None) -> tuple[list[str], list[str]]:
    """(the refs to rank, notes for the 'left out' list). Never raises."""
    refs = list(refs)
    try:
        state = state or governor.effective_state()
        if state == governor.CLOUD_LED:
            return refs, []
        local = [r for r in refs if is_local_ref(r)]
        if state == governor.HYBRID:
            if task_class in cloud_classes():
                return refs, []
            if not local:
                return refs, ["capacity is hybrid but no local model fits; cloud models kept"]
            return local, [f"{len(refs) - len(local)} cloud model(s) left out: capacity is hybrid and {task_class} work stays local"] \
                if len(local) < len(refs) else []
        # local-only
        if not local:
            local = local_candidates()
        left = len([r for r in refs if r not in local])
        return local, [f"{left} cloud model(s) left out: capacity is local-only"] if left else []
    except Exception:  # noqa: BLE001 - routing must never break on the governor
        return refs, []


def leave_reason(task_class: str = "", *, state: Optional[str] = None) -> Optional[str]:
    """Why a turn on a cloud model should use a local one for its next call, or None to stay."""
    try:
        state = state or governor.effective_state()
        if state == governor.LOCAL_ONLY:
            return "capacity is local-only"
        if state == governor.HYBRID and task_class and task_class not in cloud_classes():
            return f"capacity is hybrid and {task_class} work stays local"
    except Exception:  # noqa: BLE001
        return None
    return None


def should_leave(ref: str, task_class: str = "", *, state: Optional[str] = None) -> Optional[str]:
    """leave_reason() for the model `ref`: None when it is already local."""
    return None if is_local_ref(ref) else leave_reason(task_class, state=state)


def decide(task: str = "") -> dict:
    """A plain answer for the CLI/API: where a task like this would go right now."""
    from bot import model_router

    state = governor.effective_state()
    cls = model_router.classify(task).task_class if task else ""
    cloud = state == governor.CLOUD_LED or (state == governor.HYBRID and (not cls or cls in cloud_classes()))
    return {"state": state, "task_class": cls, "where": "any model" if state == governor.CLOUD_LED else ("cloud allowed" if cloud else "local models only")}
