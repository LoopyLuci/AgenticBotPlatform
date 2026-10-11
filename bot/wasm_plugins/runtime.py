"""bot/wasm_plugins/runtime.py - the native half, and what happens without it.

ABP's WebAssembly plugin runtime is `abp_wasm`, a Rust extension module built from
native/abp-wasm. It is optional: ABP works without it, the feature disables itself with a
clear message, and `ABP_NATIVE=0` turns it off deliberately so both paths can be tested on
one machine.

Three things live here:

* `available()` / `require()` - whether the module is there, and the one line that says which
  implementation is active. Every caller goes through this rather than importing `abp_wasm`
  directly, so the "not built" case is one message instead of an ImportError per call site.
* `HostServices` - the object a plugin's `import host` calls land in. Each plugin gets its
  own, so one plugin's key-value store is invisible to every other one, and the fetch goes
  through ABP's own web tooling rather than straight to the network.
* `Runtime` - the engine. One per process: plugins share it, so a component compiled for one
  is not compiled again for the next.

The limits (deadline, memory, output) are enforced inside the engine, not here. What this
side decides is what a plugin is *allowed*: the grants are what a person approved at install
time, and this is the code that refuses to widen them.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable, Optional, Union
from urllib.parse import urlsplit

logger = logging.getLogger("bot.wasm_plugins.runtime")

#: Where ABP keeps compiled components between runs. Under the platform's data dir rather
#: than next to the checkout: a compiled artifact is machine-specific (it is compiled for this
#: CPU, by this wasmtime) and has no business travelling with the source.
def _default_cache_dir() -> Path:
    root = os.environ.get("ABP_DATA_DIR")
    base = Path(root) if root else Path.home() / ".agentic-bot-platform"
    return base / "wasm-cache"


def native_disabled() -> bool:
    """`ABP_NATIVE=0` switches off every native module, for testing both paths on one build."""
    return os.environ.get("ABP_NATIVE", "1").strip().lower() in {"0", "false", "no", "off"}


def available() -> bool:
    if native_disabled():
        return False
    try:
        import abp_wasm  # noqa: F401
    except ImportError:
        return False
    return True


#: The one place the answer gets logged, so "which implementation is active" is a single
#: line in the log rather than something each caller has to say.
_logged: set[str] = set()


def _say_once(key: str, message: str, level: int = logging.INFO) -> None:
    if key in _logged:
        return
    _logged.add(key)
    logger.log(level, message)


def require() -> Any:
    """The `abp_wasm` module, or None with the reason already logged."""
    if native_disabled():
        _say_once("disabled", "wasm plugins: off (ABP_NATIVE=0)", logging.INFO)
        return None
    try:
        import abp_wasm
    except ImportError as exc:
        _say_once(
            "missing",
            f"wasm plugins: off - the native module is not built ({exc}). "
            "Build it with: python native/abp-wasm/build.py --venv <python.exe>",
            logging.WARNING,
        )
        return None
    _say_once("active", f"wasm plugins: native runtime {abp_wasm.__version__} (wasmtime {abp_wasm.wasmtime_version()})")
    return abp_wasm


#: Why the feature is off, in a sentence, for the exception and the dashboard. Same two cases
#: as `require()`, in words rather than in a log line.
def unavailable_reason() -> str:
    if native_disabled():
        return (
            "ABP_NATIVE=0 is set, which turns off every native module. "
            "Unset it to use the WebAssembly plugin runtime."
        )
    return (
        "the native module abp_wasm is not built. "
        "Build it with: python native/abp-wasm/build.py --venv <python.exe>"
    )


#: A call that never returns within this is a bug in a plugin, and the epoch deadline is the
#: thing that stops it. The cell's own timeout is the outer bound; this is the inner one.
DEFAULT_TIMEOUT_MS = 2_000

#: Hard ceilings on what one plugin may do, whatever it asks for. A per-plugin setting can
#: lower these; nothing raises them.
MAX_TIMEOUT_MS = 30_000
DEFAULT_MEMORY_MB = 16
MAX_MEMORY_MB = 512
DEFAULT_MAX_OUTPUT_BYTES = 256 * 1024


class WasmUnavailable(RuntimeError):
    """The feature is off because the native module is not there, or ABP_NATIVE=0 says so. The
    message carries the reason rather than "see the log": a feature that disables itself
    without saying why is the same as a feature that is broken."""


class PolicyDenied(RuntimeError):
    """A plugin asked for something it was not granted. Refused before anything happens,
    which is the only place a refusal is worth anything."""


class PluginCallError(RuntimeError):
    """A call did not finish: a deadline, a memory cap, a trap. Carries the kind, so the
    audit log and the agent tool can say which limit it was."""


class HostServices:
    """What one plugin's `import host` calls reach.

    One of these per plugin, created with that plugin's own store directory. That is the
    whole isolation story: a plugin's keys live in its directory, so nothing it writes is
    visible from another plugin, and ABP's own state is not in here at all.

    `fetch` is the one interesting method. It is given the plugin's *approved* network list,
    and it checks the URL against it before making a request - the same check the native
    runtime does, on purpose: a policy that is only enforced in one of two places is a policy
    with a hole in it, and this one is cheap to enforce twice.
    """

    def __init__(
        self,
        plugin_name: str,
        store_dir: Path,
        *,
        network: list[str],
        log_sink: Optional[Callable[[str, str], None]] = None,
        timeout_s: float = 15.0,
    ) -> None:
        self.plugin_name = plugin_name
        self.store_dir = Path(store_dir)
        self.network = list(network)
        self._log_sink = log_sink
        self._timeout_s = timeout_s
        self._lock = threading.Lock()
        self.store_dir.mkdir(parents=True, exist_ok=True)

    # ---- the host interface the runtime calls -------------------------------------------------

    def log(self, level: str, msg: str) -> None:
        if self._log_sink is not None:
            self._log_sink(level, msg)
        else:
            logger.log(_level_for(level), "wasm plugin %s: %s", self.plugin_name, msg)

    def store_get(self, key: str) -> Optional[str]:
        return self._read(key)

    def store_set(self, key: str, value: str) -> None:
        self._write(key, value)

    def store_delete(self, key: str) -> None:
        path = self._path(key)
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            logger.warning("wasm plugin %s: cannot delete %s: %s", self.plugin_name, key, exc)

    def store_keys(self) -> list[str]:
        """Every key in this plugin's store, sorted. A key is a file name, so this is a
        directory listing rather than a database query - the store is small by construction."""
        try:
            return sorted(p.name for p in self.store_dir.iterdir() if p.is_file() and not p.name.startswith("."))
        except OSError:
            return []

    def http_fetch(self, method: str, url: str, headers: list, body: Optional[str]) -> Union[dict, str]:
        """Make one HTTP request for the plugin, if its URL is on what was approved.

        Returns `{"status", "headers", "body"}`, or a string saying why not. The runtime
        turns that string into the WIT `err` variant, so a plugin can report the refusal in
        its own result rather than being trapped by it.

        A blocking `urllib` call rather than bot.agent_runtime.web: `web.fetch` is async and
        GET-only (this is called from a host function, on a thread that is already inside a
        wasm call), and its "the address must be public" rule is right for a model following a
        link and wrong here - a plugin whose allowlist says `127.0.0.1:9000` has been told
        exactly that it may go there. The allowlist *is* the policy for this path, which is
        also why it is checked here and not only inside the runtime.
        """
        if not host_allowed(url, self.network):
            host = urlsplit(url).hostname or url
            return (
                f"this plugin may not reach {host}: its approved network is "
                f"{', '.join(self.network) if self.network else '(none)'}"
            )
        request = urllib.request.Request(url, data=body.encode("utf-8") if body else None, method=method.upper())
        for name, value in headers or ():
            request.add_header(str(name), str(value))
        try:
            with urllib.request.urlopen(request, timeout=self._timeout_s) as response:  # noqa: S310 - the scheme was just checked
                payload = response.read(MAX_FETCH_BODY + 1)
                return {
                    "status": int(response.status),
                    "headers": {k.lower(): v for k, v in response.headers.items()},
                    "body": payload[:MAX_FETCH_BODY].decode("utf-8", "replace"),
                }
        except urllib.error.HTTPError as exc:
            # A 404 is an answer, not a failure: the plugin asked and the server said no.
            return {
                "status": int(exc.code),
                "headers": {k.lower(): v for k, v in (exc.headers or {}).items()},
                "body": exc.read(MAX_FETCH_BODY).decode("utf-8", "replace"),
            }
        except Exception as exc:  # noqa: BLE001 - a failed fetch is data for the plugin, not a crash
            return f"the request to {url} failed: {exc}"

    def now_ms(self) -> int:
        return int(time.time() * 1000)

    def random_bytes(self, length: int) -> bytes:
        return os.urandom(int(length))

    # ---- the key-value store ----------------------------------------------------------------

    def _path(self, key: str) -> Path:
        """Where one key lives. A key with a path separator or a dot in it is refused rather
        than sanitised: silently rewriting a key would mean a plugin writing `a/b` and reading
        back `a_b` and never finding out why."""
        if not key or "/" in key or "\\" in key or key.startswith(".") or key in {".", ".."}:
            raise PolicyDenied(f"key {key!r} is not a plain key: no slashes, no leading dot")
        return self.store_dir / key

    def _read(self, key: str) -> Optional[str]:
        try:
            return self._path(key).read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except PolicyDenied:
            raise
        except OSError as exc:
            logger.warning("wasm plugin %s: cannot read %s: %s", self.plugin_name, key, exc)
            return None

    def _write(self, key: str, value: str) -> None:
        path = self._path(key)
        # Written through a temporary file and renamed, so a crash mid-write leaves the old
        # value rather than half of a new one.
        tmp = path.with_name(f".{path.name}.tmp")
        with self._lock:
            tmp.write_text(value, encoding="utf-8")
            os.replace(tmp, path)


#: How much of a response body a plugin gets. A plugin that wants a file should ask for it
#: through a preopened directory, not by pulling a gigabyte through a string.
MAX_FETCH_BODY = 1 << 20


def host_allowed(url: str, allow: list) -> bool:
    """Whether `url`'s host is on an allowlist.

    An entry is `host` (any port) or `host:port`. An empty allowlist allows nothing, and
    only http and https are ever considered: a plugin's `http-fetch` is not a way to reach
    `file://`, and a URL with credentials in it is matched on the host after them, so
    `https://user:pw@allowed.example` counts as `allowed.example` and
    `https://allowed.example@evil.example` counts as `evil.example`.

    The runtime makes the same check in Rust before a request leaves the guest. Two
    implementations of one rule is not ideal, and it is still the right trade: a policy
    enforced in only one of two places has a hole in it exactly where the other one is not
    called, and this check is four lines.
    """
    if not isinstance(allow, (list, tuple, set)):
        return False
    try:
        parts = urlsplit(str(url))
    except ValueError:
        return False
    if parts.scheme.lower() not in {"http", "https"}:
        return False
    host, port = _host_and_port(parts)
    if not host:
        return False
    for entry in allow:
        entry = str(entry).strip()
        if not entry:
            continue
        e_host, e_port = _host_and_port(urlsplit(f"//{entry}"))
        if e_host.lower() != host.lower():
            continue
        if e_port is None or e_port == port:
            return True
    return False


def _host_and_port(parts) -> tuple:
    """(host, port) from a parsed URL, with the brackets off an IPv6 literal and the port
    None when the URL did not name one."""
    try:
        host = parts.hostname or ""
    except ValueError:
        return "", None
    try:
        port = parts.port
    except ValueError:
        # A malformed port is not a host we can allow.
        return "", None
    return host, None if port is None else str(port)


def _level_for(level: str) -> int:
    return {
        "trace": logging.DEBUG,
        "debug": logging.DEBUG,
        "info": logging.INFO,
        "warn": logging.WARNING,
        "error": logging.ERROR,
    }.get((level or "").lower(), logging.INFO)


class Runtime:
    """The engine every plugin in this process shares.

    One engine, not one per plugin: a compiled component is expensive (tens to hundreds of
    milliseconds depending on its size) and there is no reason to pay it twice, and the
    pooling allocator only pays off when instances come from one place.
    """

    _instance: Optional["Runtime"] = None
    _instance_lock = threading.Lock()

    def __init__(self, cache_dir: Optional[Path] = None) -> None:
        abp_wasm = require()
        if abp_wasm is None:
            raise WasmUnavailable(f"the ABP wasm runtime is not available: {unavailable_reason()}")
        self._module = abp_wasm
        self._engine = abp_wasm.Runtime(cache_dir=str(cache_dir or _default_cache_dir()))

    @classmethod
    def shared(cls) -> "Runtime":
        """The process-wide engine, built on first use."""
        if cls._instance is None:
            with cls._instance_lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    @classmethod
    def reset(cls) -> None:
        """Drop the engine. For tests, and for a rebuild of the native module in-process."""
        with cls._instance_lock:
            cls._instance = None

    def load(self, path: Path, *, services: HostServices, grants: dict, memory_limit_bytes: int = DEFAULT_MEMORY_MB * 1024 * 1024) -> "LoadedPlugin":
        """Compile a component and hand back the handle tools are called through.

        Everything slow is here: the component is read, validated against the world,
        instantiated once to prove it starts, and its manifest is read. A malformed or
        unusable component fails here, at install time, rather than at the agent's first call.
        """
        try:
            handle = self._engine.load(
                Path(path),
                grants=grants,
                services=services,
                memory_limit_bytes=int(memory_limit_bytes),
            )
        except self._module.WasmError as exc:
            raise PluginCallError(str(exc)) from exc
        return LoadedPlugin(handle)

    @property
    def version(self) -> str:
        return self._module.__version__


class _ClosedHandle:
    """Stands in for the native handle once a plugin was closed."""

    manifest: dict = {}

    def __getattr__(self, name: str) -> Any:
        raise PluginCallError("this plugin was unloaded")


class LoadedPlugin:
    """One compiled plugin, as this subsystem uses it.

    A thin wrapper over the native handle, and it exists for one reason: to turn the native
    module's exceptions into this package's. A deadline and a memory cap are not accidents -
    they are the sandbox working - so they arrive as `PluginCallError` and the rest of ABP can
    tell them apart from a plugin that returned an error, which is data.
    """

    def __init__(self, handle: Any) -> None:
        self._handle = handle

    @property
    def manifest(self) -> dict:
        return dict(self._handle.manifest)

    def tool_names(self) -> list:
        return list(self._handle.tool_names())

    def close(self) -> None:
        """Release the compiled component: call the native handle's own `close()` when it has one, then drop the
        reference so nothing can be invoked through this plugin afterwards."""
        handle, self._handle = self._handle, _ClosedHandle()
        closer = getattr(handle, "close", None)
        if callable(closer):
            try:
                closer()
            except Exception:  # noqa: BLE001 - closing must never fail a stop
                logger.debug("closing a wasm plugin handle raised", exc_info=True)

    @property
    def closed(self) -> bool:
        return isinstance(self._handle, _ClosedHandle)

    def allows_url(self, url: str) -> bool:
        """Whether `url` is on what this plugin was granted. Checked here before a request is
        made, and again inside the host service that makes it."""
        return bool(self._handle.allows_url(url))

    def invoke(self, tool: str, args_json: str, *, timeout_ms: int = DEFAULT_TIMEOUT_MS) -> dict:
        """Run one tool.

        `{"ok": json}` or `{"error": message}` is an answer and comes back as a dict; a
        deadline, a memory cap or a host failure raises `PluginCallError`, because none of
        those is something the caller can do differently by asking again.
        """
        try:
            return dict(self._handle.invoke(tool, args_json, timeout_ms=int(timeout_ms)))
        except self._handle_wasm_error() as exc:
            raise PluginCallError(str(exc)) from exc

    @staticmethod
    def _handle_wasm_error() -> type:
        return require().WasmError


def grants_for(
    *,
    preopens: Optional[dict] = None,
    network: Optional[list] = None,
    max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES,
) -> dict:
    """The capabilities handed to the runtime: exactly what was approved, never what the
    manifest asked for. Kept in one function so there is one place where that decision is
    written down."""
    return {
        "preopens": {str(k): str(v) for k, v in (preopens or {}).items()},
        "network": [str(h) for h in (network or [])],
        "max_output_bytes": int(max_output_bytes),
    }


def json_args(tool_input: Any) -> str:
    """A tool's arguments as the JSON text `invoke` takes. A non-dict is refused: every tool
    in the world takes an object, and quietly wrapping a list would hand the plugin something
    its own schema never described."""
    if tool_input is None:
        return "{}"
    if isinstance(tool_input, str):
        # Already JSON (the agent loop sometimes passes a string through); validate it.
        try:
            parsed = json.loads(tool_input)
        except json.JSONDecodeError as exc:
            raise PluginCallError(f"the arguments are not JSON: {exc}") from exc
    else:
        parsed = tool_input
    if not isinstance(parsed, dict):
        raise PluginCallError(
            f"a tool's arguments must be an object, not {type(parsed).__name__}"
        )
    return json.dumps(parsed, ensure_ascii=False)
