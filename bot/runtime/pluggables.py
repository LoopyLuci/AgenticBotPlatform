"""Extensions, Python plugins, WASM plugins and external MCP servers as units (docs/runtime.md, phase 3B).

    ext:<name>      a local extension (bot/extensions.py)         version = hash of the extension's files
    plugin:<name>   a Python plugin (bot/plugins.py)              version = hash of the plugin file
    wasm:<name>     a WebAssembly plugin (bot/wasm_plugins)       version = hash of its component
    mcp:<name>      an external MCP server (agent_runtime/mcp_client.py)   version = hash of its settings

Each is built beside whatever is live and committed by the runtime's normal transaction, so a new version that fails
to load, start or pass health leaves the old one serving. What each contributes:

* extension: routes (collected on a router, dispatched at their own `/api/...` paths), its MCP tools as agent tools,
  panels (kind "script"), CLI commands, the backend/integration config overlay and tasks (`api.add_task`).
* plugin / wasm plugin: a *registration* (`apply()` / `revert()`, run synchronously inside the commit) that publishes
  and retracts the plugin's own tool and command tables; wasm tools also go through the tool registry.
* mcp server: its tools through the tool registry, over a connection the unit owns (`open_private`).

`sync()` makes the runtime's manifest match what is on disk / in the database for these kinds; `desired()` is also
what the boot manifest starts from.
"""
from __future__ import annotations

import hashlib
import inspect
import json
import logging
import typing
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Optional

from fastapi import APIRouter, HTTPException

from bot.runtime.manifest import InstanceSpec, Manifest
from bot.runtime.unit import DEFAULT_INSTANCE, Contributions, Health, make_ref, split_ref

logger = logging.getLogger("bot.runtime.pluggables")

EXT, PLUGIN, WASM, MCP = "ext:", "plugin:", "wasm:", "mcp:"
KINDS = (EXT, PLUGIN, WASM, MCP)

# why a pluggable's last load failed, by unit ref (extensions also record theirs for GET /api/extensions)
errors: dict[str, str] = {}
_failed_versions: dict[str, str] = {}          # ref -> the version that failed (not retried until it changes)
_synced: set = set()
_auth: dict[str, Optional[Callable]] = {"read": None, "write": None}


def set_auth(read: Callable, write: Callable) -> None:
    """The dashboard's auth dependencies, handed to extensions' route registrations."""
    _auth["read"], _auth["write"] = read, write


async def _no_auth() -> None:
    raise HTTPException(status_code=503, detail="the dashboard's auth is not configured")


# ------------------------------------------------------------------ versions and discovery
_SKIP_DIRS = {"__pycache__", ".git", "node_modules", ".pytest_cache"}


def folder_version(folder: Path) -> str:
    """A content hash of every file under `folder` (names and bytes), skipping caches: an extension's version."""
    digest = hashlib.sha256()
    root = Path(folder)
    try:
        files = sorted(p for p in root.rglob("*") if p.is_file()
                       and not (_SKIP_DIRS & set(p.relative_to(root).parts)) and p.suffix != ".pyc")
        for path in files:
            digest.update(path.relative_to(root).as_posix().encode())
            digest.update(b"\0")
            digest.update(path.read_bytes())
            digest.update(b"\0")
    except OSError:
        return "v-unreadable"
    return "v-" + digest.hexdigest()[:12]


def ext_folders() -> dict[str, Path]:
    from bot import extensions

    root = extensions.extensions_root()
    if root is None:
        return {}
    return {p.name: p for p in sorted(root.iterdir()) if (p / extensions.MANIFEST_FILE).is_file()}


def _mcp_rows() -> dict[str, dict]:
    from bot import db

    try:
        return {r["name"]: _mcp_kwargs(r) for r in db.list_external_mcp_servers() if r["enabled"]}
    except Exception:  # noqa: BLE001 - no database yet (a fresh data folder): nothing to connect
        return {}


def _mcp_kwargs(row: Any) -> dict:
    from bot.agent_runtime import mcp_client

    return mcp_client._row_to_kwargs(row)


def _plugin_rows() -> dict[str, dict]:
    from bot import db

    try:
        return {r["name"]: dict(r) for r in db.list_plugin_rows() if r["enabled"]}
    except Exception:  # noqa: BLE001
        return {}


def _wasm_rows() -> dict[str, dict]:
    try:
        from bot.wasm_plugins import manager

        return {n: r for n, r in manager._load_state()["plugins"].items() if r.get("enabled")}
    except Exception:  # noqa: BLE001
        return {}


def desired(kinds: tuple = KINDS) -> dict[str, InstanceSpec]:
    """`unit@default` -> spec for everything on disk / in the database that should be loaded."""
    out: dict[str, InstanceSpec] = {}
    if EXT in kinds:
        for name, folder in ext_folders().items():
            out[make_ref(EXT + name)] = InstanceSpec(EXT + name, folder_version(folder))
    if PLUGIN in kinds:
        from bot import plugins

        for name, row in _plugin_rows().items():
            out[make_ref(PLUGIN + name)] = InstanceSpec(PLUGIN + name, plugins.file_version(Path(row["path"])))
    if WASM in kinds:
        from bot.wasm_plugins import manager

        for name, row in _wasm_rows().items():
            out[make_ref(WASM + name)] = InstanceSpec(WASM + name, manager.component_version(row))
    if MCP in kinds:
        for name, kwargs in _mcp_rows().items():
            blob = json.dumps(kwargs, sort_keys=True, default=str)
            out[make_ref(MCP + name)] = InstanceSpec(MCP + name, "v-" + hashlib.sha256(blob.encode()).hexdigest()[:12])
    return out


def version_of(unit: str) -> Optional[str]:
    """The version a reload of `unit` would load now (the swap endpoint's default), or None if it is not found."""
    spec = desired().get(make_ref(unit))
    return spec.version if spec else None


def refs_for_paths(paths: list) -> dict[str, list]:
    """`ext:`/`plugin:` unit refs whose files are among `paths` -> those paths (hot reload)."""
    out: dict[str, list] = {}
    resolved = [(Path(p).resolve(), Path(p)) for p in paths]
    try:
        for name, folder in ext_folders().items():
            root = folder.resolve()
            hit = [orig for res, orig in resolved if res == root or root in res.parents]
            if hit:
                out[make_ref(EXT + name)] = hit
    except OSError:
        pass
    for name, row in _plugin_rows().items():
        try:
            target = Path(row["path"]).resolve()
        except (OSError, KeyError):
            continue
        hit = [orig for res, orig in resolved if res == target]
        if hit:
            out[make_ref(PLUGIN + name)] = hit
    return out


def watch_roots() -> list:
    """Folders outside bot/ whose changes hot reload should see (the extensions folder)."""
    try:
        from bot import extensions

        root = extensions.extensions_root()
    except Exception:  # noqa: BLE001
        root = None
    return [root] if root is not None else []


def boot_instances() -> dict[str, InstanceSpec]:
    return desired()


# ------------------------------------------------------------------ syncing the manifest
def _owned(ref: str, kinds: tuple) -> bool:
    return any(ref.startswith(k) for k in kinds)


async def sync(engine: Any, kinds: tuple = KINDS):
    """Make the engine's manifest match disk / database for `kinds`: new things load, changed things swap, vanished
    things unload. A version that fails is reported (errors, GET /api/extensions) and the live one keeps serving;
    a version that failed is not retried until it changes. Returns the last transaction report (or None)."""
    from bot import extensions

    want = desired(kinds)
    report = None
    while True:
        live = engine.manifest
        instances = {r: s for r, s in live.instances.items() if not _owned(r, kinds)}
        for ref, spec in want.items():
            if _failed_versions.get(ref) == spec.version and ref not in live.instances:
                continue
            if _failed_versions.get(ref) == spec.version:
                spec = live.instances[ref]                     # keep what runs; this version already failed
            instances[ref] = spec
        for ref, cur in live.instances.items():           # other instances of a unit follow its default's version
            unit, name = split_ref(ref)
            base = instances.get(make_ref(unit))
            if _owned(ref, kinds) and name != DEFAULT_INSTANCE and base is not None:
                instances[ref] = InstanceSpec(cur.unit, base.version, dict(cur.settings))
        target = Manifest(live.config, instances)
        if engine.plan(target).empty:
            _synced.add((id(engine), _scope_key()))
            return report
        report = await engine.apply(target, label="sync " + ",".join(k.rstrip(":") for k in kinds))
        if report.ok:
            for ref in want:
                if _failed_versions.get(ref) == want[ref].version:
                    continue
                _failed_versions.pop(ref, None)
                errors.pop(ref, None)
                if ref.startswith(EXT):
                    extensions.clear_error(ref[len(EXT):].partition("@")[0])
            _synced.add((id(engine), _scope_key()))
            return report
        if not report.failed:
            return report                                        # a failure that is not one unit's; try again later
        if not any(ref in want for ref in report.failed):
            return report
        for ref, message in report.failed.items():
            errors[ref] = message
            if ref in want:
                _failed_versions[ref] = want[ref].version
            if ref.startswith(EXT):
                extensions.record_error(ref[len(EXT):].partition("@")[0], message)


def note_boot_failures(engine: Any) -> None:
    """After a boot that left failed units out: remember why (GET /api/extensions, the failed-version memory)."""
    from bot import extensions

    want = desired()
    for ref, message in engine._failures.items():
        if not _owned(ref, KINDS):
            continue
        errors[ref] = message
        if ref in want:
            _failed_versions[ref] = want[ref].version
        if ref.startswith(EXT):
            extensions.record_error(ref[len(EXT):].partition("@")[0], message)


async def reconnect(name: str) -> bool:
    """(Re)connect an external MCP server through the engine: unload its unit if loaded, then load it from the
    database row. True when the unit is live afterwards."""
    engine = _live().get_engine()
    ref = make_ref(MCP + name)
    _failed_versions.pop(ref, None)
    if ref in engine._active:
        await engine.unload(MCP + name)
        await engine.settle()
    await sync(engine, kinds=(MCP,))
    return ref in engine._active


async def disconnect(name: str) -> None:
    engine = _live().get_engine()
    if make_ref(MCP + name) in engine._active:
        await engine.unload(MCP + name)
        await engine.settle()


def _live() -> Any:
    from bot.runtime import live

    return live


def sync_from_sync_code(kind: str) -> None:
    """Called by plugins / wasm_plugins after they changed their own tables: make the manifest, generation and
    runtime.changed event say so (a transaction that adopts the version just published)."""
    try:
        live = _live()
        live.run_sync(lambda: sync(live.get_engine(), kinds=(kind,)))
    except Exception:  # noqa: BLE001 - the direct change already happened; the next sync repairs the manifest
        logger.exception("could not sync the %s units with the runtime", kind)


def _scope_key() -> str:
    from bot import extensions

    return str(extensions.private_dir() or "")


async def ensure_loaded(engine: Any) -> None:
    """Load the extensions once per engine and private folder (the ASGI route fallback and the extension file route
    call this, so a dashboard started without the lifespan still serves extension routes)."""
    if (id(engine), _scope_key()) in _synced:
        return
    _synced.add((id(engine), _scope_key()))
    await sync(engine, kinds=(EXT,))


def reset() -> None:
    errors.clear()
    _failed_versions.clear()
    _synced.clear()


# ------------------------------------------------------------------ extensions
class _Collector(APIRouter):
    """Stands in for the FastAPI app an extension's `register(app, read_auth, write_auth)` expects."""

    def __init__(self) -> None:
        super().__init__()
        self.state = SimpleNamespace()


_JSON_TYPES = {str: "string", int: "integer", float: "number", bool: "boolean", list: "array", dict: "object"}


def _tool_entry(func: Callable, name: str, origin: str = "registered") -> tuple:
    """(schema, spec, handler) for an extension's MCP-style tool function (docstring = description)."""
    from bot.agent_runtime import toolspec

    try:
        hints = typing.get_type_hints(func)
    except Exception:  # noqa: BLE001
        hints = {}
    props: dict[str, dict] = {}
    required: list[str] = []
    for param in inspect.signature(func).parameters.values():
        if param.kind in (param.VAR_POSITIONAL, param.VAR_KEYWORD):
            continue
        hint = hints.get(param.name)
        props[param.name] = {"type": _JSON_TYPES.get(typing.get_origin(hint) or hint, "string")}
        if param.default is param.empty:
            required.append(param.name)
    schema = {"name": name, "description": inspect.getdoc(func) or name,
              "input_schema": {"type": "object", "properties": props, "required": required}}

    async def handler(tool_input: dict, **_context: Any) -> str:
        kwargs = {k: v for k, v in (tool_input or {}).items() if k in props}
        out = func(**kwargs)
        if inspect.isawaitable(out):
            out = await out
        return out if isinstance(out, str) else json.dumps(out, default=str, ensure_ascii=False)

    return schema, toolspec.ToolSpec(name, "external", False, False, origin=origin), handler


def _literal_prefix(path: str) -> Optional[str]:
    literal = path.split("{", 1)[0].rstrip("/")
    parts = [p for p in literal.split("/") if p]
    if len(parts) < 2 or parts[0] != "api":
        return None                                     # only /api/<something>... can be dispatched
    return "/" + "/".join(parts)


class ExtensionUnit:
    """The Unit contract over one extension folder. The folder is read and `register(api)` run when the unit is
    built, so a broken new version fails the stage; the loaded code is kept, so a rollback reuses the old build."""

    def __init__(self, unit_id: str) -> None:
        from bot import extensions

        self.id = unit_id
        name = unit_id[len(EXT):]
        folder = ext_folders().get(name)
        if folder is None:
            raise LookupError(f"no extension folder named {name!r}")
        self.folder = folder
        self.ext = extensions._load_one(folder)
        self._replaced: Optional[Any] = None

    def contributions(self, ctx: Any) -> Contributions:
        api = self.ext.api
        read, write = _auth["read"] or _no_auth, _auth["write"] or _no_auth
        router = _Collector()
        for _name, register in api.routes:
            register(router, read, write)
        prefixes = sorted({p for route in router.routes
                           if (p := _literal_prefix(getattr(route, "path", ""))) is not None})
        if router.routes and not prefixes:
            logger.warning("extension %s: none of its routes is under /api/<name>, so none can be dispatched", api.name)
        overlay: dict[str, Any] = {}
        if api.backends:
            overlay["backends"] = {k: dict(v) for k, v in api.backends.items()}
        if api.integrations:
            overlay["integrations"] = {k: dict(v) for k, v in api.integrations.items()}
        panels = [{"id": p.id, "title": p.title, "group": "extensions", "icon": p.icon, "kind": "script",
                   "script": f"/extensions/{api.name}/{p.script}", "order": 100,
                   "styles": [f"/extensions/{api.name}/{s}" for s in p.styles]} for p in api.panels]
        return Contributions(
            routes=router if router.routes else None, route_prefixes=prefixes if router.routes else [],
            tools=[_tool_entry(fn, name) for fn, name in api.mcp_tools], panels=panels,
            cli=list(api.cli_commands), tasks=list(api.tasks), config_overlay=overlay)

    async def start(self, ctx: Any) -> None:
        from bot import extensions

        self._replaced = extensions._publish(self.ext)

    async def health(self, ctx: Any) -> Health:
        return Health("ok")

    async def stop(self, ctx: Any, *, deadline: float) -> None:
        from bot import extensions

        self.ext.retired = True                      # type: ignore[attr-defined] - a later abort must not bring it back
        replaced, self._replaced = self._replaced, None
        extensions._retire(self.ext, None if replaced is None or getattr(replaced, "retired", False) else replaced)


# ------------------------------------------------------------------ python plugins
class PluginUnit:
    def __init__(self, unit_id: str) -> None:
        from bot import db

        self.id = unit_id
        self.name = unit_id[len(PLUGIN):]
        row = db.get_plugin_row(self.name)
        if row is None:
            raise LookupError(f"no plugin named {self.name!r}")
        self.path = Path(row["path"])
        self.staged: Any = None
        self.adopted = False

    def contributions(self, ctx: Any) -> Contributions:
        from bot import plugins

        if self.staged is None or self.staged.torn:
            adopted = plugins.adopt(self.name, self.path) if self.staged is None else None
            self.adopted = adopted is not None
            self.staged = adopted or plugins.stage(self.name, self.path)
            if not self.adopted:
                self.staged.managed = True           # its tasks run in the unit's task group
        return Contributions(registrations=[self.staged], tasks=[] if self.adopted else list(self.staged.tasks))

    async def start(self, ctx: Any) -> None:
        return None

    async def health(self, ctx: Any) -> Health:
        return Health("ok")

    async def stop(self, ctx: Any, *, deadline: float) -> None:
        if self.staged is not None:
            self.staged.revert()                    # idempotent; calls the plugin's teardown() if it was published


# ------------------------------------------------------------------ wasm plugins
class WasmUnit:
    def __init__(self, unit_id: str) -> None:
        from bot.wasm_plugins import manager

        self.id = unit_id
        self.name = unit_id[len(WASM):]
        self.row = manager._row(self.name)
        self.staged: Any = None
        self.adopted = False

    def contributions(self, ctx: Any) -> Contributions:
        from bot.wasm_plugins import manager

        if self.staged is None or self.staged.closed:
            adopted = manager.adopt(self.name, self.row) if self.staged is None else None
            self.adopted = adopted is not None
            self.staged = adopted or manager.stage(self.name, self.row)     # no native module: raises with the reason
            if not self.adopted:
                self.staged.managed = True
        return Contributions(registrations=[self.staged], tools=list(self.staged.entries))

    async def start(self, ctx: Any) -> None:
        return None

    async def health(self, ctx: Any) -> Health:
        return Health("failed", "closed") if self.staged is None or self.staged.closed else Health("ok")

    async def stop(self, ctx: Any, *, deadline: float) -> None:
        if self.staged is not None:
            self.staged.revert()                    # unpublishes if needed and closes the handle


# ------------------------------------------------------------------ external MCP servers
_owners: dict[str, int] = {}


class McpUnit:
    """Start connects (over a connection this unit owns), stop disconnects; the server's tools are contributed once
    connected. (`contributions()` returns the list that `start()` then fills: the registry reads it after staging.)"""

    def __init__(self, unit_id: str) -> None:
        from bot.agent_runtime import mcp_client

        self.id = unit_id
        self.name = unit_id[len(MCP):]
        try:
            self.kwargs = mcp_client.server_kwargs(self.name)
        except KeyError as exc:
            raise LookupError(str(exc)) from exc
        self.conn: Any = None
        self._contrib: Optional[Contributions] = None

    def contributions(self, ctx: Any) -> Contributions:
        self._contrib = Contributions()
        return self._contrib

    async def start(self, ctx: Any) -> None:
        from bot.agent_runtime import mcp_client, mcp_pins, toolspec

        self.conn = await mcp_client.open_private(self.name, self.kwargs)
        _owners[self.name] = _owners.get(self.name, 0) + 1
        mcp_client.runtime_owned.add(self.name)
        conn, tools = self.conn, []
        for tool in conn.tools:
            if mcp_pins.observe(self.name, tool) == "changed":
                logger.warning("mcp server %s: tool %s changed since it was pinned; not offered", self.name, tool["name"])
                continue
            full = f"{mcp_client.TOOL_PREFIX}{self.name}_{tool['name']}"
            schema = {"name": full, "description": f"[{self.name}] {tool['description']}",
                      "input_schema": tool["input_schema"]}

            async def handler(tool_input: dict, _real=tool["name"], **_context: Any) -> str:
                return await conn.call(_real, tool_input)

            tools.append((schema, toolspec.ToolSpec(full, "external", False, False, origin="mcp"), handler))
        self._contrib.tools[:] = tools

    async def health(self, ctx: Any) -> Health:
        return Health("ok") if self.conn is not None and self.conn.open else Health("failed", "not connected")

    async def stop(self, ctx: Any, *, deadline: float) -> None:
        from bot.agent_runtime import mcp_client

        conn, self.conn = self.conn, None
        if conn is not None:
            await conn.close()
            left = _owners.get(self.name, 1) - 1
            if left <= 0:
                _owners.pop(self.name, None)
                mcp_client.runtime_owned.discard(self.name)
            else:
                _owners[self.name] = left


def register_factories(factories: Any) -> None:
    factories.register_prefix(EXT, lambda spec: ExtensionUnit(spec.unit))
    factories.register_prefix(PLUGIN, lambda spec: PluginUnit(spec.unit))
    factories.register_prefix(WASM, lambda spec: WasmUnit(spec.unit))
    factories.register_prefix(MCP, lambda spec: McpUnit(spec.unit))
