"""Local extensions: everything an install adds that the public repo must not carry.

ABP's repo is public, so it holds nothing private or local: no machine paths, no private
integrations, no overlay for someone's own project. Everything a particular install needs on
top of a public ABP lives in one folder outside the checkout, named by the ABP_PRIVATE_DIR
environment variable:

    <ABP_PRIVATE_DIR>/config/<name>.yaml   deep-merged over the tracked config/<name>.yaml
    <ABP_PRIVATE_DIR>/catalog/<id>/        one more module-overlay root (next to catalog/)
    <ABP_PRIVATE_DIR>/extensions/<name>/   a Python package with register(api)
    <ABP_PRIVATE_DIR>/denylist.txt         terms that must never reach a public repo

With the variable unset, missing or pointing at nothing, every function here returns the empty
answer and ABP behaves exactly like a plain public install. Nothing in bot/ imports this module
at import time except where an empty answer is the normal case (bot/config.py merges overlays,
bot/modules/registry.py lists overlay roots), so an unset variable costs one env lookup.

An extension's register(api) receives an ExtensionAPI and may add backends or integrations,
dashboard routes, a UI panel, an abp_cli command, an MCP tool, module entries and test
directories. A failing extension is never fatal: the exception is logged, the extension is
recorded as failed (bot/dashboard/extensions_api.py serves that to the modules/extensions view),
and ABP carries on without it.

Inside the dashboard process each extension is a unit of the runtime (`ext:<name>`, bot/runtime/pluggables.py):
its routes go through the runtime's dispatcher, its tools, panels, CLI commands and config overlay through the
runtime's registries, and it can be reloaded from disk with
`POST /api/runtime/units/ext:<name>/swap` (the new version is the hash of its files). `load_all()` stays the
plain, cached discovery used by processes that have no runtime (abp_cli, the stdio MCP server).
"""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import logging
import os
import re
import sys
import threading
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

logger = logging.getLogger("bot.extensions")

PRIVATE_DIR_ENV = "ABP_PRIVATE_DIR"
MANIFEST_FILE = "abp_extension.toml"


def resolve_private_dir(raw: str) -> Optional[Path]:
    """The folder a raw ABP_PRIVATE_DIR value names, or None for a blank one. Pure, so the
    expansion rules are testable on their own: variables first (a "$HOME/abp-private" in a unit
    file is a natural thing to write), then a leading ~, then normalised."""
    text = (raw or "").strip()
    if not text:
        return None
    return Path(os.path.expandvars(text)).expanduser().resolve()


def private_dir(environ: Any = None) -> Optional[Path]:
    """The configured private folder, or None when there is none.

    A blank value, a value naming a folder that isn't there and a value naming a file all mean
    "no private folder" — a public install must never fail to start because of a stale
    environment variable."""
    env = os.environ if environ is None else environ
    path = resolve_private_dir(env.get(PRIVATE_DIR_ENV) or "")
    return path if path is not None and path.is_dir() else None


def config_overlay(name: str, environ: Any = None) -> Optional[Path]:
    """<ABP_PRIVATE_DIR>/config/<name>.yaml, when it exists. The tracked config/<name>.yaml is
    what it is merged over; a missing overlay is not an error, it's the public default."""
    root = private_dir(environ)
    if root is None:
        return None
    path = root / "config" / f"{name}.yaml"
    return path if path.is_file() else None


def catalog_root(environ: Any = None) -> Optional[Path]:
    """<ABP_PRIVATE_DIR>/catalog as a module-overlay root, when it exists."""
    root = private_dir(environ)
    if root is None:
        return None
    path = root / "catalog"
    return path if path.is_dir() else None


def extensions_root(environ: Any = None) -> Optional[Path]:
    """<ABP_PRIVATE_DIR>/extensions, when it exists."""
    root = private_dir(environ)
    if root is None:
        return None
    path = root / "extensions"
    return path if path.is_dir() else None


def denylist_path(environ: Any = None) -> Optional[Path]:
    """<ABP_PRIVATE_DIR>/denylist.txt — the private list scripts/leak_guard.py reads. The list
    itself never enters ABP, so it is only ever read from here."""
    root = private_dir(environ)
    if root is None:
        return None
    path = root / "denylist.txt"
    return path if path.is_file() else None


# --------------------------------------------------------------------------------------
# Config overlays
# --------------------------------------------------------------------------------------

_APPEND_SUFFIX = "+"


def merge_overlay(base: Any, overlay: Any) -> Any:
    """Deep-merge one config document over another. Three rules, and only three:

      * a mapping merges key by key, recursively;
      * a list in the overlay REPLACES the tracked list, unless the overlay key ends in '+'
        (e.g. `security.allowed_origins+:`), which appends instead — replacing is the safer
        default for a list that grants anything;
      * null deletes the key, which is how a private folder turns off a tracked default.

    Anything that is not a mapping at the top is replaced outright: a partial list of backends
    is a list of backends, not a patch.
    """
    if not isinstance(base, dict) or not isinstance(overlay, dict):
        return copy.deepcopy(overlay)
    out: dict[str, Any] = copy.deepcopy(base)
    for key, value in overlay.items():
        if value is None:
            out.pop(key, None)
            continue
        append = isinstance(key, str) and key.endswith(_APPEND_SUFFIX)
        name = key[:-1] if append else key
        if append and isinstance(value, list) and isinstance(out.get(name), list):
            out[name] = out[name] + copy.deepcopy(value)
        elif isinstance(value, dict) and isinstance(out.get(name), dict):
            out[name] = merge_overlay(out[name], value)
        else:
            out[name] = copy.deepcopy(value)
    return out


def load_config_overlay(name: str, environ: Any = None) -> dict:
    """The private overlay document for config/<name>.yaml, or {} when there is none."""
    import yaml

    path = config_overlay(name, environ)
    if path is None:
        return {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return yaml.safe_load(fh) or {}
    except (OSError, yaml.YAMLError):
        # A broken overlay must not take ABP down: the tracked defaults stay live and the
        # pipeline's leak-guard step reports the file.
        logger.exception("couldn't read the private config overlay %s", path)
        return {}


def merged_config(name: str, data: dict, environ: Any = None) -> dict:
    """The tracked config/<name>.yaml document with the private overlay and every extension's
    own contributions applied over it, in that order. Extensions can therefore add or override
    a value without the private folder having to restate it."""
    out = merge_overlay(data, load_config_overlay(name, environ))
    return merge_overlay(out, _backend_overlay())


def _backend_overlay() -> dict:
    """The config fragment every loaded extension asked for through
    ExtensionAPI.add_backend()/add_integration(). {} when no extension registered one."""
    return _state().loaded.config_overlay


# --------------------------------------------------------------------------------------
# The extension API
# --------------------------------------------------------------------------------------


@dataclass
class Panel:
    """One UI panel an extension contributes: a section in the navigation whose script and
    stylesheet are served out of the extension's own folder, so nothing is copied into ABP."""

    id: str
    title: str
    description: str = ""
    script: str = "panel.js"
    styles: list[str] = field(default_factory=list)
    icon: str = "◆"


@dataclass
class Extension:
    """One loaded extension: what its manifest declared and what it registered."""

    name: str
    version: str
    description: str
    directory: Path
    api: "ExtensionAPI"

    def describe(self) -> dict:
        return {
            "name": self.name,
            "version": self.version,
            "description": self.description,
            "path": str(self.directory),
            "backends": sorted(self.api.backends),
            "integrations": sorted(self.api.integrations),
            # Counted and named, not sorted as-is: two (name, callable) pairs or two command dicts do not compare.
            "routes": len(self.api.routes),
            "panels": [p.id for p in self.api.panels],
            "cli_commands": sorted(c["name"] for c in self.api.cli_commands),
            "mcp_tools": sorted(name for _func, name in self.api.mcp_tools),
            "modules": [str(m.get("id", "")) for m in self.api.modules],
            "test_dirs": [str(p) for p in self.api.test_dirs],
            "backend_classes": sorted(self.api.backend_classes),
            "integration_presets": sorted(self.api.integration_presets),
            "lab_importers": sorted(self.api.lab_importers),
            "lab_projects": sorted(self.api.lab_projects),
            "lab_families": sorted(self.api.lab_families),
            "memory_sources": sorted(self.api.memory_sources),
            "model_locations": [m["app"] for m in self.api.model_locations],
            "lab_worker_plugins": [str(p) for p in self.api.lab_worker_plugins],
        }


class ExtensionAPI:
    """The stable surface an extension's register(api) is handed.

    Every method is a plain registration call: nothing here reaches into ABP's internals, and
    an extension that registers nothing is simply invisible. Raising from register() marks the
    extension failed (see load_all) — nothing already registered by an earlier, healthy
    extension is ever rolled back."""

    def __init__(self, name: str, version: str, description: str, directory: Path):
        self.name = name
        self.version = version
        self.description = description
        self.directory = directory
        self.backends: dict[str, dict] = {}
        self.integrations: dict[str, dict] = {}
        self.routes: list[tuple[str, Callable]] = []
        self.panels: list[Panel] = []
        self.cli_commands: list[dict] = []
        self.mcp_tools: list[tuple[Callable, str]] = []
        self.modules: list[dict] = []
        self.test_dirs: list[Path] = []
        self.backend_classes: dict[str, dict] = {}
        self.integration_scopes: dict[str, list[tuple[str, str]]] = {}
        self.integration_presets: dict[str, dict] = {}
        self.lab_importers: dict[str, dict] = {}
        self.lab_projects: dict[str, str] = {}
        self.lab_families: dict[str, dict] = {}
        self.memory_sources: dict[str, dict] = {}
        self.model_locations: list[dict] = []
        self.lab_worker_plugins: list[Path] = []
        self.provider_sources: list[Callable] = []
        self.hub_started_hooks: list[Callable] = []
        self.tasks: list[Callable] = []

    # -- configuration -------------------------------------------------------------

    def add_backend(self, name: str, **settings: Any) -> None:
        """Offer a backend under `name` (a CLI/proxy/model server this extension drives)."""
        self.backends[str(name)] = dict(settings)

    def add_integration(self, name: str, **settings: Any) -> None:
        """Register a third-party integration (`integrations.<name>` in the config, the same
        place bot/integrations.py reads)."""
        self.integrations[str(name)] = dict(settings)

    # -- dashboard -----------------------------------------------------------------

    def add_routes(self, register: Callable) -> None:
        """Add dashboard API routes. `register` is called exactly like one of ABP's own route
        modules' register(app, read_auth, write_auth): it adds its routes to the FastAPI app it
        is handed, with the same two auth callables as dependencies."""
        if not callable(register):
            raise TypeError(f"extension {self.name}: add_routes() needs a callable, not {type(register).__name__}")
        self.routes.append((self.name, register))

    def add_panel(self, panel_id: str, title: str, *, description: str = "", script: str = "panel.js",
                  styles: Optional[list[str]] = None, icon: str = "◆") -> None:
        """Add a UI panel to the dashboard and the desktop app's navigation.

        `script` and each entry of `styles` are relative to the extension's own folder and are
        served from /extensions/<name>/..., so the panel is never copied into the repo. The
        script is an ordinary ABP panel script: it draws into #<panel_id>-root and reloads on
        its own hashchange (bot/dashboard/static/extensions.js adds the section, the nav entry
        and the <script> tag at runtime)."""
        self.panels.append(Panel(id=str(panel_id), title=str(title), description=str(description),
                                 script=str(script), styles=[str(s) for s in (styles or [])],
                                 icon=str(icon)))

    # -- command line, MCP, modules, tests -----------------------------------------

    def add_cli_command(self, name: str, run: Callable, *, help: str = "", add_arguments: Optional[Callable] = None) -> None:
        """Add an `abp_cli` command. `run(args, client) -> int` is awaited exactly like one of
        ABP's own command handlers; `add_arguments(parser)` declares the command's flags."""
        if not callable(run):
            raise TypeError(f"extension {self.name}: add_cli_command() needs a callable")
        self.cli_commands.append({"name": str(name), "run": run, "help": help, "add_arguments": add_arguments})

    def add_mcp_tool(self, func: Callable, name: Optional[str] = None) -> None:
        """Add an MCP tool to bot/mcp_server.py's server. `func` is an async function whose
        docstring is the tool's description, like every tool ABP ships itself."""
        if not callable(func):
            raise TypeError(f"extension {self.name}: add_mcp_tool() needs a callable")
        self.mcp_tools.append((func, str(name or getattr(func, "__name__", ""))))

    def add_task(self, coro_fn: Callable) -> None:
        """Run `coro_fn()` (an async function) in the background while the extension is loaded. The runtime starts
        it when the extension goes live and cancels it when the extension is unloaded or replaced."""
        if not callable(coro_fn):
            raise TypeError(f"extension {self.name}: add_task() needs a callable")
        self.tasks.append(coro_fn)

    def add_module(self, entry: dict) -> None:
        """Add a module entry (the same shape as one item of modules.extra), so a private
        project is a module. The registry re-reads its manifests within seconds, so this takes
        effect without a restart."""
        if not isinstance(entry, dict):
            raise TypeError(f"extension {self.name}: add_module() needs a dict")
        self.modules.append(dict(entry))

    def add_test_dir(self, path: str | Path) -> None:
        """A directory of tests the local pipeline runs when ABP_PRIVATE_DIR is set. Relative
        paths are resolved against the extension's folder; a directory that isn't there is
        reported and skipped rather than failing the pipeline."""
        raw = Path(str(path))
        resolved = raw if raw.is_absolute() else self.directory / raw
        self.test_dirs.append(resolved)

    # -- backends, integration keys, the Neural Lab, the memory fabric, local models ------

    def add_backend_class(self, kind: str, factory: Callable, *, family: str = "custom", label: str = "",
                          ready: Optional[Callable] = None) -> None:
        """A backend a bot can be routed to, next to ABP's own (api, cli, native_agent, ...).

        `factory(settings, model)` returns a bot.backends Backend: `settings` is `backends.<kind>` from the
        effective config, `model` the bot's model field (None when it has none). `family` groups it in the
        model pickers (bot.models.backend_family). `ready() -> (ok, reason)` is the pre-flight check the router
        runs before each attempt (bot/setup_wizard.py). A kind ABP already has is ignored: ABP's own wins."""
        if not callable(factory):
            raise TypeError(f"extension {self.name}: add_backend_class() needs a callable factory")
        self.backend_classes[str(kind)] = {"factory": factory, "family": str(family), "label": str(label or kind),
                                           "ready": ready}

    def add_integration_scope(self, name: str, rules: list) -> None:
        """An integration-key scope (bot/integrations.py): a list of (HTTP method, path regex) pairs, usually the
        extension's own routes, so another program can be given exactly those and nothing else."""
        self.integration_scopes[str(name)] = [(str(m).upper(), str(p)) for m, p in rules]

    def add_integration_preset(self, name: str, *, label: str, scopes: list, note: str = "") -> None:
        """A named set of scopes offered when minting an integration key for a known program."""
        self.integration_presets[str(name)] = {"label": str(label), "scopes": [str(s) for s in scopes],
                                               **({"note": str(note)} if note else {})}

    def add_lab_importer(self, kind: str, *, label: str, run: Callable, list_items: Optional[Callable] = None) -> None:
        """A way to bring a design into the Neural Lab (next to BrainBuilder graphs).

        `run(body) -> spec` turns the import request's body into a design spec (bot/neurallab/spec.py);
        `list_items() -> [{"id", "label", "detail", "body"}]` is what the Neural Lab shows as importable, each
        `body` being what an Import click sends back to `run`."""
        if not callable(run):
            raise TypeError(f"extension {self.name}: add_lab_importer() needs a callable run")
        self.lab_importers[str(kind)] = {"label": str(label), "run": run, "list": list_items}

    def add_lab_project(self, key: str, folder: str) -> None:
        """A project the Neural Lab lists (its Projects card): found as <ABP_PROJECTS_DIR>/<folder>."""
        self.lab_projects[str(key)] = str(folder)

    def add_lab_family(self, family_id: str, *, label: str, status: Callable) -> None:
        """A model family the lab runs but does not train (its own checkpoints, its own runtime): `status() ->
        {"present", ...}` is shown in the lab's overview."""
        if not callable(status):
            raise TypeError(f"extension {self.name}: add_lab_family() needs a callable status")
        self.lab_families[str(family_id)] = {"label": str(label), "status": status}

    def add_memory_source(self, kind: str, *, required: list, read: Callable, item_kind: str = "note",
                          complete: bool = True, label: str = "", validate: Optional[Callable] = None) -> None:
        """A kind of memory-fabric source (bot/memoryfabric/sources.py), like "folder" or "web".

        `read(source)` yields (item_id, title, text, timestamp, tags) for what the source holds now, `source` being
        its stored config; `required` names the config fields a source of this kind must have, and
        `validate(config)` may raise ValueError for a bad one. `complete`: the listing is the whole source, so an
        item that disappears from it was deleted. `item_kind` is the knowledge base's kind for its items."""
        if not callable(read):
            raise TypeError(f"extension {self.name}: add_memory_source() needs a callable read")
        self.memory_sources[str(kind)] = {"required": [str(f) for f in required], "read": read,
                                          "item_kind": str(item_kind), "complete": bool(complete),
                                          "label": str(label or kind), "validate": validate}

    def add_model_location(self, app: str, path: str | Path, namespace: str) -> None:
        """A folder where another program on this machine keeps model files, for Local AI's Discover
        (bot/localai/discover.py). `namespace` prefixes the names ABP gives the models it finds there."""
        self.model_locations.append({"app": str(app), "path": Path(str(path)).expanduser(), "namespace": str(namespace)})

    def add_provider_source(self, source: Callable) -> None:
        """Model providers that exist only while something is up (like a module's own OpenAI-compatible server):
        `source() -> {name: {"base_url", "protocol", "api_key", "description"}}`, asked whenever ABP lists its live
        providers (bot/providers.py) and cached with them. Raising or returning nothing simply adds none."""
        if not callable(source):
            raise TypeError(f"extension {self.name}: add_provider_source() needs a callable")
        self.provider_sources.append(source)

    def on_hub_started(self, hook: Callable) -> None:
        """`hook(module_id)` runs after a module hub ABP started answers (bot/modules/harness.py), e.g. to hand it a
        session. A hook that raises is logged and skipped."""
        if not callable(hook):
            raise TypeError(f"extension {self.name}: on_hub_started() needs a callable")
        self.hub_started_hooks.append(hook)

    def add_lab_worker_plugin(self, path: str | Path) -> None:
        """A Python file the Neural Lab's training worker imports (by file location, in its own process) for
        formats ABP does not ship: `TOKENIZERS`, `CHECKPOINT_READERS` and `CHECKPOINT_WRITERS`, see
        bot/neurallab/nn_worker.py. Relative paths are resolved against the extension's folder."""
        raw = Path(str(path))
        self.lab_worker_plugins.append(raw if raw.is_absolute() else self.directory / raw)


# --------------------------------------------------------------------------------------
# Loading
# --------------------------------------------------------------------------------------

_lock = threading.Lock()
_state_holder: dict[str, Any] = {}


@dataclass
class Loaded:
    """The result of one load pass: what loaded, what failed and why."""

    extensions: list[Extension] = field(default_factory=list)
    errors: dict[str, str] = field(default_factory=dict)

    def by_name(self, name: str) -> Optional[Extension]:
        return next((e for e in self.extensions if e.name == name), None)

    def panels(self) -> list[tuple[Extension, Panel]]:
        return [(e, p) for e in self.extensions for p in e.api.panels]

    @property
    def routes(self) -> list[tuple[str, Callable]]:
        """(extension name, register) for every extension that registered dashboard routes."""
        return [r for e in self.extensions for r in e.api.routes]

    @property
    def mcp_tools(self) -> list[tuple[Callable, str]]:
        """(function, tool name) for every extension that registered an MCP tool."""
        return [t for e in self.extensions for t in e.api.mcp_tools]

    @property
    def module_entries(self) -> list[dict]:
        """Every module entry an extension registered, so a private project is a module. Read by
        bot/modules/registry.py, which re-reads its manifests within seconds."""
        return [m for e in self.extensions for m in e.api.modules]

    def _merged(self, attr: str) -> dict:
        """One extension-registered dict across every loaded extension (the first to register a key keeps it)."""
        out: dict[str, Any] = {}
        for e in self.extensions:
            for k, v in getattr(e.api, attr).items():
                out.setdefault(k, v)
        return out

    @property
    def backend_classes(self) -> dict[str, dict]:
        return self._merged("backend_classes")

    @property
    def integration_scopes(self) -> dict[str, list]:
        return self._merged("integration_scopes")

    @property
    def integration_presets(self) -> dict[str, dict]:
        return self._merged("integration_presets")

    @property
    def lab_importers(self) -> dict[str, dict]:
        return self._merged("lab_importers")

    @property
    def memory_sources(self) -> dict[str, dict]:
        return self._merged("memory_sources")

    @property
    def lab_projects(self) -> dict[str, str]:
        return self._merged("lab_projects")

    @property
    def lab_families(self) -> dict[str, dict]:
        return self._merged("lab_families")

    @property
    def model_locations(self) -> list[dict]:
        return [m for e in self.extensions for m in e.api.model_locations]

    @property
    def lab_worker_plugins(self) -> list[Path]:
        return [p for e in self.extensions for p in e.api.lab_worker_plugins if p.is_file()]

    @property
    def provider_sources(self) -> list[Callable]:
        return [s for e in self.extensions for s in e.api.provider_sources]

    @property
    def hub_started_hooks(self) -> list[Callable]:
        return [h for e in self.extensions for h in e.api.hub_started_hooks]

    @property
    def config_overlay(self) -> dict:
        """The one config fragment every loaded extension asked for, folded into a document."""
        out: dict[str, Any] = {}
        for ext in self.extensions:
            if ext.api.backends:
                out.setdefault("backends", {}).update(copy.deepcopy(ext.api.backends))
            if ext.api.integrations:
                out.setdefault("integrations", {}).update(copy.deepcopy(ext.api.integrations))
        return out

    def describe(self) -> dict:
        return {"extensions": [e.describe() for e in self.extensions], "errors": dict(self.errors)}


class _State:
    """The cached load, plus a monotonic version so a caller can tell one pass from the next."""

    def __init__(self) -> None:
        self.loaded = Loaded()
        self.version = 0


def _state() -> _State:
    with _lock:
        if "state" not in _state_holder:
            _state_holder["state"] = _State()
        return _state_holder["state"]


def _publish(ext: "Extension") -> "Optional[Extension]":
    """Make `ext` the loaded extension of its name (called when its unit starts). Returns the one it replaced."""
    state = _state()
    with _lock:
        previous = state.loaded.by_name(ext.name)
        rest = [e for e in state.loaded.extensions if e.name != ext.name]
        errors = {k: v for k, v in state.loaded.errors.items() if k != ext.directory.name}
        state.loaded = Loaded(sorted(rest + [ext], key=lambda e: e.name), errors)
        state.version = max(state.version, 1)
        return previous


def _retire(ext: "Extension", restore: "Optional[Extension]" = None) -> None:
    """Take `ext` out of the loaded set (its unit stopped) if it is still the current one, putting back the version
    it had replaced when a swap was aborted."""
    state = _state()
    with _lock:
        if state.loaded.by_name(ext.name) is not ext:
            return
        rest = [e for e in state.loaded.extensions if e is not ext]
        if restore is not None:
            rest.append(restore)
        state.loaded = Loaded(sorted(rest, key=lambda e: e.name), dict(state.loaded.errors))


def record_error(folder_name: str, message: str) -> None:
    """Remember why an extension did not load (shown by GET /api/extensions)."""
    state = _state()
    with _lock:
        errors = dict(state.loaded.errors)
        errors[folder_name] = message
        state.loaded = Loaded(list(state.loaded.extensions), errors)
        state.version = max(state.version, 1)


def clear_error(folder_name: str) -> None:
    state = _state()
    with _lock:
        if folder_name in state.loaded.errors:
            state.loaded = Loaded(list(state.loaded.extensions),
                                  {k: v for k, v in state.loaded.errors.items() if k != folder_name})


def _version_tuple(text: str) -> tuple:
    return tuple(int(p) for p in re.findall(r"\d+", str(text))[:4])


def _parse_manifest(path: Path) -> dict:
    """An abp_extension.toml as {name, version, description, entry, minimum_abp}.

    [extension] holds the first four; `minimum_abp` is the oldest ABP the extension works with.
    Anything missing is an error naming the field, because a silently half-loaded extension is
    worse than one that reports itself broken."""
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    section = data.get("extension") if isinstance(data, dict) else None
    if not isinstance(section, dict):
        raise ValueError(f"no [extension] section in {path.name}")
    out = {
        "name": str(section.get("name") or "").strip(),
        "version": str(section.get("version") or "").strip(),
        "description": str(section.get("description") or "").strip(),
        "entry": str(section.get("entry") or "").strip(),
        "minimum_abp": str(section.get("minimum_abp") or "").strip(),
    }
    missing = [f for f in ("name", "version", "description", "entry") if not out[f]]
    if missing:
        raise ValueError(f"{path.name}: [extension] is missing {', '.join(missing)}")
    return out


def _import_extension_module(directory: Path, entry: str):
    """Import the extension's Python package by file location.

    The extension folder is not on sys.path (nothing in it is importable by accident, and a
    private folder must not shadow ABP's own modules), so the package file is located by hand.

    The module name carries a digest of the folder's own path: two private folders may both hold
    an extension called "broken", and giving them the same module name would silently run the
    first one's code for the second. The module is executed on every load rather than reused from
    sys.modules, so a reload really does pick up an edited file."""
    rel = entry.replace(".", "/")
    candidates = [directory / f"{rel}.py", directory / entry / "__init__.py", directory / rel / "__init__.py"]
    for candidate in candidates:
        if candidate.is_file():
            break
    else:
        raise ModuleNotFoundError(f"entry module {entry!r} not found under {directory.name}")
    digest = hashlib.md5(str(directory.resolve()).encode("utf-8", "replace")).hexdigest()[:10]
    name = f"_abp_extension_{digest}_{entry.replace('.', '_')}"
    sys.modules.pop(name, None)
    # A package entry is loaded AS a package, so its own modules can import each other relatively (from . import x).
    search = [str(candidate.parent)] if candidate.name == "__init__.py" else None
    spec = importlib.util.spec_from_file_location(name, candidate, submodule_search_locations=search)
    if spec is None or spec.loader is None:
        raise ImportError(f"couldn't load {candidate}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def load_all(environ: Any = None, force: bool = False) -> Loaded:
    """Discover, import and register every extension under <ABP_PRIVATE_DIR>/extensions/.

    Cached: the first call loads, later calls return the same Loaded unless force=True. Each
    extension is loaded in isolation — a missing manifest, an import that raises, a register()
    that raises, or a minimum_abp newer than this ABP are all recorded in Loaded.errors and
    skipped, because one broken integration must never stop ABP from starting."""
    state = _state()
    with _lock:
        if state.version and not force and environ is None:
            return state.loaded
        root = extensions_root(environ)
        loaded = Loaded()
    if root is not None:
        for folder in sorted(p for p in root.iterdir() if (p / MANIFEST_FILE).is_file()):
            try:
                loaded.extensions.append(_load_one(folder))
            except Exception as exc:  # noqa: BLE001 — a failing extension is skipped, by design
                name = folder.name
                logger.exception("extension %s failed to load; ABP continues without it", name)
                loaded.errors[name] = f"{type(exc).__name__}: {exc}"
    # Published in one step, and outside the import: an extension whose register() reads the
    # config must see the config as it was, not a half-registered set of extensions.
    with _lock:
        state.loaded = loaded
        state.version += 1
        return loaded


def _load_one(folder: Path) -> Extension:
    """Load and register one extension. Raises on anything wrong with it; load_all turns that
    into a recorded error."""
    manifest = _parse_manifest(folder / MANIFEST_FILE)
    minimum = manifest["minimum_abp"]
    if minimum:
        from bot import __version__

        if _version_tuple(minimum) > _version_tuple(__version__):
            raise RuntimeError(f"needs ABP {minimum} or newer (this is {__version__})")
    mod = _import_extension_module(folder, manifest["entry"])
    register = getattr(mod, "register", None)
    if not callable(register):
        raise AttributeError(f"entry module {manifest['entry']!r} has no register(api)")
    api = ExtensionAPI(manifest["name"], manifest["version"], manifest["description"], folder)
    register(api)
    logger.info("loaded extension %s %s (%s)", api.name, api.version, folder)
    return Extension(api.name, api.version, api.description, folder, api)


def loaded() -> Loaded:
    """The loaded extensions (loads them the first time it's asked)."""
    return load_all()


def errors() -> dict[str, str]:
    """Extension -> why it failed to load. Shown in the modules/extensions view."""
    return dict(load_all().errors)


def panels() -> list[dict]:
    """Every registered panel, with the extension that owns it and the URL its script is
    served from. This is what bot/dashboard/extensions_api.py serves to both UIs."""
    out = []
    for ext, panel in load_all().panels():
        out.append({
            "id": panel.id, "title": panel.title, "description": panel.description,
            "icon": panel.icon, "extension": ext.name,
            "script": f"/extensions/{ext.name}/{panel.script}",
            "styles": [f"/extensions/{ext.name}/{s}" for s in panel.styles],
        })
    return out


def test_dirs() -> list[Path]:
    """Test directories every loaded extension declared, existing ones only."""
    return [p for ext in load_all().extensions for p in ext.api.test_dirs if p.is_dir()]