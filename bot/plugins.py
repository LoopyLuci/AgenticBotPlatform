"""A local, file-backed plugin system — lets a bit of Python already on
this machine's disk register new agent tools and/or slash commands into
BotServer without editing core code.

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
"""

from __future__ import annotations

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

    def __init__(self, plugin_name: str) -> None:
        self._plugin_name = plugin_name

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
        _tools[name] = {
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
            if other is not None and other != name:
                raise PluginError(f"command alias {alias!r} is already used by /{other}")
        cdef = CommandDef(
            name=name, description=description, category=category, aliases=tuple(aliases), args_hint=args_hint
        )
        _commands[name] = {"def": cdef, "handler": handler, "raw_args": bool(raw_args), "plugin": self._plugin_name}
        for alias in aliases:
            _command_aliases[alias] = name


def _unregister_plugin(name: str) -> None:
    for tname in [t for t, d in _tools.items() if d["plugin"] == name]:
        del _tools[tname]
    for cname in [c for c, d in _commands.items() if d["plugin"] == name]:
        cdef = _commands.pop(cname)["def"]
        for alias in cdef.aliases:
            _command_aliases.pop(alias, None)
    _loaded.pop(name, None)


def _load_module(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(f"botserver_plugin_{name}", path)
    if spec is None or spec.loader is None:
        raise PluginError(f"couldn't load {path} as a Python module")
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception as exc:
        raise PluginError(f"error while importing {path.name}: {exc}") from exc
    return module


def _activate(name: str, path: Path) -> ModuleType:
    """Loads and runs setup() for a plugin, leaving nothing registered on
    any failure (either a bad module or a setup() that raises)."""
    module = _load_module(name, path)
    setup_fn = getattr(module, "setup", None)
    if not callable(setup_fn):
        raise PluginError(f"{path.name} has no setup(api) function")
    api = PluginAPI(name)
    try:
        setup_fn(api)
    except Exception as exc:
        _unregister_plugin(name)
        raise PluginError(f"setup() raised: {exc}") from exc
    _loaded[name] = module
    return module


def install(path: str) -> dict:
    p = Path(path).expanduser()
    if not p.exists() or not p.is_file():
        raise PluginError(f"{path!r} does not exist or isn't a file")
    name = p.stem
    _validate_name(name, kind="plugin")
    if name in _loaded:
        _unregister_plugin(name)
    module = _activate(name, p)
    description = (getattr(module, "PLUGIN_DESCRIPTION", "") or "").strip() or "(no description)"
    db.install_plugin_row(name, str(p), description)
    return describe(name)


def enable(name: str) -> dict:
    row = db.get_plugin_row(name)
    if row is None:
        raise PluginError(f"no plugin named {name!r} — install it first")
    if name not in _loaded:
        _activate(name, Path(row["path"]))
    db.set_plugin_enabled(name, True)
    return describe(name)


def disable(name: str) -> dict:
    row = db.get_plugin_row(name)
    if row is None:
        raise PluginError(f"no plugin named {name!r}")
    _unregister_plugin(name)
    db.set_plugin_enabled(name, False)
    return describe(name)


def remove(name: str) -> bool:
    _unregister_plugin(name)
    return db.delete_plugin_row(name)


def describe(name: str) -> dict:
    row = db.get_plugin_row(name)
    return {
        "name": name,
        "description": row["description"] if row else "",
        "enabled": bool(row["enabled"]) if row else False,
        "path": row["path"] if row else "",
        "tools": sorted(t for t, d in _tools.items() if d["plugin"] == name),
        "commands": sorted(c for c, d in _commands.items() if d["plugin"] == name),
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
