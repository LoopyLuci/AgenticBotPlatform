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
    "bot.main", "bot.router", "bot.db", "bot.config", "bot.dashboard.server", "bot.dashboard.cicd_api", "bot.dashboard.agent_security_api", "bot.dashboard.models_info_api", "bot.dashboard.traces_api", "bot.dashboard.coordination_api", "bot.dashboard.capacity_api", "bot.dashboard.conductor_api", "bot.dashboard.manager_api", "bot.dashboard.evals_api", "bot.dashboard.vault_api", "bot.dashboard.approvals_api", "bot.dashboard.channels_api", "bot.dashboard.agent_config_api", "bot.dashboard.tailscale_api", "bot.dashboard.infra_api", "bot.dashboard.browser_api", "bot.dashboard.browser_gateway_api", "bot.browser_gateway",
    # Extensions (ABP_PRIVATE_DIR): the loader is loaded once and caches what it found, and its
    # routes are registered on the live app at build time - same reason bot.config is here.
    "bot.extensions", "bot.dashboard.extensions_api",
    "bot.agent_runtime.engine", "bot.agent_runtime.approval", "bot.agent_runtime.subagent_registry",
    "bot.platform_supervisor",
    "bot.tasks",  # holds the strong references that keep in-flight background tasks alive
    # The unit runtime owns the live route/tool/panel tables, the task groups and an sqlite connection; a reload
    # would orphan them. (The runtime replaces units itself, through its own transactions.)
    "bot.runtime.unit", "bot.runtime.tasks", "bot.runtime.state", "bot.runtime.dispatcher",
    "bot.runtime.registries", "bot.runtime.manifest", "bot.runtime.engine", "bot.runtime.api", "bot.runtime.live", "bot.runtime.legacy", "bot.runtime.pluggables", "bot.runtime.reload", "bot.runtime.snapshot",
    # bot.db's per-area functions (bot/storage/): part of bot.db, which is denylisted above for the connection it
    # owns; reloading a piece would leave bot.db re-exporting the stale functions.
    "bot.storage.agent_settings", "bot.storage.approvals", "bot.storage.chat", "bot.storage.devices",
    "bot.storage.extensions", "bot.storage.jobs", "bot.storage.kanban", "bot.storage.memory",
    "bot.storage.peers", "bot.storage.schedules", "bot.storage.server_chat", "bot.storage.sessions",
    "bot.storage.stats", "bot.storage.support_bot", "bot.storage.swarms", "bot.storage.telemetry",
    # Route modules split out of server.py: their routes are registered on the live app once, at build time;
    # reloading a module can't re-register them, same reason bot.dashboard.server itself is here.
    "bot.dashboard.routes.agent_control", "bot.dashboard.routes.android_apk", "bot.dashboard.routes.bots",
    "bot.dashboard.routes.chat", "bot.dashboard.chat_stream", "bot.dashboard.routes.delegation", "bot.dashboard.routes.devices",
    "bot.dashboard.routes.files", "bot.dashboard.routes.hermes_delegation", "bot.dashboard.routes.kanban",
    "bot.dashboard.routes.mobile_keys", "bot.dashboard.routes.mutations", "bot.dashboard.routes.ops_endpoints",
    "bot.dashboard.routes.pairing", "bot.dashboard.routes.peers", "bot.dashboard.routes.platforms",
    "bot.dashboard.routes.reads", "bot.dashboard.routes.schedules", "bot.dashboard.routes.server_chat",
    "bot.dashboard.routes.setup_wizard", "bot.dashboard.routes.shared_context", "bot.dashboard.routes.swarms",
    "bot.dashboard.routes.wasm_plugins",
    # The Sentinel holds live state (the running watchdog thread, the installed
    # log handler, alert de-duplication, the boot record). A reload would orphan
    # the old watchdog and double-count every error.
    "bot.sentinel.journal", "bot.sentinel.backup", "bot.sentinel.repair", "bot.sentinel.cve",
    "bot.sentinel.security", "bot.sentinel.bug_hunter", "bot.sentinel.watchdog", "bot.sentinel.bootguard",
    "bot.sentinel.guardian", "bot.dashboard.sentinel_api", "bot.dashboard.editors_api",
    "bot.provider_store",  # owns a lock and the path of the on-disk provider store; a reload would orphan both
    "bot.envfile", "bot.handlers", "bot.outbox", "bot.plugins", "bot.attachments",
    # The wasm plugin registry holds live compiled components (an engine reference, a few MB
    # each) and a module-level dict of them; a reload would orphan both, the way the MCP
    # client above loses its connections.
    "bot.wasm_plugins.manager", "bot.wasm_plugins.tools", "bot.wasm_plugins.runtime",
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
    "bot.agent_runtime.persistent_shell",  # holds the live shell processes
    "bot.agent_runtime.web",  # registers its tools at import time
    # errors.py defines ToolError, which callers catch by identity: a reloaded copy would
    # be a different class and their `except ToolError` would stop matching.
    "bot.agent_runtime.errors", "bot.agent_runtime.state",
    "bot.agent_runtime.text_toolcalls",  # holds the per-model tool-call counters (native / text / repaired / failed)
    # Security layer: permissions.py holds the run-mode ContextVar, taint.py the set of tainted
    # sessions, secrets_guard.py the injected-secret table, mcp_pins.py its warn-once set, and
    # sandbox.py is only ever imported by the (denied) shell module. A reload would reset them.
    "bot.agent_runtime.permissions", "bot.agent_runtime.taint", "bot.agent_runtime.secrets_guard",
    "bot.agent_runtime.mcp_pins", "bot.agent_runtime.sandbox", "bot.agent_runtime.win_job", "bot.agent_runtime.appcontainer", "bot.agent_runtime.wsl_vm",
    "bot.agent_runtime.net_allowlist",  # imported by sandbox.py (denied above); its classes must stay the ones sandbox.py validates against
    # The Sandbox Nervous System: guard.py replaces subprocess.Popen process-wide (a reload would
    # either install a second wrapper or restore the original and silently stop being windowless),
    # and registry.py holds the live process table, the event ring buffer, the sampler thread and
    # the run id whose entries live.json is keyed by. cell.py owns job handles and the cells the
    # reflexes act on. Reloading any of them would orphan exactly the state this denylist exists
    # to protect.
    "bot.sandbox_ns.guard", "bot.sandbox_ns.registry", "bot.sandbox_ns.cell", "bot.sandbox_ns.spawn",
    "bot.sandbox_ns.policy", "bot.sandbox_ns.reaper", "bot.sandbox_ns.reflexes",
    # wasm.py's cells live in that registry: a reloaded WasmCell class would not match the live ones.
    "bot.sandbox_ns.wasm",
    "bot.redblue.schemas", "bot.redblue.verifier", "bot.redblue.events", "bot.redblue.detector", "bot.redblue.watchdog", "bot.redblue.taxonomy",  # P0 foundation + P1 WatchDog detector: live event feed, canary sinks, rule engine; no reload path yet
    # The routes into that registry: they register once, and a reloaded copy would serve its own
    # (empty) idea of what is running.
    "bot.dashboard.sandbox_ns_api",
    "bot.agent_runtime.settings_schema",  # imports permissions/sandbox (both denied above); a reload would split the classes it validates against
    "bot.agent_runtime.project_rules",  # imported by prompt.py; reloaded with it is not needed and is safe to skip
    "bot.agent_runtime.code_intel",  # running language servers are process state
    "bot.capacity.governor",  # holds the lock and the dedupe table that keep concurrent signals from double counting
    "bot.capacity.events", "bot.capacity.probe", "bot.capacity.autopack",  # the live event ring and listeners, the probe loop and its sender, the contextvar of the running pack
    "bot.model_router", "bot.agent_runtime.trajectory",  # registers a tool / reads the trace store
    # The router's store serialises writes with a module lock, and policy/learn cache the policy and the trained
    # classifier; a reload mid-turn would split the lock and serve a stale policy until the cache expired.
    "bot.router_brain.store", "bot.router_brain.policy", "bot.router_brain.learn", "bot.dashboard.router_api",
    # Unsloth: harness.py holds the lock and in-flight table that make concurrent on-demand loads share one load, and
    # tools.py registers tools at import time; client.py caches where Studio is and its API description.
    "bot.unsloth.client", "bot.unsloth.harness", "bot.unsloth.tools", "bot.dashboard.unsloth_api",
    # Ollama: harness.py holds the background pull/push/create jobs, tools.py registers tools at import time.
    "bot.ollama.client", "bot.ollama.harness", "bot.ollama.tools", "bot.dashboard.ollama_api",
    # ModelMistress: client.py caches where the hub is, tools.py registers tools at import time.
    "bot.modelmistress.client", "bot.modelmistress.harness", "bot.modelmistress.supervisor", "bot.modelmistress.tools", "bot.dashboard.modelmistress_api",
    # VM-Harness: harness.py holds the background setup/update jobs, tools.py registers tools at import time.
    "bot.vm_harness.client", "bot.vm_harness.harness", "bot.vm_harness.tools", "bot.dashboard.vm_harness_api",
    # Hermes Manager: harness.py holds the background setup/update jobs, tools.py registers tools at import time.
    "bot.hermes_manager.client", "bot.hermes_manager.harness", "bot.hermes_manager.tools", "bot.dashboard.hermes_manager_api",
    # TransferDaemon: harness.py holds the background build/update jobs, tools.py registers tools at import time.
    "bot.transferdaemon.client", "bot.transferdaemon.harness", "bot.transferdaemon.tools", "bot.dashboard.transferdaemon_api",
    # overlay.py's lock and status/route cache: a reload would split the lock and serve an empty cache.
    "bot.transferdaemon.overlay",
    # Modules: harness.py holds the background jobs, registry.py the manifest cache, tools.py registers at import time.
    "bot.modules.manifest", "bot.modules.registry", "bot.modules.client", "bot.modules.adapters", "bot.modules.harness",
    "bot.modules.tools", "bot.modules.conformance", "bot.modules.adoption", "bot.modules.refresh", "bot.modules.catalog", "bot.modules.assist", "bot.modules.panel",
    "bot.dashboard.modules_api",
    # Integration keys: the gate middleware is registered once at startup.
    "bot.integrations", "bot.dashboard.integrations_api",
    # Git stacks: the poller thread and the deploy lock; the routes register once.
    "bot.git_stacks", "bot.dashboard.git_stacks_api",
    # Vision: routes register once, tools.py registers at import time, pipelines.py caches loaded models, zoo.py and
    # dnn.py hold locks and the device fallbacks.
    "bot.dashboard.vision_api", "bot.vision.tools", "bot.vision.pipelines", "bot.vision.zoo", "bot.vision.dnn",
    "bot.vision.service", "bot.vision.images", "bot.vision.ops",
    # Hosting: routes register once and hosting_api.py holds the runs; tools.py registers at import time; store.py
    # holds the write lock, upnp.py the discovered router.
    "bot.dashboard.hosting_api", "bot.hosting.tools", "bot.hosting.store", "bot.hosting.upnp", "bot.hosting.service",
    # File server: routes register once; tools.py registers at import time; store.py holds the write lock, shares.py
    # the sign-in throttle, index.py the embedder choice.
    "bot.dashboard.fileserver_api", "bot.fileserver.tools", "bot.fileserver.store", "bot.fileserver.shares",
    "bot.fileserver.index", "bot.fileserver.service",
    # Local AI and the Neural Lab: routes register once; engine.py holds the loaded models, telemetry.py and systune.py
    # their caches and the recorder, service.py the recorder thread; tools.py registers at import time.
    "bot.dashboard.localai_api", "bot.localai.engine", "bot.localai.service", "bot.localai.tools", "bot.neurallab.telemetry",
    "bot.neurallab.systune", "bot.neurallab.service", "bot.neurallab.infer",
    # The rest of hosting, the file server, local AI and the lab, and their TUI screens: separate processes (the file
    # server, the edge, the model server, GPU workers) or state held across calls; they take effect on restart.
    "bot.hosting.accounts", "bot.hosting.acme", "bot.hosting.caddy", "bot.hosting.deploy", "bot.hosting.dns",
    "bot.hosting.edge", "bot.hosting.netinfo", "bot.hosting.procs", "bot.hosting.tunnels", "bot.hosting.vps",
    "bot.fileserver.apps", "bot.fileserver.array", "bot.fileserver.backup", "bot.fileserver.dav", "bot.fileserver.disks",
    "bot.fileserver.exports", "bot.fileserver.gf", "bot.fileserver.guard", "bot.fileserver.mover", "bot.fileserver.server",
    "bot.fileserver.transfer", "bot.localai.discover", "bot.localai.gguf", "bot.localai.mesh", "bot.localai.modelfile",
    "bot.localai.models", "bot.localai.paths", "bot.localai.pull", "bot.localai.server", "bot.localai.train",
    "bot.localai.train_worker", "bot.localai.train_forge",
    "bot.neurallab.edn", "bot.neurallab.interop", "bot.neurallab.lab", "bot.neurallab.nn_worker", "bot.neurallab.spec",
    "bot.tui.screens.hosting", "bot.tui.screens.localai", "bot.tui.screens.storage",
    # The memory fabric: routes register once; store.py and knowledge.py hold their table set-up and the embedder,
    # diff.py the git lock, service.py the daily close; the rest is used by those.
    "bot.dashboard.memory_api", "bot.memoryfabric.store", "bot.memoryfabric.knowledge", "bot.memoryfabric.sources",
    "bot.memoryfabric.vault", "bot.memoryfabric.diff", "bot.memoryfabric.service", "bot.memoryfabric.extract",
    "bot.memoryfabric.rules",
    # The context vault: the singleton and its worker thread, the SQLite handle, the RAM tier and the registered tool; the
    # capture hooks are imported lazily by denied modules.
    "bot.context_vault.store", "bot.context_vault.tiers", "bot.context_vault.capture", "bot.context_vault.tools",
    "bot.context_vault.settings", "bot.context_vault.search", "bot.context_vault.redact", "bot.context_vault.embed",
    "bot.context_vault.__main__", "bot.dashboard.context_vault_api",
    # The user model: the routes register once; user_model.py keeps its table-ready marker, and is imported by denied modules.
    "bot.dashboard.user_model_api", "bot.agent_runtime.user_model",
    # Privacy mode: routes register once; privacy.py caches its settings file. TokenJuice keeps compacted originals.
    "bot.dashboard.privacy_api", "bot.privacy", "bot.agent_runtime.tokenjuice",
    # The DNS resolver keeps its DNS-over-HTTPS connection open.
    "bot.resolver",
    # Studio: routes register once and studio_api.py holds the generation jobs; variants.py holds its write lock; gate.py runs sandbox cells.
    "bot.dashboard.studio_api", "bot.studio.variants", "bot.studio.log", "bot.studio.generate", "bot.studio.edits",
    "bot.studio.tokens", "bot.studio.shots", "bot.studio.gate", "bot.studio.km",
    # Cluster: offer.py holds the budget's reservations, executor.py the running jobs, store.py its connection,
    # membership.py the peers' reports; blobs.py the blob store and its pins; tools.py registers at import time.
    "bot.cluster.blobs", "bot.cluster.inventory", "bot.cluster.offer", "bot.cluster.store", "bot.cluster.executor", "bot.cluster.membership",
    "bot.cluster.scheduler", "bot.cluster.tools", "bot.dashboard.cluster_api",
    # The toolkit registers its actions and agent tools at import time.
    "bot.agent_runtime.toolkit_tools",
    # Power: power.py holds the keep-awake thread and its holds; power_tools.py registers tools at import time.
    "bot.power", "bot.power_tools", "bot.dashboard.power_api",
    "bot.nodes", "bot.canvas", "bot.voice", "bot.platforms._relay",
    "bot.platforms.email_platform", "bot.platforms.sms_platform", "bot.platforms.signal_platform", "bot.platforms.imessage_platform",
    "bot.platforms.googlechat_platform", "bot.platforms.teams_platform", "bot.platforms._jwt",  # long-running adapters
    "bot.platforms._voice",  # loaded by the long-running Discord/Slack adapters, which are not reloaded
    "bot.platforms._stream_edit",  # likewise (Telegram handlers, Discord, Slack, Matrix; Teams and Google Chat via _relay)
    "bot.agent_runtime.browser", "bot.vault", "bot.routines", "bot.approvals_view",  # a running browser / encrypted store / registered tools
    # Routines: the routes register once, and routines_view.py holds the run-history bookkeeping
    # a page's "run now" writes into.
    "bot.dashboard.routines_api", "bot.routines_view",
    # Shortcuts: the routes register once, so a reloaded copy would serve its own idea of the
    # saved bindings while the live app keeps the old app's routes.
    "bot.dashboard.shortcuts_api",
    # Rules (bot/rules): engine.py holds the compiled-rule cache and the one-shot "is an engine
    # installed" answer, store.py owns the module's import of bot.db, and the routes register once -
    # all three would split a running turn from the table it is reading.
    "bot.rules.engine", "bot.rules.store", "bot.rules.integration", "bot.rules.context",
    "bot.dashboard.routes.rules",
    "bot.agent_runtime.session_export", "bot.backends.external_agent_backend",  # imported by denied modules (dashboard, router)
    "bot.model_catalog", "bot.agent_runtime.usage_limits", "bot.agent_runtime.model_tools",  # usage counters and per-process caches
    "bot.agent_runtime.turn_budget",  # holds the contextvar of the running turn's meter; a reloaded copy would not see it
    "bot.custom_commands", "bot.agent_runtime.skill_learning", "bot.agent_runtime.skill_draft_eval",  # small, stateless, but imported by denied modules only
    "bot.skill_packs", "bot.skill_install",  # skill_packs registers read_skill_file at import time
    "bot.agent_runtime.agent_defs", "bot.agent_runtime.worktrees",  # agent_defs registers a tool at import time
    # Code index (bot/codeintel): per-repository write locks, the schema-verified set and the token counters live in
    # module state, agent_tools.py registers its tools at import time, and the API registers its routes once.
    "bot.codeintel.store", "bot.codeintel.indexer", "bot.codeintel.parsers", "bot.codeintel.treesitter", "bot.codeintel.tools",
    "bot.codeintel.agent_tools", "bot.codeintel.roots", "bot.codeintel.bench", "bot.codeintel.cli", "bot.codeintel.__main__",
    "bot.dashboard.codeintel_api",
    # Memory graph: tools.py registers memory_relevant at import time; the rest is imported lazily by the trace hook.
    "bot.memory_graph.tools", "bot.memory_graph.builder", "bot.memory_graph.recall", "bot.memory_graph.settings",
    "bot.memory_graph.store", "bot.memory_graph.tasktype",
    "bot.agent_runtime.run_tests", "bot.agent_runtime.repo_map", "bot.agent_runtime.ext_browser", "bot.agent_runtime.search_index",  # register their tools at import time; repo_map caches
    "bot.agent_runtime.small_model",  # holds the set of sessions already told that small-model mode is on
    "bot.agent_runtime.context_window",  # holds the learned chars-per-token ratio per model
    "bot.hotreload",  # never reload the reloader mid-cycle
    "bot.mcp_server",  # a separate process (python -m bot.mcp_server); not part of this one anyway
    "bot.tui.app", "bot.tui.client", "bot.tui.units", "bot.tui.__main__",  # a separate process (python -m bot.tui); not part of this one anyway
    "bot.tui.screens.connect", "bot.tui.screens.bot_list", "bot.tui.screens.add_bot", "bot.tui.screens.bot_detail",
    "bot.tui.screens.chat", "bot.tui.screens.providers", "bot.tui.screens.agent_settings",
    "bot.tui.screens.swarms", "bot.tui.screens.sessions", "bot.tui.screens.ssh_toolkit", "bot.tui.screens.infra", "bot.tui.screens.manage",
    "bot.editor_integrations", "bot.ssh_toolkit",  # shell out to a separately maintained tool; nothing to reload mid-run either way
    "bot.browser_bridge",  # holds live extension connections and pending pairings; a reload would drop them
    "bot.terminal_broker",  # holds live PTY/socket sessions; a reload would orphan them
    "bot.infra_automation",  # its run_forever() loop is a live background task holding the in-flight tick
    "bot.ssh_session_monitor",  # holds live in-flight session/subprocess state; a reload would orphan it
    "bot.dashboard_client",  # only ever imported by the tui/cli processes above, or abp_cli (its own separate process)
    "bot.swarm.base", "bot.swarm.strategies", "bot.swarm.engine",
    "bot.swarm.board_run",  # strategies hold its Step/SwarmBoard classes; reloading one without the other would split them
    # The work board: cached boards and probe result (package __init__), the ContextVars that carry a running work session
    # and turn, live heartbeat tasks, and the BoardError class that every module catches by identity.
    "bot.coordination.model", "bot.coordination.base", "bot.coordination.local", "bot.coordination.sentinel",
    "bot.coordination.session", "bot.coordination.fileguard", "bot.coordination.subagent_board",
    "bot.coordination.hermes_board",  # holds the live WorkSession of a Hermes dispatch in flight
    "bot.coordination.watch",  # its run_forever() loop is a live background task holding the check state
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
    # write is lock-guarded) � nothing worth re-executing mid-run, and a reload
    # would only re-read the same file.
    "bot.server_identity",
    # The leader lease owns an OS file lock (held for as long as this process
    # leads) plus the running-singleton controller, and the dashboard's lease
    # routes reach that controller through the module-level `_controller`
    # singleton. A reload would rebind the module name to a fresh copy with no
    # lock and no controller: /api/lease would answer from the new module while
    # the old one keeps the singletons running, and the release/take the gate
    # does mid-swap would go nowhere. New code reaches this machine through the
    # gate's hot swap instead, which restarts the process on purpose.
    "bot.lease",
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
    "bot.poller_claim",  # pure path/key functions; each held claim lives on its runner task, not the module
    "bot.config_backups",  # constants and file functions; callers reach it as `config_backups.<fn>`, nothing cached
    "bot.network_info",
    "bot.platform_guides",
    "bot.personas",
    "bot.bot_instances",  # depends only on validators/personas — before pairing/memory, which import it at module scope
    "bot.model_pricing",
    "bot.capacity.classify", "bot.capacity.handoff", "bot.capacity.routing", "bot.capacity.follow",  # pure functions over the governor's state; reached as module attributes
    "bot.conductor.store", "bot.conductor.cache", "bot.conductor.proof", "bot.conductor.llm", "bot.conductor.shards", "bot.conductor.navctx", "bot.conductor.planner", "bot.conductor.reviewer", "bot.conductor.integrator", "bot.conductor.pusher", "bot.conductor.confine", "bot.conductor.run",  # the conductor: a connection per call, no state held in the modules
    "bot.manager.store", "bot.manager.escalations", "bot.manager.digest", "bot.manager.checkin",  # the manager channel: reads the board/conductor/capacity; a connection per call, no state held in the modules
    "bot.models",
    "bot.push",
    "bot.desktop",
    "bot.pairing",
    "bot.thumbnails",
    "bot.firewall",
    "bot.retention",
    "bot.storage_tiering",  # stateless: every call takes the state root and settings, the index is opened per call
    "bot.peers",
    "bot.cache",  # CacheIt's client: a reload only drops its pooled connection and its cached hub address
    "bot.hotcache",  # the hot paths' hit/miss counters (in-process); a reload would zero them
    "bot.tailscale_mgr",
    "bot.browser_policy",
    "bot.native_host",
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
    "bot.hermes_gateway",  # which program owns a bot token; only a 5 s cache, but it sits beside hermes_config
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
    "bot.gpu",  # pure VRAM probe (DXGI via ctypes), no bot-internal deps
    "bot.agent_runtime.gpu_tool",  # the gpu_status tool; imports bot.gpu, imported by tools.py below
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
_last_units: list[dict[str, Any]] = []      # the latest unit swaps (and failures), newest first
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
    out = {"enabled": enabled, "degraded": _degraded, "recent_events": list(_last_events), "units": list(_last_units)}
    try:
        from bot.runtime import live

        if live._engine is not None and live.booted:
            last = live._engine.last_transaction
            out["runtime_last_transaction"] = last.to_dict() if last else None
    except Exception:  # noqa: BLE001
        pass
    return out


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


async def _module_cycle(
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

    _scrub_unloaded_tools()
    detail = f"{len(changed_modules)} file(s) changed -> reloaded {len(reloaded)} module(s)"
    if restarted_summary:
        detail += f"; restarted {', '.join(restarted_summary)}"
    _record("applied", detail)
    return {"status": "applied", "detail": detail}


def _scrub_unloaded_tools() -> None:
    """A module reload can re-register tools as an import side effect; those of units that are not running go again."""
    try:
        from bot.runtime import legacy, live

        if live.booted:
            legacy.scrub_inactive_tools()
    except Exception:  # noqa: BLE001
        logger.exception("hot-reload: could not scrub the tools of unloaded units")


async def _reload_units(paths: list[Path]) -> dict[str, Any]:
    from bot.runtime import reload as runtime_reload

    return await runtime_reload.reload_units(paths)


def _unit_summary(units: list[dict[str, Any]]) -> dict[str, Any]:
    bad = [u for u in units if not u.get("ok", True)]
    detail = "; ".join(str(u.get("detail") or u.get("unit")) for u in units)
    return {"status": "unit_failed" if bad else "applied", "detail": detail}


async def run_cycle(changed_files: list[Path], *, unit_reload: Optional[Callable[[list[Path]], Awaitable[dict[str, Any]]]] = None,
                    **kwargs: Any) -> dict[str, Any]:
    """One reload cycle. Files that belong to a unit (a feature area, extension or plugin) are first handed to
    `unit_reload`, which swaps the unit through the runtime engine (bot/runtime/reload.py): all-or-nothing, the old
    version keeps serving if the new one fails, and a failure there never degrades later cycles. The remaining files
    take the module-reload path (`_module_cycle`), where kernel and denylisted modules need a restart."""
    unit_results: list[dict[str, Any]] = []
    rest = list(changed_files)
    if unit_reload is not None:
        try:
            out = await unit_reload(list(changed_files))
        except Exception as exc:  # noqa: BLE001 - never takes the watcher down
            logger.exception("hot-reload: unit reload failed")
            out = {"handled": [], "units": [{"unit": "?", "ok": False, "action": "swap",
                                              "detail": f"unit reload raised {type(exc).__name__}: {exc}"}]}
        handled = {Path(p) for p in out.get("handled", [])}
        unit_results = list(out.get("units", []))
        rest = [p for p in changed_files if Path(p) not in handled]
        for u in unit_results:
            if u.get("action") == "unchanged":
                continue
            kind = "unit_failed" if not u.get("ok", True) else ("unit_dropped" if u.get("action") == "dropped" else "unit_swapped")
            _record(kind, str(u.get("detail") or u.get("unit")))
            _last_units.insert(0, {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), **u})
        del _last_units[_MAX_EVENTS:]
    result = await _module_cycle(rest, **kwargs)
    if unit_results:
        summary = _unit_summary(unit_results)
        if result["status"] == "no_change":
            result = summary
        elif result["status"] == "applied":
            result = {"status": summary["status"], "detail": f"{summary['detail']}; {result['detail']}"}
        result["units"] = unit_results
    return result


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
    result = await run_cycle(paths, unit_reload=_reload_units, shutdown_backends=_shutdown_backends,
                             restart_instances_for_platform=_restart_platform_instances)
    try:                                   # extensions and plugins carry real file hashes: bring them up to date too
        from bot.runtime import live, pluggables

        if live.booted:
            await pluggables.sync(live.get_engine(), kinds=(pluggables.EXT, pluggables.PLUGIN))
    except Exception:  # noqa: BLE001
        logger.exception("hot-reload: syncing extensions and plugins failed")
    return result


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
            roots = [str(BOT_PKG_DIR)]
            try:
                from bot.runtime import pluggables

                roots += [str(r) for r in pluggables.watch_roots()]
            except Exception:  # noqa: BLE001
                pass
            async for changes in awatch(*roots):
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
                        unit_reload=_reload_units,
                        shutdown_backends=_shutdown_backends,
                        restart_instances_for_platform=_restart_platform_instances,
                    )
                except Exception:
                    logger.exception("hot-reload cycle failed for %s", changed_paths)
        except Exception:
            logger.exception("hot-reload file watcher crashed — restarting it")
            await asyncio.sleep(2)
