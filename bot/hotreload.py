"""Python code hot-reload for BotServer's own `bot/*.py` source — applies
an edit to the already-running process instead of requiring the full
stop/rebuild/relaunch cycle `scripts/local_pipeline.py` uses today.

This is deliberately conservative, not a generic "reload anything
safely" claim. Every file under `bot/` is classified into one of three
tiers (see DENYLIST/PLATFORM_MODULES/RELOAD_ORDER below):

- **Denylist**: holds live singleton/subprocess/socket/DB-connection
  state, or (like `bot/handlers.py`) hands specific function objects to
  an external library that never looks them up again — reload would
  either orphan that state or be silently inert. A change here is
  detected and reported as "restart required"; nothing is touched.
- **Platform adapters** (`bot/platforms/discord_platform.py`,
  `slack_platform.py`, `matrix_platform.py`): reloaded, then every live
  instance of that one platform is restarted via the existing
  `platform_supervisor.restart_instance()` — required because each
  registers a callback object with an external library (discord.py/
  slack_bolt/matrix-nio) that, once registered, is never looked up by
  name again; only tearing down and reconstructing the connection makes
  it call into freshly-reloaded code.
- **Leaf/business-logic** (everything else — commands, validators,
  plugins-of-BotServer-itself... see RELOAD_ORDER): reloaded only, no
  restart of anything, takes effect on the very next call, because every
  call site reaches these through a fresh attribute/global lookup at
  call time against the live module `__dict__` `importlib.reload()`
  mutates in place.

See `tests/test_hotreload_classification.py` for the check that keeps
this classification from silently rotting as the codebase grows (every
`bot/**/*.py` file must appear in exactly one tier, and the fixed reload
order must be a real topological order against each file's actual
imports).

Reload is **not** transactional: `importlib.reload()` re-executing a
module's top-level code can fail partway through (a `NameError`, a bad
edit), leaving that module half-old/half-new with no rollback. On the
first such failure this module enters a degraded state — no further
reload cycles run until the process is actually restarted — rather than
risk compounding a half-applied module with more reload attempts.
"""

from __future__ import annotations

import importlib
import logging
import py_compile
import sys
import time
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from bot.envfile import PROJECT_ROOT

logger = logging.getLogger("bot.hotreload")

BOT_PKG_DIR = PROJECT_ROOT / "bot"
PKG_DOTTED_PREFIX = "bot"

# Holds live singleton/subprocess/socket/connection state, or hands bare
# function objects to an external library that never re-looks-up the
# name — see module docstring for the two failure shapes this guards
# against, and the plan/commit history for the specific evidence behind
# each entry (several were found only by grepping every candidate file
# for module-level mutable containers, not by inspection alone).
DENYLIST: frozenset[str] = frozenset({
    "bot.main", "bot.router", "bot.db", "bot.config", "bot.dashboard.server",
    "bot.agent_runtime.engine", "bot.agent_runtime.approval", "bot.platform_supervisor",
    "bot.envfile", "bot.handlers", "bot.outbox", "bot.plugins", "bot.attachments",
    "bot.hotreload",  # never reload the reloader mid-cycle
    "bot.mcp_server",  # a separate process (python -m bot.mcp_server); not part of this one anyway
    "bot.tui.app", "bot.tui.client", "bot.tui.__main__",  # a separate process (python -m bot.tui); not part of this one anyway
    "bot.tui.screens.connect", "bot.tui.screens.bot_list", "bot.tui.screens.add_bot", "bot.tui.screens.bot_detail",
    "bot.swarm.base", "bot.swarm.strategies", "bot.swarm.engine",
    "bot.support_bot.model", "bot.support_bot.training_data", "bot.support_bot.hybrid",
    "bot.support_bot.actions", "bot.support_bot.nn_model", "bot.support_bot.slots",
    "bot.support_bot.engine",
})

# module dotted-name -> the platform name to pass to
# platform_supervisor.restart_instance() for every live instance of it.
PLATFORM_MODULES: dict[str, str] = {
    "bot.platforms.discord_platform": "discord",
    "bot.platforms.slack_platform": "slack",
    "bot.platforms.matrix_platform": "matrix",
}

# Leaf/business-logic modules with no restart-requiring callback
# registration and no orphan-on-reload state of their own (confirmed by
# reading each one, not assumed) — reloaded in this fixed order, leaves
# first, every cycle (not just the changed files), so a module that
# already re-executed `from X import y` this cycle never gets stuck
# holding X's pre-reload value for the rest of the cycle.
_TIER3_LEAVES: tuple[str, ...] = (
    "bot.validators",
    "bot.platform_guides",
    "bot.personas",
    "bot.bot_instances",  # depends only on validators/personas — before pairing/memory, which import it at module scope
    "bot.model_pricing",
    "bot.models",
    "bot.push",
    "bot.desktop",
    "bot.pairing",
    "bot.thumbnails",
    "bot.firewall",
    "bot.retention",
    "bot.peers",
    "bot.turn",
    "bot.auth",
    "bot.slash_access",
    "bot.scheduler",
    "bot.memory",
    "bot.kanban",
    "bot.skills",
    "bot.shared_context",
    "bot.providers",
    "bot.hermes_config",
    "bot.swarm.prompts",
    "bot.swarm.child_parser",
    "bot.swarm_budget",
    "bot.swarm.observability",  # only ever imported lazily (inside a function body), so no ordering constraint from anything else here
    "bot.setup_wizard",
    "bot.snapshots",
    "bot.agent_control",
    "bot.backends.base",
    "bot.agent_runtime.transports.base",
    "bot.agent_runtime.transports.anthropic",
    "bot.agent_runtime.transports.openai_compatible",
    "bot.backends.native_backend",
    "bot.agent_runtime.tools",
    "bot.agent_runtime.checkpoints",
    "bot.agent_runtime.tool_loop",
    "bot.backends.api_backend",
    "bot.backends.cli_backend",
    "bot.backends.ui_backend",
    "bot.backends.hermes_cli_backend",
    "bot.backends.hermes_gateway_backend",
    "bot.backends.custom_model_backend",
    "bot.slash_commands",
)

# bot.commands depends on (imports) most of _TIER3_LEAVES; the platform
# adapters and whatsapp_platform depend on bot.commands (`from
# bot.commands import CmdContext, dispatch_command`) — both groups must
# be reloaded *after* it every cycle, or their own already-executed
# import line would bind against commands.py's pre-reload code for the
# rest of that cycle.
RELOAD_ORDER: tuple[str, ...] = (
    *_TIER3_LEAVES,
    "bot.commands",
    *PLATFORM_MODULES,
    "bot.platforms.whatsapp_platform",
)

# Modules whose reload should evict Router's cached backend instances
# (see module docstring's HermesGatewayBackend/subprocess note) —
# anything that changes what a `_build_backend()` call produces.
BACKEND_MODULES: frozenset[str] = frozenset(m for m in RELOAD_ORDER if m.startswith("bot.backends."))

_degraded: Optional[str] = None
_last_events: list[dict[str, Any]] = []
_MAX_EVENTS = 20


def is_degraded() -> Optional[str]:
    return _degraded


def _record(status: str, detail: str) -> None:
    _last_events.insert(0, {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "status": status, "detail": detail})
    del _last_events[_MAX_EVENTS:]
    try:
        from bot import db

        db.log_audit(actor="hot-reload", action=f"hot_reload_{status}", detail=detail)
    except Exception:
        logger.exception("failed to record hot-reload event to audit log")


def status() -> dict[str, Any]:
    enabled = True
    try:
        from bot.config import config

        enabled = bool(config.current.get("hot_reload_enabled", True))
    except Exception:
        pass
    return {"enabled": enabled, "degraded": _degraded, "recent_events": list(_last_events)}


def _path_to_module(path: Path, pkg_root: Path, pkg_dotted_prefix: str) -> Optional[str]:
    try:
        rel = path.resolve().relative_to(pkg_root.resolve())
    except ValueError:
        return None
    if rel.suffix != ".py":
        return None
    parts = rel.parts[:-1] + (rel.stem,)
    return pkg_dotted_prefix + "." + ".".join(parts) if parts else pkg_dotted_prefix


def _module_to_path(mod_name: str, pkg_root: Path, pkg_dotted_prefix: str) -> Path:
    rel_parts = mod_name[len(pkg_dotted_prefix) + 1 :].split(".")
    return pkg_root.joinpath(*rel_parts).with_suffix(".py")


async def run_cycle(
    changed_files: list[Path],
    *,
    pkg_root: Path = BOT_PKG_DIR,
    pkg_dotted_prefix: str = PKG_DOTTED_PREFIX,
    denylist: frozenset[str] = DENYLIST,
    reload_order: tuple[str, ...] = RELOAD_ORDER,
    platform_modules: dict[str, str] = PLATFORM_MODULES,
    backend_modules: frozenset[str] = BACKEND_MODULES,
    shutdown_backends: Optional[Callable[[], Awaitable[None]]] = None,
    restart_instances_for_platform: Optional[Callable[[str], Awaitable[int]]] = None,
) -> dict[str, Any]:
    """Parameterized so tests can drive this against a throwaway package
    under tmp_path instead of mutating real bot/*.py files. The real
    watch loop and the manual "reload now" trigger both call this with
    BotServer's own constants (the defaults above)."""
    relevant = [p for p in changed_files if p.suffix == ".py" and p.name != "__init__.py"]
    if not relevant:
        return {"status": "no_change", "detail": "no relevant .py files changed"}

    global _degraded
    if _degraded is not None:
        detail = f"skipped — degraded since a previous cycle: {_degraded}"
        return {"status": "skipped_degraded", "detail": detail}

    for path in relevant:
        try:
            py_compile.compile(str(path), doraise=True)
        except py_compile.PyCompileError as exc:
            detail = f"{path.name}: {exc}"
            _record("syntax_error", detail)
            return {"status": "syntax_error", "detail": detail}

    changed_modules = set()
    for path in relevant:
        mod = _path_to_module(path, pkg_root, pkg_dotted_prefix)
        if mod:
            changed_modules.add(mod)

    hit_denylist = sorted(changed_modules & denylist)
    if hit_denylist:
        detail = f"restart required for: {', '.join(hit_denylist)}"
        _record("restart_required", detail)
        return {"status": "restart_required", "detail": detail}

    unclassified = sorted(m for m in changed_modules if m not in reload_order)
    if unclassified:
        detail = f"restart required for unclassified file(s): {', '.join(unclassified)}"
        _record("restart_required", detail)
        return {"status": "restart_required", "detail": detail}

    touched_backends = False
    touched_platforms: set[str] = set()
    reloaded: list[str] = []
    for mod_name in reload_order:
        module = sys.modules.get(mod_name)
        if module is None:
            continue  # never imported this run (e.g. a backend never used) — nothing live to refresh
        try:
            importlib.reload(module)
        except Exception as exc:
            _degraded = f"{mod_name} failed to reload: {exc}"
            logger.exception("hot-reload: %s failed to reload — degraded, restart required", mod_name)
            _record("degraded", _degraded)
            return {"status": "degraded", "detail": _degraded}
        reloaded.append(mod_name)
        if mod_name in backend_modules:
            touched_backends = True
        if mod_name in platform_modules:
            touched_platforms.add(platform_modules[mod_name])

    if touched_backends and shutdown_backends is not None:
        await shutdown_backends()

    restarted_summary = []
    if restart_instances_for_platform is not None:
        for platform_name in sorted(touched_platforms):
            n = await restart_instances_for_platform(platform_name)
            restarted_summary.append(f"{platform_name}:{n}")

    detail = f"{len(changed_modules)} file(s) changed -> reloaded {len(reloaded)} module(s)"
    if restarted_summary:
        detail += f"; restarted {', '.join(restarted_summary)}"
    _record("applied", detail)
    return {"status": "applied", "detail": detail}


async def _shutdown_backends() -> None:
    from bot.router import router

    await router.shutdown_backends()


async def _restart_platform_instances(platform_name: str) -> int:
    from bot import bot_instances, platform_supervisor

    count = 0
    for row in bot_instances.list_instances(platform=platform_name, enabled_only=True):
        if platform_supervisor.is_running(row["id"]):
            await platform_supervisor.restart_instance(row["id"])
            count += 1
    return count


async def trigger_manual_reload() -> dict[str, Any]:
    """The dashboard's "Reload now" button — forces a full cycle over
    every Tier 2/3 module regardless of what actually changed on disk."""
    paths = [_module_to_path(m, BOT_PKG_DIR, PKG_DOTTED_PREFIX) for m in RELOAD_ORDER]
    return await run_cycle(paths, shutdown_backends=_shutdown_backends,
                            restart_instances_for_platform=_restart_platform_instances)


async def watch_forever() -> None:
    """Background task (started alongside bot/config.py's own
    watch_forever() in bot/main.py): watches bot/ for .py changes and
    hot-reloads on each one, unless hot_reload_enabled is off in
    config/backends.yaml (checked live, every event, so toggling it in
    the dashboard takes effect without a restart)."""
    from watchfiles import awatch

    async for changes in awatch(str(BOT_PKG_DIR)):
        try:
            from bot.config import config

            if not config.current.get("hot_reload_enabled", True):
                continue
        except Exception:
            pass
        changed_paths = [Path(p) for _change, p in changes]
        await run_cycle(changed_paths, shutdown_backends=_shutdown_backends,
                         restart_instances_for_platform=_restart_platform_instances)
