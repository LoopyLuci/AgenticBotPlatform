"""bot/wasm_plugins/manager.py - installing, enabling, disabling and calling plugins.

A WASM plugin is a component file. This is what ABP does with one:

    install(path_or_url, approve=...)  -> copy the component in, read its manifest, and
                                          refuse to activate until a person has been shown
                                          what it asks for
    enable(name) / disable(name)       -> its tools appear in the agent's tool list, or do not
    uninstall(name)                    -> the component, its store and its registration go
    invoke(name, tool, args)           -> one call, under a deadline and a memory cap

**The install is where the trust decision happens.** A plugin's manifest says which
directories it wants and which hosts it wants to reach; a person approves a subset of that,
and only the approved subset is ever handed to the runtime. A plugin cannot widen its own
grants afterwards: the grants live here, in the registry, not in the guest.

State is a JSON file under the project's data directory rather than a database table. A wasm
plugin's row is a manifest, a grants list and a flag, and one file per ABP keeps this
subsystem out of bot/db.py's schema and its migrations.

Every call is audit-logged (`db.log_audit`): the plugin, the tool, how long it took, and how
it ended. That is the record of what ran code on this machine.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import threading
import time
from pathlib import Path
from typing import Any, Optional

from bot.wasm_plugins import runtime as rt

logger = logging.getLogger("bot.wasm_plugins")

_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")

#: Where components live, one directory per plugin: the .wasm beside its store, so uninstall
#: is one directory removal and a plugin can never read another's store.
def _root() -> Path:
    from bot import envfile

    return Path(envfile.PROJECT_ROOT) / "data" / "wasm_plugins"


def _state_path() -> Path:
    return _root() / "plugins.json"


class WasmPluginError(RuntimeError):
    """Anything that stops a plugin being installed, enabled or called. The message is what
    a person reads, so it says what to do about it."""


def _load_state() -> dict:
    path = _state_path()
    if not path.exists():
        return {"plugins": {}}
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        # A corrupt registry must not take the whole app down; the file is rewritten from
        # whatever is on disk on the next install, and this is logged loudly.
        logger.error("wasm plugin registry at %s is unreadable (%s); starting empty", path, exc)
        return {"plugins": {}}
    state.setdefault("plugins", {})
    return state


def _save_state(state: dict) -> None:
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


_state_lock = threading.RLock()

#: name -> {"handle", "services", "manifest"} for the plugins currently loaded. A loaded
#: plugin holds a compiled component (a few MB) and an engine reference, so this is a cache
#: with a lifetime, not a registry: `list_plugins()` reads the JSON, not this.
_loaded: dict = {}


# ------------------------------------------------------------------ inspection

def capabilities(manifest: dict) -> dict:
    """What a plugin says it wants, in the shape the approval prompt and the dashboard show.

    Split out from `install()` because the point is to be able to *show* this before anything
    has been granted - a caller can install a component, look at this, and decide.
    """
    return {
        "tools": [
            {"name": t.get("name", ""), "description": t.get("description", "")}
            for t in manifest.get("tools", [])
        ],
        "preopens": list(manifest.get("preopens", [])),
        "network": list(manifest.get("network", [])),
    }


def requested_to_grants(manifest: dict, *, approve_preopens: Optional[list] = None,
                        approve_network: Optional[list] = None) -> dict:
    """Turn a manifest plus a person's answers into the grants handed to the runtime.

    The intersection is the point: a guest path or host that was not approved is not in the
    result, whatever the manifest asked for and whatever the caller passed in. Approving
    something the plugin did not ask for is refused rather than silently ignored, because a
    grant the plugin never asked for is a grant nobody reviewed.
    """
    wants_preopens = list(manifest.get("preopens", []))
    wants_network = list(manifest.get("network", []))
    approved_preopens = set(approve_preopens if approve_preopens is not None else wants_preopens)
    approved_network = set(approve_network if approve_network is not None else wants_network)

    unknown = approved_preopens - set(wants_preopens)
    if unknown:
        raise WasmPluginError(
            f"approved directories the plugin did not ask for: {', '.join(sorted(unknown))}. "
            f"It asked for: {', '.join(wants_preopens) or '(none)'}"
        )
    unknown = approved_network - set(wants_network)
    if unknown:
        raise WasmPluginError(
            f"approved hosts the plugin did not ask for: {', '.join(sorted(unknown))}. "
            f"It asked for: {', '.join(wants_network) or '(none)'}"
        )
    return {
        "preopens": [p for p in wants_preopens if p in approved_preopens],
        "network": [h for h in wants_network if h in approved_network],
    }


# ------------------------------------------------------------------ install

def install(
    source: str,
    *,
    approve_preopens: Optional[list] = None,
    approve_network: Optional[list] = None,
    timeout_ms: int = rt.DEFAULT_TIMEOUT_MS,
    memory_mb: int = rt.DEFAULT_MEMORY_MB,
    enabled: bool = False,
    audit: bool = True,
) -> dict:
    """Install a plugin from a local file or an https URL.

    The component is copied into ABP's own directory, compiled, and asked what it is. It is
    **not enabled** by default: `describe()` has now been read, so the caller has the
    capability list to show a person, and enabling is a separate, deliberate act.

    Approving the capabilities is two lists. `None` for either means "approve everything the
    plugin asked for", which is only ever right for a plugin whose manifest asks for nothing
    - which is what `examples/wasm/rust-hello` does, and why it installs without a
    conversation.
    """
    if not rt.available():
        raise WasmPluginError(
            f"installing a plugin needs the native wasm runtime, and it is not available: {rt.unavailable_reason()}"
        )
    path = _fetch(source)
    # The manifest name is the plugin's own: ABP's registry is keyed on it, and a plugin that
    # says something else than its file name is telling the truth about itself.
    manifest = _describe(path, timeout_ms=timeout_ms, memory_mb=memory_mb)
    name = str(manifest.get("name") or "").strip()
    if not _NAME_RE.match(name):
        raise WasmPluginError(
            f"the plugin calls itself {name!r}; a name must be lowercase letters, digits, "
            "dashes or underscores, starting with a letter"
        )
    grants = requested_to_grants(
        manifest, approve_preopens=approve_preopens, approve_network=approve_network
    )

    with _state_lock:
        home = _root() / name
        if home.exists():
            _remove_component(name)
        home.mkdir(parents=True, exist_ok=True)
        component = home / "plugin.wasm"
        shutil.copy2(path, component)

        state = _load_state()
        row = {
            "name": name,
            "version": manifest.get("version", "0"),
            "description": manifest.get("description", ""),
            "source": str(source),
            "component": str(component),
            # The manifest's own tool list, kept so the plugin page and the approval prompt
            # can be answered without loading (and therefore without trusting) the plugin
            # again. `describe` shows it; `enable` re-reads it from the component itself.
            "tools": [
                {"name": t.get("name", ""), "description": t.get("description", ""), "schema": t.get("schema", "")}
                for t in manifest.get("tools", [])
            ],
            # What the plugin asked for, kept next to what it was given: the difference
            # between the two is what a person approved, and `narrow_grants` needs the first
            # to make sure the second can only ever shrink.
            "requested": {
                "preopens": list(manifest.get("preopens", [])),
                "network": list(manifest.get("network", [])),
            },
            "grants": grants,
            "timeout_ms": int(timeout_ms),
            "memory_mb": int(memory_mb),
            "enabled": False,
            "installed_at": time.time(),
        }
        state["plugins"][name] = row
        _save_state(state)
    _audit("wasm_plugin_install", f"{name}: {manifest.get('description', '')}")

    if enabled:
        enable(name)
    return describe(name)


def _fetch(source: str) -> Path:
    """The component file to install, from a path or an https URL.

    A URL is downloaded to a temporary file first and only copied into ABP's directory once
    it has turned out to be a component - so a failed download never leaves a half-file where
    a plugin is supposed to be. Only https: an install that reaches the network at all is
    already a decision, and it should not be one an http:// typo can make silently.
    """
    candidate = Path(source).expanduser()
    if candidate.exists():
        if not candidate.is_file():
            raise WasmPluginError(f"{source!r} is not a file")
        return candidate
    if not source.lower().startswith("https://"):
        raise WasmPluginError(
            f"{source!r} is neither a file that exists nor an https:// URL"
        )
    import tempfile
    import urllib.request

    tmp = Path(tempfile.gettempdir()) / f"abp-wasm-plugin-{int(time.time() * 1000)}.wasm"
    try:
        with urllib.request.urlopen(source, timeout=30) as response:  # noqa: S310 - the scheme was just checked
            tmp.write_bytes(response.read(64 * 1024 * 1024))
    except Exception as exc:  # noqa: BLE001 - the message is what the user needs
        tmp.unlink(missing_ok=True)
        raise WasmPluginError(f"could not download {source}: {exc}") from exc
    return tmp


def _describe(path: Path, *, timeout_ms: int, memory_mb: int) -> dict:
    """Compile a component in a throwaway runtime and read its manifest.

    Deliberately does not register anything: this is what turns "a file someone handed me"
    into "a plugin that says it does these things", which is what has to be true before anyone
    is asked to approve it.
    """
    module = rt.require()
    if module is None:
        raise WasmPluginError(f"the native wasm runtime is not available: {rt.unavailable_reason()}")
    services = rt.HostServices(
        "uninstalled",
        _root() / ".uninstalled",
        network=[],
        log_sink=lambda level, msg: logger.debug("wasm plugin (uninstalled): %s", msg),
    )
    try:
        engine = module.Runtime(cache_dir=str(_root() / ".cache"))
        handle = engine.load(
            path,
            grants=rt.grants_for(),
            services=services,
            memory_limit_bytes=int(memory_mb) * 1024 * 1024,
        )
    except Exception as exc:  # noqa: BLE001 - every failure here is "this is not a plugin"
        raise WasmPluginError(f"that file is not a plugin ABP can load: {exc}") from exc
    manifest = dict(handle.manifest)
    # A manifest that names no tools cannot be useful, and one whose tool names collide would
    # make the agent's tool list ambiguous. Both are refused here rather than at call time.
    tools = [t for t in manifest.get("tools", []) if t.get("name")]
    if not tools:
        raise WasmPluginError("the plugin describes no tools, so there is nothing to install it for")
    seen: set = set()
    for tool in tools:
        name = tool["name"]
        if name in seen:
            raise WasmPluginError(f"the plugin describes a tool named {name!r} twice")
        seen.add(name)
    manifest["tools"] = tools
    return manifest


def narrow_grants(name: str, *, preopens: Optional[list] = None, network: Optional[list] = None) -> dict:
    """Reduce an installed plugin's grants, never raise them.

    The dashboard's "approve less" action, and the only path that changes a grant after
    install. The plugin's original requests are stored next to its grants for exactly this:
    the approved lists are intersected with *those*, so naming something the plugin never
    asked for is refused rather than granted - a grant nobody reviewed is not a grant.
    """
    row = _row(name)
    original = row.get("requested") or {"preopens": [], "network": []}
    granted = requested_to_grants(
        original,
        approve_preopens=preopens if preopens is not None else row["grants"].get("preopens", []),
        approve_network=network if network is not None else row["grants"].get("network", []),
    )
    with _state_lock:
        state = _load_state()
        state["plugins"][name]["grants"] = granted
        _save_state(state)
    return granted


# ------------------------------------------------------------------ enable / disable

def enable(name: str) -> dict:
    """Load a plugin and register its tools with the agent. Replacing a plugin that is already loaded is a swap:
    the new component is compiled beside the live one and only a successful load retires the old one, so a
    component that will not load leaves the live version serving."""
    row = _row(name)
    with _state_lock:
        try:
            _load(name, row)
        except WasmPluginError:
            raise
        except Exception as exc:  # noqa: BLE001 - a broken plugin must not take the boot down
            raise WasmPluginError(f"{name} would not load: {exc}") from exc
        state = _load_state()
        state["plugins"][name]["enabled"] = True
        _save_state(state)
    _audit("wasm_plugin_enable", name)
    _runtime_sync()
    return describe(name)


def disable(name: str) -> dict:
    """Unregister a plugin's tools and unload it. Its component and store stay: disabling is
    reversible, and a plugin's store is its own state rather than ABP's to delete."""
    _row(name)
    with _state_lock:
        _unload(name)
        state = _load_state()
        state["plugins"][name]["enabled"] = False
        _save_state(state)
    _audit("wasm_plugin_disable", name)
    _runtime_sync()
    return describe(name)


def uninstall(name: str) -> bool:
    """Remove a plugin for good: its registration, its component and its store."""
    _row(name)
    with _state_lock:
        _unload(name)
        state = _load_state()
        existed = state["plugins"].pop(name, None) is not None
        _save_state(state)
        _remove_component(name)
    if existed:
        _audit("wasm_plugin_uninstall", name)
    _runtime_sync()
    return existed


def _runtime_sync() -> None:
    """Tell the unit runtime what changed (a transaction); the direct stage/apply/revert stays the mechanism."""
    from bot.runtime import pluggables

    pluggables.sync_from_sync_code(pluggables.WASM)


def _remove_component(name: str) -> None:
    home = _root() / name
    if home.exists():
        shutil.rmtree(home, ignore_errors=True)


def load_enabled() -> None:
    """Load every plugin marked enabled. Called once at start-up, next to
    bot/plugins.py's own loader. A plugin that fails to load is logged and skipped, left
    enabled in the registry so fixing it and restarting picks it back up - the same rule
    bot/plugins.py follows, for the same reason."""
    for row in list(_load_state()["plugins"].values()):
        if not row.get("enabled"):
            continue
        try:
            enable(row["name"])
        except WasmPluginError as exc:
            logger.error("wasm plugin %r failed to load: %s", row["name"], exc)


# ------------------------------------------------------------------ loading

class StagedWasm:
    """One compiled version of a plugin, built beside the live one. `apply()` publishes it (retiring the version it
    replaces), `revert()` unpublishes it and closes the handle; both are synchronous and idempotent."""

    def __init__(self, name: str, row: dict, handle: Any, services: Any, manifest: dict) -> None:
        self.name = name
        self.row = row
        self.handle = handle
        self.services = services
        self.manifest = manifest
        self.applied = False
        self.closed = False
        self.managed = False                    # True: the unit runtime registers the tools through its registry
        self.version = component_version(row)  # of the component as it was when this was compiled
        self.names, self.entries = _tool_entries(name, manifest)

    def apply(self) -> None:
        if self.applied:
            return
        from bot.wasm_plugins import tools

        old = _loaded.get(self.name)
        if old is not None and old.get("staged") is not self:
            old["staged"].revert()
        _loaded[self.name] = {"handle": self.handle, "services": self.services, "manifest": self.manifest,
                              "staged": self}
        if self.managed:
            tools._registered[self.name] = dict(self.names)
        else:
            tools.register_plugin(self.name, self.manifest)
        self.applied = True

    def revert(self) -> None:
        if self.applied:
            from bot.wasm_plugins import tools

            self.applied = False
            if (_loaded.get(self.name) or {}).get("staged") is self:
                del _loaded[self.name]
                if self.managed:
                    tools._registered.pop(self.name, None)
                else:
                    tools.unregister_plugin(self.name)
        self.close()

    def close(self) -> None:
        if not self.closed:
            self.closed = True
            closer = getattr(self.handle, "close", None)
            if callable(closer):
                closer()


def _tool_entries(name: str, manifest: dict) -> tuple[dict, list]:
    from bot.wasm_plugins import tools

    return tools.plugin_entries(name, manifest)


def stage(name: str, row: dict) -> StagedWasm:
    """Compile a plugin and give it its granted capabilities, touching no registry. Raises on any failure."""
    rt.Runtime.shared()                           # raises WasmUnavailable (with the reason) when the native module is absent
    services = rt.HostServices(
        name,
        _root() / name / "store",
        network=list(row["grants"].get("network", [])),
        log_sink=lambda level, msg, n=name: logger.log(_level(level), "wasm plugin %s: %s", n, msg),
    )
    preopens = {
        guest: str(_root() / name / "data" / Path(guest.lstrip("/")).as_posix())
        for guest in row["grants"].get("preopens", [])
    }
    for guest in preopens:
        Path(preopens[guest]).mkdir(parents=True, exist_ok=True)
    grants = rt.grants_for(
        preopens=preopens,
        network=row["grants"].get("network", []),
        max_output_bytes=rt.DEFAULT_MAX_OUTPUT_BYTES,
    )
    handle = rt.Runtime.shared().load(
        Path(row["component"]),
        services=services,
        grants=grants,
        memory_limit_bytes=int(row.get("memory_mb", rt.DEFAULT_MEMORY_MB)) * 1024 * 1024,
    )
    return StagedWasm(name, row, handle, services, dict(handle.manifest))


def _load(name: str, row: dict) -> None:
    """Stage a plugin, then publish it in one step (the version it replaces is retired only after this loaded)."""
    staged = stage(name, row)
    staged.apply()
    logger.info("wasm plugin %s loaded: %d tool(s)", name, len(staged.manifest.get("tools", [])))


def component_version(row: dict) -> str:
    """The content hash of a plugin's component: its version for the unit runtime."""
    import hashlib

    try:
        return "v-" + hashlib.sha256(Path(row["component"]).read_bytes()).hexdigest()[:12]
    except OSError:
        return "v-missing"


def adopt(name: str, row: dict) -> Optional[StagedWasm]:
    """The version of `name` that is already loaded, if its component is the one on disk."""
    current = (_loaded.get(name) or {}).get("staged")
    if current is not None and current.applied and current.version == component_version(row):
        return current
    return None


def _unload(name: str) -> None:
    entry = _loaded.get(name)
    if entry is None:
        return
    staged = entry.get("staged")
    if staged is not None:
        staged.revert()
    else:
        _loaded.pop(name, None)
        from bot.wasm_plugins import tools

        tools.unregister_plugin(name)


def _register_tools(name: str, manifest: dict) -> None:
    """Each tool the plugin offers becomes an agent tool, with its own JSON schema."""
    from bot.wasm_plugins import tools

    tools.register_plugin(name, manifest)


def _level(level: str) -> int:
    return {"trace": 5, "debug": 10, "info": 20, "warn": 30, "error": 40}.get(
        (level or "").lower(), 20
    )


# ------------------------------------------------------------------ calling

def invoke(name: str, tool: str, tool_input: Any = None, *, timeout_ms: Optional[int] = None,
           audit: bool = True) -> str:
    """Call one tool on a loaded plugin. Returns the tool's JSON result as text.

    Every call is audit-logged with the plugin, the tool, how long it took and how it ended,
    whether or not it worked: "this plugin ran and was stopped after 2 seconds" is exactly the
    kind of thing a person needs to be able to find later.
    """
    entry = _loaded.get(name)
    if entry is None:
        raise WasmPluginError(
            f"{name!r} is not loaded; enable it first"
        )
    args = rt.json_args(tool_input)
    budget = int(timeout_ms if timeout_ms is not None else _row(name).get("timeout_ms", rt.DEFAULT_TIMEOUT_MS))
    budget = max(1, min(budget, rt.MAX_TIMEOUT_MS))
    started = time.perf_counter()
    outcome = "ok"
    detail = ""
    try:
        result = entry["handle"].invoke(tool, args, timeout_ms=budget)
    except rt.PluginCallError:
        outcome = "limited"
        raise
    except Exception as exc:  # noqa: BLE001 - every failure is recorded before it propagates
        outcome = "error"
        detail = str(exc)
        raise
    finally:
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        if audit:
            _audit(
                "wasm_plugin_call",
                f"{name}.{tool} {outcome} in {elapsed_ms:.1f}ms" + (f": {detail}" if detail else ""),
            )
    if "error" in result:
        # The plugin ran and reported a failure. That is an answer (a bad argument, a refused
        # URL), so it comes back as text rather than as an exception - and it is still audited.
        return json.dumps({"error": result["error"]}, ensure_ascii=False)
    return str(result.get("ok", "{}"))


def is_loaded(name: str) -> bool:
    return name in _loaded


# ------------------------------------------------------------------ listing

def _row(name: str) -> dict:
    row = _load_state()["plugins"].get(name)
    if row is None:
        raise WasmPluginError(
            f"no wasm plugin named {name!r}; installed ones are {', '.join(sorted(_load_state()['plugins'])) or '(none)'}"
        )
    return row


def describe(name: str) -> dict:
    """One plugin: what it says it is, what it asked for, what it was granted, and whether it
    is loaded. The two are listed next to each other on purpose - the difference between the
    requests and the grants is what a person approved."""
    row = _row(name)
    manifest = dict(row)
    manifest["requested"] = dict(row.get("requested") or {"preopens": [], "network": []})
    manifest["granted"] = dict(row["grants"])
    manifest["loaded"] = is_loaded(name)
    if is_loaded(name):
        manifest["tools"] = [
            {"name": t.get("name", ""), "description": t.get("description", ""), "schema": t.get("schema", "")}
            for t in _loaded[name]["manifest"].get("tools", [])
        ]
    else:
        manifest["tools"] = []
    return manifest


def list_plugins() -> list[dict]:
    return [describe(name) for name in sorted(_load_state()["plugins"])]


def has_tool(tool_name: str) -> bool:
    from bot.wasm_plugins import tools

    return tools.has_tool(tool_name)


def tool_schemas() -> list[dict]:
    """Every wasm plugin tool, as the agent's tool schemas. This is what puts a plugin's
    tools in front of the model."""
    from bot.wasm_plugins import tools

    return tools.tool_schemas()


def plugin_for_tool(tool_name: str) -> Optional[str]:
    from bot.wasm_plugins import tools

    return tools.plugin_for_tool(tool_name)


# ------------------------------------------------------------------ audit

def _audit(action: str, detail: str) -> None:
    """One audit row per install, enable, call and uninstall. A wasm plugin is code that ran
    on this machine; the record of what ran, when and how it ended is not optional."""
    try:
        from bot import db

        db.log_audit(actor="wasm_plugins", action=action, detail=detail)
    except Exception as exc:  # noqa: BLE001 - auditing must not be the thing that fails
        logger.warning("could not write the wasm plugin audit row for %s: %s", action, exc)


def reset_for_tests() -> None:
    """Forget every loaded plugin. Tests use it so one test's plugin is not the next test's."""
    with _state_lock:
        for name in list(_loaded):
            _unload(name)
