"""Small-model mode (Local Factory LF2): a leaner turn for models with a small context window.

A local model with an 8k-32k window does worse with a long system prompt and dozens of tool definitions. When this
mode is on for a model, what is SENT is trimmed: a compact set of core tools, a shorter system prompt, and tighter
context management. Nothing is removed from ABP - the other tools still exist and run if an agent definition asks
for them by name; they are just not offered to the small model. Cloud models are never affected (unless forced).

    native_agent:
      small_model:
        enabled: auto            # auto = on for a local engine whose window <= max_window; true / false force it
        max_window: 32768
        tools: []                # empty = the core set below; or name the tools to send
        compact_prompt: true     # skip the long optional prompt sections
        keep_tool_results: 3     # replaces native_agent.context.keep_tool_results while on
        compact_at: 0.6          # replaces native_agent.context.compact_at while on

The core tool set is built from the tool groups in agent_defs.py (read, grep, glob, edit, write), `run_shell`, and a
test/verify tool when one exists. Tool-call mode: a model with no `native_agent.tool_call_modes` entry is switched
to `text` only when its transport says it cannot do native tool calls (`supports_native_tools = False`).

`profile_for(transport, model)` is the one place the decision is made; None means "not small, change nothing".
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any, Iterable, Optional

DEFAULT_MAX_WINDOW = 32_768
DEFAULT_KEEP_TOOL_RESULTS = 3
DEFAULT_COMPACT_AT = 0.6
CORE_GROUPS = ("read", "grep", "glob", "edit", "write")   # keys of agent_defs.ALIASES
CORE_EXTRA = ("run_shell", "shell_output")
VERIFY_TOOLS = ("run_tests", "verify")                    # included when such a tool exists


@dataclass(frozen=True)
class SmallProfile:
    window: int
    reason: str
    tools: frozenset
    compact_prompt: bool = True
    keep_tool_results: int = DEFAULT_KEEP_TOOL_RESULTS
    compact_at: float = DEFAULT_COMPACT_AT
    text_tool_calls: bool = False


def _cfg() -> dict:
    try:
        from bot.config import config

        cfg = (config.current.get("native_agent") or {}).get("small_model") or {}
        return cfg if isinstance(cfg, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _enabled(value: Any) -> str:
    """'auto' | 'on' | 'off' from the YAML value (true / false / auto, or their words)."""
    if isinstance(value, bool):
        return "on" if value else "off"
    low = str(value if value is not None else "auto").strip().lower()
    if low in ("true", "yes", "on", "1"):
        return "on"
    if low in ("false", "no", "off", "0"):
        return "off"
    return "auto"


def _num(value: Any, default, kind=int):
    try:
        return kind(value)
    except (TypeError, ValueError):
        return default


def _existing_tool_names() -> set:
    try:
        from bot.agent_runtime import tools as agent_tools

        return {s["name"] for s in agent_tools.all_tool_schemas()}
    except Exception:  # noqa: BLE001
        return set()


def core_tools(existing: Optional[Iterable[str]] = None) -> frozenset:
    """The compact tool set: names from agent_defs' groups, limited to tools that exist."""
    from bot.agent_runtime import agent_defs

    names: set = set(CORE_EXTRA) | set(VERIFY_TOOLS)
    for group in CORE_GROUPS:
        names.update(agent_defs.ALIASES.get(group, ()))
    have = set(existing) if existing is not None else _existing_tool_names()
    return frozenset(n for n in names if n in have)


def _provider(transport: Any) -> tuple[str, bool]:
    """(provider name for window lookup, is it a local engine) - the same reading context_window.manage uses."""
    from bot.agent_runtime import turn_budget

    named = str(getattr(transport, "router_provider", "") or "").lower()
    if named == "modelmistress":
        return "modelmistress", True
    local = named in turn_budget.LOCAL_PROVIDERS or turn_budget.is_local(transport)
    return str(getattr(transport, "provider_key", "") or ""), local or turn_budget.is_loopback_endpoint(transport)


def profile_for(transport: Any, model: str) -> Optional[SmallProfile]:
    cfg = _cfg()
    mode = _enabled(cfg.get("enabled", "auto"))
    if mode == "off":
        return None
    from bot.agent_runtime import context_window

    provider, local = _provider(transport)
    window = context_window.window_for(str(model or ""), provider, local=local)
    max_window = _num(cfg.get("max_window"), DEFAULT_MAX_WINDOW)
    if mode == "auto":
        if not local or window > max_window:
            return None
        reason = f"local model with a {window}-token window (<= {max_window})"
    else:
        reason = "forced on by native_agent.small_model.enabled"
    existing = _existing_tool_names()
    named = cfg.get("tools")
    tools = frozenset(str(n) for n in named if str(n) in existing) if isinstance(named, list) and named else core_tools(existing)
    return SmallProfile(
        window=window, reason=reason, tools=tools,
        compact_prompt=bool(cfg.get("compact_prompt", True)),
        keep_tool_results=max(0, _num(cfg.get("keep_tool_results"), DEFAULT_KEEP_TOOL_RESULTS)),
        compact_at=_num(cfg.get("compact_at"), DEFAULT_COMPACT_AT, float),
        text_tool_calls=getattr(transport, "supports_native_tools", True) is False,
    )


def default_tool_mode(transport: Any, model: str) -> Optional[str]:
    """'text' for a small model on a transport known not to support native tools, else None (leave native)."""
    try:
        profile = profile_for(transport, model)
    except Exception:  # noqa: BLE001
        return None
    return "text" if profile is not None and profile.text_tool_calls else None


def filter_schemas(schemas: list[dict], profile: Optional[SmallProfile]) -> list[dict]:
    """The tools to send: only the profile's, unless there is no profile."""
    if profile is None:
        return schemas
    return [s for s in schemas if s["name"] in profile.tools]


def filter_allowed(schemas: list[dict], profile: Optional[SmallProfile]) -> list[dict]:
    """`schemas` is already limited to an agent's allowed_tools. Small mode sends the intersection with its core set, but
    never drops a tool the agent could not finish without: the edit and write tools, the verify tool (run_tests / verify)
    and spawn_subagent. If the intersection would be empty, the allowed tools are sent as they are."""
    if profile is None:
        return schemas
    from bot.agent_runtime import agent_defs

    keep = set(profile.tools) | set(VERIFY_TOOLS) | {"spawn_subagent"}
    for group in ("edit", "write"):
        keep.update(agent_defs.ALIASES.get(group, ()))
    kept = [s for s in schemas if s["name"] in keep]
    return kept or schemas


# ---- once per session -------------------------------------------------------------------------
_announced: set = set()
_lock = threading.Lock()


def announce(session_key: Any, profile: Optional[SmallProfile], model: str, note) -> bool:
    """Say once per session (via `note(text)`, the trace's note) that small-model mode is on. True when said now."""
    if profile is None:
        return False
    key = (session_key, model)
    with _lock:
        if key in _announced:
            return False
        if len(_announced) > 2000:
            _announced.clear()
        _announced.add(key)
    note(f"small-model mode on for {model}: {profile.reason}; sending {len(profile.tools)} core tools"
         f"{', compact prompt' if profile.compact_prompt else ''}, keeping {profile.keep_tool_results} tool results, "
         f"compacting at {int(profile.compact_at * 100)}%")
    return True
