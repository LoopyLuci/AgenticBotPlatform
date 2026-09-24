"""Python code hot-reload for AgenticBotPlatform's own `bot/*.py` source — applies
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
  plugins-of-AgenticBotPlatform-itself... see RELOAD_ORDER): reloaded only, no
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

import asyncio
import importlib
import logging
import py_compile
import sys
import time
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from bot import envfile
from bot.envfile import CODE_ROOT

logger = logging.getLogger("bot.hotreload")

BOT_PKG_DIR = CODE_ROOT / "bot"
PKG_DOTTED_PREFIX = "bot"


def _enabled_by_default() -> bool:
    """Hot reload re-executes modules from the `bot/` source tree while the
    app runs — a development convenience. When ABP is embedded in a host
    (ABP_HOME set, e.g. as a git submodule/sidecar) that tree is the host's
    dependency: a `git submodule update` while running would trigger partial
    reloads of a half-updated package. So it defaults OFF there; an explicit
    `hot_reload_enabled` in config/backends.yaml still wins either way."""
    return not envfile.ABP_HOME_ACTIVE

# Holds live singleton/subprocess/socket/connection state, or hands bare
# function objects to an external library that never re-looks-up the
# name — see module docstring for the two failure shapes this guards
# against, and the plan/commit history for the specific evidence behind
# each entry (several were found only by grepping every candidate file
# for module-level mutable containers, not by inspection alone).
DENYLIST: frozenset[str] = frozenset({
    "bot.main", "bot.router", "bot.db", "bot.config", "bot.dashboard.server", "bot.dashboard.cicd_api", "bot.dashboard.agent_security_api", "bot.dashboard.models_info_api", "bot.dashboard.approvals_api", "bot.dashboard.channels_api", "bot.dashboard.agent_config_api", "bot.dashboard.tailscale_api", "bot.dashboard.infra_api",
    "bot.agent_runtime.engine", "bot.agent_runtime.approval", "bot.agent_runtime.subagent_registry",
    "bot.platform_supervisor",
    "bot.provider_store",  # owns a lock and the path of the on-disk provider store; a reload would orphan both
    "bot.envfile", "bot.handlers", "bot.outbox", "bot.plugins", "bot.attachments",
    # Same hazard class as outbox.py/plugins.py above: _connections/
    # _tool_index are module-level dicts holding LIVE external MCP
    # subprocess/HTTP sessions (each an open AsyncExitStack). A reload
    # would wipe the dict references while the real connections stay
    # open and orphaned — every external tool would vanish from
    # all_tool_schemas() until a manual reconnect, with no self-healing.
    "bot.agent_runtime.mcp_client",
    # trace.py holds a ContextVar naming the in-flight run and a cache of open event
    # stores; toolspec.py holds the registry of registered tools. A reload would drop
    # both while runs and registrations made against the old objects are still live.
    "bot.agent_runtime.trace", "bot.agent_runtime.toolspec",
    # coding_tools.py keeps per-session read tracking and registers tools at import time.
    "bot.agent_runtime.coding_tools", "bot.agent_runtime.shell",  # shell.py holds the live background-job table
    "bot.agent_runtime.web",  # registers its tools at import time
    # errors.py defines ToolError, which callers catch by identity: a reloaded copy would
    # be a different class and their `except ToolError` would stop matching.
    "bot.agent_runtime.errors", "bot.agent_runtime.state",
    # Security layer: permissions.py holds the run-mode ContextVar, taint.py the set of tainted
    # sessions, secrets_guard.py the injected-secret table, mcp_pins.py its warn-once set, and
    # sandbox.py is only ever imported by the (denied) shell module. A reload would reset them.
    "bot.agent_runtime.permissions", "bot.agent_runtime.taint", "bot.agent_runtime.secrets_guard",
    "bot.agent_runtime.mcp_pins", "bot.agent_runtime.sandbox", "bot.agent_runtime.win_job",
    "bot.agent_runtime.settings_schema",  # imports permissions/sandbox (both denied above); a reload would split the classes it validates against
    "bot.agent_runtime.project_rules",  # imported by prompt.py; reloaded with it is not needed and is safe to skip
    "bot.agent_runtime.code_intel",  # running language servers are process state
    "bot.model_router", "bot.agent_runtime.trajectory",  # registers a tool / reads the trace store
    "bot.nodes", "bot.canvas", "bot.voice", "bot.platforms._relay",
    "bot.platforms.email_platform", "bot.platforms.sms_platform", "bot.platforms.signal_platform", "bot.platforms.imessage_platform",  # long-running adapters
    "bot.agent_runtime.browser", "bot.vault", "bot.routines", "bot.approvals_view",  # a running browser / encrypted store / registered tools
    "bot.agent_runtime.session_export", "bot.backends.external_agent_backend",  # imported by denied modules (dashboard, router)
    "bot.model_catalog", "bot.agent_runtime.usage_limits", "bot.agent_runtime.model_tools",  # usage counters and per-process caches
    "bot.custom_commands", "bot.agent_runtime.skill_learning",  # small, stateless, but imported by denied modules only
    "bot.skill_packs", "bot.skill_install",  # skill_packs registers read_skill_file at import time
    "bot.agent_runtime.agent_defs", "bot.agent_runtime.worktrees",  # agent_defs registers a tool at import time
    "bot.agent_runtime.repo_map", "bot.agent_runtime.search_index",  # register their tools at import time; repo_map caches
    "bot.agent_runtime.context_window",  # holds the learned chars-per-token ratio per model
    "bot.hotreload",  # never reload the reloader mid-cycle
    "bot.mcp_server",  # a separate process (python -m bot.mcp_server); not part of this one anyway
    "bot.tui.app", "bot.tui.client", "bot.tui.__main__",  # a separate process (python -m bot.tui); not part of this one anyway
    "bot.tui.screens.connect", "bot.tui.screens.bot_list", "bot.tui.screens.add_bot", "bot.tui.screens.bot_detail",
    "bot.tui.screens.chat", "bot.tui.screens.providers", "bot.tui.screens.agent_settings",
    "bot.tui.screens.swarms", "bot.tui.screens.sessions", "bot.tui.screens.ssh_toolkit",
    "bot.ssh_toolkit",  # shells out to a separately maintained tool; nothing to reload mid-run either way
    "bot.terminal_broker",  # holds live PTY/socket sessions; a reload would orphan them
    "bot.infra_automation",  # its run_forever() loop is a live background task holding the in-flight tick
    "bot.ssh_session_monitor",  # holds live in-flight session/subprocess state; a reload would orphan it
    "bot.dashboard_client",  # only ever imported by the tui/cli processes above, or abp_cli (its own separate process)
    "bot.swarm.base", "bot.swarm.strategies", "bot.swarm.engine",
    "bot.support_bot.model", "bot.support_bot.training_data", "bot.support_bot.hybrid",
    "bot.support_bot.actions", "bot.support_bot.nn_model", "bot.support_bot.slots",
    "bot.support_bot.engine",
    # _module_classifiers is a module-level cache of live, trained
    # classifier-pair instances (same hazard class as hybrid.py/model.py
    # above) — a reload would silently drop every cached module pair
    # while anything already holding a reference keeps using the old one.
    "bot.support_bot.cascade",
    # _backend is a module-level cache of a loaded (and, if enabled,
    # heavyweight) sentence-transformers model instance — same hazard
    # class as cascade.py's _module_classifiers above.
    "bot.support_bot.embeddings",
    # Module-level `_zeroconf`/`_service_info` singleton for a real
    # registered mDNS service (see start()/stop()) — a reload would rebind
    # the module's own names to None while the old Zeroconf instance is
    # still actually running and advertising in the background, orphaning
    # it exactly like the outbox.py/plugins.py/attachments.py cases this
    # project has hit before. Never restart-required in practice: this
    # module changes rarely and a stale advertisement is harmless (the
    # dashboard's real HTTP server is unaffected either way).
    "bot.mdns_advertise",
    # _handler is a module-level singleton logging.Handler already
    # ATTACHED to the real root logger (see install()) — the exact same
    # hazard as mdns_advertise.py's _zeroconf singleton above: a reload
    # would rebind this module's own `_handler` name to a fresh None
    # while the old handler instance keeps sitting in the root logger's
    # handler list, live and orphaned. Any code that then calls
    # install()/subscribe() again gets a second handler layered on top —
    # duplicate delivery of every future log record — instead of the one
    # ring buffer this whole feature assumes there is exactly one of.
    "bot.activity_log",
    # Same hazard as activity_log.py's _handler above (a module-level
    # logging.Handler singleton already attached to the root logger),
    # plus a second module-level singleton of its own: `telemetry`, an
    # in-memory _Telemetry instance holding live counters/recent-events
    # state that every self-healing hook (platform_supervisor, etc.)
    # holds a reference to via `from bot.diagnostics import telemetry` —
    # a reload would rebind the module's own name to a fresh instance
    # while every existing reference keeps incrementing the orphaned old
    # one, silently freezing the Diagnostics tab's counters.
    "bot.diagnostics",
    # Caches this install's identity in a module-level variable (and its
    # write is lock-guarded) — nothing worth re-executing mid-run, and a reload
    # would only re-read the same file.
    "bot.server_identity",
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
    "bot.effort",  # pure, stateless mapping functions, zero bot-internal deps — must precede hermes_config/the two transports, which import it
    "bot.device_tiers",  # pure, stateless rank-comparison functions, zero bot-internal deps
    "bot.support_bot.model_io",  # pure save/load functions, no module-level mutable state
    "bot.support_bot.knowledge_modules",  # static registry built once at import, never mutated afterward
    "bot.support_bot.module_manifest",  # pure read/write-manifest functions, no module-level mutable state
    "bot.support_bot.eval",  # pure split/evaluate functions, no module-level mutable state
    "bot.support_bot.calibration",  # pure isotonic-regression functions, no module-level mutable state
    "bot.support_bot.synthetic_gen",  # pure async dispatch-building functions, no module-level mutable state
    "bot.support_bot.model_providers",  # pure async provider-selection function, no module-level mutable state
    "bot.support_bot.llm_fallback",  # pure async dispatch functions, no module-level mutable state
    "bot.server_chat_admin",  # pure async dispatch functions, no module-level mutable state
    "bot.validators",
    "bot.network_info",
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
    "bot.tailscale_mgr",
    "bot.docker_mgr",
    "bot.vm_mgr",
    "bot.turn",
    "bot.auth",
    "bot.slash_access",
    "bot.scheduler",
    "bot.auto_manage",  # depends on bot.scheduler (enable/disable manage the underlying schedule row) — must follow it
    "bot.memory",
    "bot.kanban",
    "bot.skills",
    "bot.shared_context",
    "bot.providers",
    "bot.file_share",
    "bot.android_apk",
    "bot.mobile_pairing",
    "bot.hermes_config",
    "bot.swarm.prompts",
    "bot.swarm.child_parser",
    "bot.swarm_budget",
    "bot.swarm.observability",  # only ever imported lazily (inside a function body), so no ordering constraint from anything else here
    "bot.setup_wizard",
    "bot.snapshots",
    "bot.ui_customize",  # pure functions + disk-backed history, no module-level mutable state; moa/providers/models/dashboard.server are all imported lazily inside function bodies, so no ordering constraint from them
    "bot.agent_control",
    "bot.backends.base",
    "bot.agent_runtime.estop",  # depends only on bot.backends.base (EstopEngagedError subclasses BackendError) and db — safe leaf
    "bot.agent_runtime.transports.base",
    "bot.agent_runtime.provider_quirks",  # pure data/functions, no bot-internal deps — must precede the transport that imports it
    "bot.agent_runtime.vision",  # pure data/functions, no bot-internal deps — safe leaf, only imported lazily inside native_backend.py::ask()
    "bot.agent_runtime.anthropic_server_tools",  # pure config-driven function, no state — only imported lazily inside AnthropicTransport.send()
    "bot.agent_runtime.transports.anthropic",
    "bot.agent_runtime.transports.openai_compatible",
    "bot.agent_runtime.transports.responses_api",
    "bot.agent_runtime.compression",  # depends on transports.base's ProviderTransport type only; pure function, no state
    "bot.agent_runtime.hooks",  # pure functions, no module-level state — only imported lazily inside tool_loop.py/native_backend.py
    "bot.agent_runtime.prompt",  # pure functions over config, no state — only imported lazily inside native_backend.py::ask()
    "bot.agent_runtime.loop_guard",  # pure functions plus a per-turn Watchdog object; no module state
    "bot.backends.native_backend",
    "bot.agent_runtime.tools",
    "bot.agent_runtime.checkpoints",
    "bot.agent_runtime.tool_loop",
    "bot.agent_runtime.output_schema",
    "bot.agent_runtime.subagents",
    "bot.agent_settings",  # depends on subagents.DEFAULT_MAX_CONCURRENT_CHILDREN — must follow it
    "bot.agent_runtime.moa",
    "bot.agent_runtime.batches",  # pure functions, no module-level state — only imported lazily inside tools.py::execute_tool()
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
    enabled = _enabled_by_default()
    try:
        from bot.config import config

        enabled = bool(config.current.get("hot_reload_enabled", _enabled_by_default()))
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
    AgenticBotPlatform's own constants (the defaults above)."""
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
    the dashboard takes effect without a restart).

    Two-layer defense against silently going dark forever: a bad
    individual reload cycle (run_cycle() raising) only skips that one
    cycle, never taking the whole watcher down with it; and if awatch()
    itself dies (a real, documented watchfiles failure mode — a deleted
    watched directory, a permission change, an OS-level file-watching
    backend hiccup), the whole watch is re-entered fresh after a short
    delay instead of leaving hot-reload permanently, silently disabled
    for the rest of the process's life with no way to notice short of a
    restart. The same class of bug this project's dashboard-port-bind
    crash already demonstrated once for a different subsystem."""
    from watchfiles import awatch

    while True:
        try:
            async for changes in awatch(str(BOT_PKG_DIR)):
                try:
                    from bot.config import config

                    if not config.current.get("hot_reload_enabled", _enabled_by_default()):
                        continue
                except Exception:
                    pass
                changed_paths = [Path(p) for _change, p in changes]
                try:
                    await run_cycle(
                        changed_paths,
                        shutdown_backends=_shutdown_backends,
                        restart_instances_for_platform=_restart_platform_instances,
                    )
                except Exception:
                    logger.exception("hot-reload cycle failed for %s", changed_paths)
        except Exception:
            logger.exception("hot-reload file watcher crashed — restarting it")
            await asyncio.sleep(2)
