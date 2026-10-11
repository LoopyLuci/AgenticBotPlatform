"""A local, file-backed plugin system — lets a bit of Python already on
this machine's disk register new agent tools and/or slash commands into
AgenticBotPlatform without editing core code.

Trust model (see docs/adr/0007-plugins-are-trusted-local-code.md): a
plugin is trusted local code that runs in-process with the full
privileges the app already has (the same boundary `run_shell` already
accepts — see bot/agent_runtime/tools.py's module docstring). This is
*not* a sandbox and *not* a marketplace: install() only ever loads a
`plugin.py` file already sitting on disk, mirroring bot/skills.py's own
"local install, not networked" precedent. Installing a plugin is exactly
as sensitive as handing someone shell access, because on this codebase's
own stated security model, it already implies that.

A plugin file defines a module-level `setup(api)` function. `api` is a
`PluginAPI` instance with two calls:

    api.register_tool(name, description, input_schema, handler,
                       dangerous=False)
    api.register_command(name, description, handler,
                          category="Plugins", args_hint="", aliases=())

Tool handlers are `async def handler(tool_input: dict, *, workspace:
Path, instance_id: Optional[int]) -> str`, matching the built-in tools in
bot/agent_runtime/tools.py exactly, so a plugin tool is indistinguishable
from a built-in one to the agent loop. Command handlers are
`async def handler(ctx: CmdContext, args: list[str]) -> str` (or take a
raw string if registered with `raw_args=True`), matching bot/commands.py's
existing cmd_* functions.

An optional module-level `PLUGIN_DESCRIPTION` string is shown in the
plugin list. `setup()` raising any exception aborts the install/enable
and leaves nothing registered.

A plugin may also define `teardown()`, called once when the plugin is disabled, removed, replaced or its
unit is unloaded, and its `setup(api)` may call `api.spawn(async_fn)` to run a background coroutine for as long as
the plugin is live (cancelled on teardown; under the unit runtime it runs in the unit's task group).

**Replacing a plugin is a swap.** The new version is loaded and its `setup()` run *beside* the live one without
touching any registry; only when that succeeded does one synchronous step retire the old version (calling its
`teardown()`) and publish the new one. A new version that fails to load leaves the old one serving.

**Versioning.** The plugin API has a version, `SDK_VERSION` (semantic: a breaking change to
what `setup(api)` and the handlers may rely on bumps the major number; additions bump the
minor). A plugin can say which versions it was written for with a module-level
`REQUIRES_SDK = ">=1.0,<2"` (clauses joined by commas; `>=`, `>`, `<=`, `<`, `==`, `!=`), and an
optional `PLUGIN_VERSION = "1.2.0"` shown in the plugin list. A plugin that requires a version
this build does not provide is refused with a message saying which version it needs and which
one is installed; a plugin that says nothing is assumed to want the 1.x line it was written
against. See docs/agents/plugin-sdk.md.
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import logging
import re
from pathlib import Path
from types import ModuleType
from typing import Any, Callable, Optional

from bot import db
from bot.slash_commands import ALIASES as BUILTIN_ALIASES
from bot.slash_commands import COMMAND_REGISTRY as BUILTIN_COMMANDS
from bot.slash_commands import CommandDef

logger = logging.getLogger("bot.plugins")

_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")

SDK_VERSION = "1.0"
_CLAUSE = re.compile(r"^\s*(>=|<=|==|!=|>|<)\s*(\d+(?:\.\d+)*)\s*$")


def _parse_version(text: str) -> tuple[int, ...]:
    parts = [int(p) for p in str(text).strip().split(".")]
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts)


def sdk_satisfies(requirement: str, version: str = SDK_VERSION) -> bool:
    """Does `version` meet a requirement like ">=1.0,<2"? Raises ValueError for one that cannot be read."""
    have = _parse_version(version)
    for clause in str(requirement).split(","):
        m = _CLAUSE.match(clause)
        if not m:
            raise ValueError(f"cannot read {clause.strip()!r} in REQUIRES_SDK (use >=, >, <=, <, ==, != and a version)")
        op, want = m.group(1), _parse_version(m.group(2))
        if not {">=": have >= want, ">": have > want, "<=": have <= want, "<": have < want, "==": have == want, "!=": have != want}[op]:
            return False
    return True

# name -> {"description", "input_schema", "handler", "dangerous", "plugin"}
_tools: dict[str, dict[str, Any]] = {}
# name -> {"def": CommandDef, "handler", "raw_args", "plugin"}
_commands: dict[str, dict[str, Any]] = {}
_command_aliases: dict[str, str] = {}
# plugin name -> loaded module, so re-installing/removing can be undone cleanly
_loaded: dict[str, ModuleType] = {}


class PluginError(Exception):
    pass


def _validate_name(name: str, *, kind: str) -> None:
    if not _NAME_RE.match(name or ""):
        raise PluginError(f"{kind} name {name!r} must be lowercase letters/digits/underscore, starting with a letter")


class PluginAPI:
    """Passed to a plugin's setup() function — the only surface a plugin
    is meant to touch. Never exposed as a bare module so a plugin can't
    reach into another plugin's registrations."""

    def __init__(self, plugin_name: str, staged: "Optional[StagedPlugin]" = None) -> None:
        self._plugin_name = plugin_name
        self._staged = staged if staged is not None else StagedPlugin(plugin_name, Path(""))

    def spawn(self, coro_fn: Callable[[], Any]) -> None:
        """Run `coro_fn()` as a background task while the plugin is live; it is cancelled when the plugin stops."""
        if not callable(coro_fn):
            raise PluginError("spawn() needs a callable returning a coroutine")
        self._staged.tasks.append(coro_fn)

    def register_tool(
        self,
        name: str,
        description: str,
        input_schema: dict,
        handler: Callable[..., Any],
        *,
        dangerous: bool = False,
    ) -> None:
        _validate_name(name, kind="tool")
        from bot.agent_runtime import tools as agent_tools

        if name in agent_tools.TOOL_SCHEMA_NAMES:
            raise PluginError(f"tool name {name!r} is already a built-in tool")
        existing = _tools.get(name)
        if existing is not None and existing["plugin"] != self._plugin_name:
            raise PluginError(f"tool name {name!r} is already registered by plugin {existing['plugin']!r}")
        self._staged.tools[name] = {
            "description": description,
            "input_schema": input_schema,
            "handler": handler,
            "dangerous": bool(dangerous),
            "plugin": self._plugin_name,
        }

    def register_command(
        self,
        name: str,
        description: str,
        handler: Callable[..., Any],
        *,
        category: str = "Plugins",
        args_hint: str = "",
        raw_args: bool = False,
        aliases: tuple[str, ...] = (),
    ) -> None:
        _validate_name(name, kind="command")
        for alias in aliases:
            _validate_name(alias, kind="command alias")
        if name in BUILTIN_COMMANDS or name in BUILTIN_ALIASES:
            raise PluginError(f"command name {name!r} collides with a built-in command")
        existing = _commands.get(name)
        if existing is not None and existing["plugin"] != self._plugin_name:
            raise PluginError(f"command name {name!r} is already registered by plugin {existing['plugin']!r}")
        for alias in aliases:
            if alias in BUILTIN_COMMANDS or alias in BUILTIN_ALIASES:
                raise PluginError(f"command alias {alias!r} collides with a built-in command")
            other = _command_aliases.get(alias)
            if other is not None and other != name and _commands.get(other, {}).get("plugin") != self._plugin_name:
                raise PluginError(f"command alias {alias!r} is already used by /{other}")
        cdef = CommandDef(
            name=name, description=description, category=category, aliases=tuple(aliases), args_hint=args_hint
        )
        self._staged.commands[name] = {"def": cdef, "handler": handler, "raw_args": bool(raw_args),
                                       "plugin": self._plugin_name}


class StagedPlugin:
    """One loaded version of a plugin, built beside the live one. `apply()` publishes it (and retires the version it
    replaces) in one synchronous step; `revert()` takes it out and calls the plugin's `teardown()`. Both are
    idempotent, so the unit runtime's registry and the direct install()/enable() path can share it."""

    def __init__(self, name: str, path: Path, module: Optional[ModuleType] = None, file_hash: str = "") -> None:
        self.name = name
        self.path = path
        self.module = module
        self.file_hash = file_hash
        self.tools: dict[str, dict] = {}
        self.commands: dict[str, dict] = {}
        self.tasks: list[Callable[[], Any]] = []
        self.applied = False
        self.torn = False
        self.managed = False                    # True: the unit runtime supervises tasks
        self._running: set = set()

    # -- publish / retract
    def apply(self) -> None:
        if self.applied:
            return
        old = _staged.get(self.name)
        if old is not None and old is not self:
            old.revert()
        for tname, entry in self.tools.items():
            _tools[tname] = {**entry, "staged": self}
        for cname, entry in self.commands.items():
            _commands[cname] = {**entry, "staged": self}
            for alias in entry["def"].aliases:
                _command_aliases[alias] = cname
        if self.module is not None:
            _loaded[self.name] = self.module
        _staged[self.name] = self
        self.applied, self.torn = True, False
        if not self.managed:
            self._start_tasks()

    def revert(self) -> None:
        if not self.applied:
            return
        self.applied, self.torn = False, True
        for tname in [t for t, d in _tools.items() if d.get("staged") is self]:
            del _tools[tname]
        for cname in [c for c, d in _commands.items() if d.get("staged") is self]:
            for alias in _commands.pop(cname)["def"].aliases:
                if _command_aliases.get(alias) == cname:
                    del _command_aliases[alias]
        if _staged.get(self.name) is self:
            del _staged[self.name]
            _loaded.pop(self.name, None)
        for task in list(self._running):
            task.cancel()
        self._running.clear()
        teardown = getattr(self.module, "teardown", None)
        if callable(teardown):
            try:
                teardown()
            except Exception:  # noqa: BLE001 - a plugin's cleanup failing must not block its removal
                logger.exception("plugin %r: teardown() raised", self.name)

    def _start_tasks(self) -> None:
        if not self.tasks:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            logger.warning("plugin %r: no event loop yet, its spawned tasks were not started", self.name)
            return
        for fn in self.tasks:
            task = loop.create_task(fn())
            self._running.add(task)
            task.add_done_callback(self._running.discard)


# plugin name -> the version currently published (the one whose tools/commands are in the tables above)
_staged: dict[str, StagedPlugin] = {}


def file_version(path: Path) -> str:
    """The content hash of a plugin file: its version for the unit runtime."""
    try:
        return "v-" + hashlib.sha256(Path(path).read_bytes()).hexdigest()[:12]
    except OSError:
        return "v-missing"


def _unregister_plugin(name: str) -> None:
    current = _staged.get(name)
    if current is not None:
        current.revert()
    for tname in [t for t, d in _tools.items() if d["plugin"] == name]:
        del _tools[tname]
    for cname in [c for c, d in _commands.items() if d["plugin"] == name]:
        cdef = _commands.pop(cname)["def"]
        for alias in cdef.aliases:
            _command_aliases.pop(alias, None)
    _loaded.pop(name, None)


def _load_module(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(f"agenticbotplatform_plugin_{name}", path)
    if spec is None or spec.loader is None:
        raise PluginError(f"couldn't load {path} as a Python module")
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception as exc:
        raise PluginError(f"error while importing {path.name}: {exc}") from exc
    return module


def stage(name: str, path: Path) -> StagedPlugin:
    """Load a plugin and run setup() into a StagedPlugin, touching no registry. Raises PluginError on any failure
    (bad module, wrong SDK, setup() raising), leaving everything that is live exactly as it was."""
    path = Path(path)
    module = _load_module(name, path)
    requirement = getattr(module, "REQUIRES_SDK", None)
    if requirement is not None:
        try:
            compatible = sdk_satisfies(str(requirement))
        except ValueError as exc:
            raise PluginError(str(exc)) from exc
        if not compatible:
            raise PluginError(f"{path.name} needs plugin SDK {requirement}, but this build provides {SDK_VERSION}")
    setup_fn = getattr(module, "setup", None)
    if not callable(setup_fn):
        raise PluginError(f"{path.name} has no setup(api) function")
    staged = StagedPlugin(name, path, module, file_version(path))
    try:
        setup_fn(PluginAPI(name, staged))
    except Exception as exc:
        raise PluginError(f"setup() raised: {exc}") from exc
    return staged


def adopt(name: str, path: Path) -> Optional[StagedPlugin]:
    """The version of `name` that is already published, if it was built from the same file contents."""
    current = _staged.get(name)
    if current is not None and current.applied and current.file_hash == file_version(path):
        return current
    return None


def _activate(name: str, path: Path) -> ModuleType:
    """Stage a plugin and publish it, retiring the version it replaces only after the new one loaded."""
    staged = stage(name, path)
    staged.apply()
    return staged.module


def _runtime_sync() -> None:
    """Tell the unit runtime what changed (a transaction: manifest, generation, runtime.changed). The direct
    stage/apply/revert above stays the mechanism; the units adopt what was just published."""
    from bot.runtime import pluggables

    pluggables.sync_from_sync_code(pluggables.PLUGIN)


def install(path: str) -> dict:
    p = Path(path).expanduser()
    if not p.exists() or not p.is_file():
        raise PluginError(f"{path!r} does not exist or isn't a file")
    name = p.stem
    _validate_name(name, kind="plugin")
    module = _activate(name, p)           # stage first: a failing new version leaves the old one serving
    description = (getattr(module, "PLUGIN_DESCRIPTION", "") or "").strip() or "(no description)"
    db.install_plugin_row(name, str(p), description)
    _runtime_sync()
    return describe(name)


def enable(name: str) -> dict:
    row = db.get_plugin_row(name)
    if row is None:
        raise PluginError(f"no plugin named {name!r} — install it first")
    if name not in _loaded:
        _activate(name, Path(row["path"]))
    db.set_plugin_enabled(name, True)
    _runtime_sync()
    return describe(name)


def disable(name: str) -> dict:
    row = db.get_plugin_row(name)
    if row is None:
        raise PluginError(f"no plugin named {name!r}")
    _unregister_plugin(name)
    db.set_plugin_enabled(name, False)
    _runtime_sync()
    return describe(name)


def remove(name: str) -> bool:
    _unregister_plugin(name)
    removed = db.delete_plugin_row(name)
    _runtime_sync()
    return removed


def describe(name: str) -> dict:
    row = db.get_plugin_row(name)
    return {
        "name": name,
        "description": row["description"] if row else "",
        "enabled": bool(row["enabled"]) if row else False,
        "path": row["path"] if row else "",
        "tools": sorted(t for t, d in _tools.items() if d["plugin"] == name),
        "commands": sorted(c for c, d in _commands.items() if d["plugin"] == name),
        "version": str(getattr(_loaded.get(name), "PLUGIN_VERSION", "") or ""),
        "requires_sdk": str(getattr(_loaded.get(name), "REQUIRES_SDK", "") or ""),
        "sdk_version": SDK_VERSION,
    }


def list_plugins() -> list[dict]:
    return [describe(row["name"]) for row in db.list_plugin_rows()]


def load_enabled() -> None:
    """Loads every plugin marked enabled — called once at startup. A
    plugin that fails to load is logged and skipped (left enabled in the
    DB, so fixing the file and restarting picks it back up) rather than
    taking the whole process down."""
    for row in db.list_plugin_rows():
        if not row["enabled"]:
            continue
        try:
            _activate(row["name"], Path(row["path"]))
        except PluginError as exc:
            logger.error("plugin %r failed to load: %s", row["name"], exc)


# ---------------------------------------------------------------- tools API

def has_tool(name: str) -> bool:
    return name in _tools


def tool_schemas() -> list[dict[str, Any]]:
    return [
        {"name": n, "description": d["description"], "input_schema": d["input_schema"]}
        for n, d in sorted(_tools.items())
    ]


def is_dangerous_tool(name: str) -> bool:
    d = _tools.get(name)
    return bool(d and d["dangerous"])


async def execute_tool(name: str, tool_input: dict, *, workspace: Path, instance_id: Optional[int]) -> str:
    d = _tools.get(name)
    if d is None:
        raise KeyError(name)
    return await d["handler"](tool_input, workspace=workspace, instance_id=instance_id)


# -------------------------------------------------------------- commands API

def resolve_command(raw: str) -> Optional[str]:
    raw = (raw or "").strip().lower()
    if raw in _commands:
        return raw
    return _command_aliases.get(raw)


def get_command(name: str) -> Optional[dict]:
    canonical = resolve_command(name)
    return _commands.get(canonical) if canonical else None


def list_command_defs() -> list[CommandDef]:
    return [d["def"] for d in _commands.values()]


def all_dispatchable_names() -> list[str]:
    return list(_commands.keys()) + list(_command_aliases.keys())
