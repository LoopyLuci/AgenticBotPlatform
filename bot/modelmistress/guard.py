"""Guards for the edge cases around the shared ModelMistress hub (found 2026-10-19 bringing up the 0.3.0 hub):

    packaged_identity()   this process runs with an MSIX package identity (e.g. a tool launched from a packaged desktop
                          app): Windows virtualizes its writes under %LOCALAPPDATA% / %APPDATA% into the package's own store
                          and lets a copy there SHADOW the real file. Such a process sees a stale control.json and its
                          writes never reach the real MM home.
    virtualized(path)     whether `path` is in that virtualized view for this process.
    hub_processes(home)   hub processes (`model-mistress ... serve --home <home>`) already running for a home, whether or
                          not this process can read their control file - so a second hub is never started for one home.
    engine_problem(hub)   a hub that runs but cannot serve models (built without its engine: "llama-server was not found").
    context_problem(...)  a model whose per-slot context is too small for agent work (each request gets n_ctx / slots).

Each answers with a short reason (or None) for the caller to show; none of them changes anything (the redirect probe writes
one empty, uniquely named file and removes it, once per process).
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("bot.modelmistress.guard")

_APPMODEL_ERROR_NO_PACKAGE = 15700
_ERROR_INSUFFICIENT_BUFFER = 122


def packaged_identity() -> Optional[str]:
    """This process's package full name when it runs with an MSIX package identity, else None (and always None off Windows)."""
    if os.name != "nt":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        fn = ctypes.windll.kernel32.GetCurrentPackageFullName
        fn.argtypes = [ctypes.POINTER(wintypes.UINT), wintypes.LPWSTR]
        fn.restype = wintypes.LONG
        n = wintypes.UINT(0)
        rc = fn(ctypes.byref(n), None)
        if rc == _APPMODEL_ERROR_NO_PACKAGE:
            return None
        if rc != _ERROR_INSUFFICIENT_BUFFER or n.value <= 0:
            return None
        buf = ctypes.create_unicode_buffer(n.value)
        return buf.value if fn(ctypes.byref(n), buf) == 0 and buf.value else None
    except Exception:  # noqa: BLE001 - an old Windows without the API is not packaged
        return None


_probe_cache: dict[str, Optional[str]] = {}


def _probe(root: str, store: str) -> Optional[str]:
    """Write a uniquely named file in `root` and look for it in every package's LocalCache/<store>: the package whose store got
    it is the one this process's writes there are redirected to. GetCurrentPackageFullName does not see every such process
    (a tool started from a packaged desktop app reported none while its writes were redirected), so this is the real test."""
    if root in _probe_cache:
        return _probe_cache[root]
    import uuid

    name = f".abp-vprobe-{uuid.uuid4().hex}.tmp"
    pkg: Optional[str] = None
    probe = Path(root) / name
    try:
        probe.write_bytes(b"")
        packages = Path(os.environ.get("LOCALAPPDATA", root)) / "Packages"
        for d in packages.iterdir() if packages.is_dir() else []:
            if (d / "LocalCache" / store / name).exists():
                pkg = d.name
                break
    except OSError:
        pkg = None
    finally:
        try:
            probe.unlink()
        except OSError:
            pass
    _probe_cache[root] = pkg
    return pkg


def virtualized(path: Path | str) -> Optional[str]:
    """A reason when `path` is under %LOCALAPPDATA% / %APPDATA% and this process's view there is redirected into a package's
    store (an MSIX-packaged process, or one started from one): it may see a stale copy and its writes never reach the real file."""
    if os.name != "nt":
        return None
    p = os.path.normcase(os.path.abspath(str(path)))
    for var, store in (("LOCALAPPDATA", "Local"), ("APPDATA", "Roaming")):
        root = os.environ.get(var, "")
        r = os.path.normcase(os.path.abspath(root)) if root else ""
        if not r or not (p == r or p.startswith(r + os.sep)):
            continue
        pkg = packaged_identity() or _probe(root, store)
        if pkg:
            return (f"this process runs inside the packaged app {pkg.split('_')[0]}, so Windows redirects its view of {path}: "
                    "it may see a stale copy of the hub's control file and its writes there never reach the real one")
    return None


def _same_path(a: str, b: Path) -> bool:
    try:
        return os.path.normcase(os.path.abspath(a.strip('"'))) == os.path.normcase(os.path.abspath(str(b)))
    except (TypeError, ValueError):
        return False


def hub_processes(home: Path) -> list[int]:
    """Pids of running `model-mistress ... serve` processes whose --home is `home` (another process's hub included)."""
    try:
        import psutil
    except ImportError:
        return []
    out: list[int] = []
    # names first, a command line only for a model-mistress process: reading every process's command line took ~4.5 s
    # on Windows, on every hub lookup that found no hub
    for p in psutil.process_iter(["pid", "name"]):
        try:
            name = (p.info.get("name") or "").lower()
            if not name.startswith("model-mistress"):
                continue
            cmd = p.info.get("cmdline") or p.cmdline() or []
            if "serve" not in cmd or "--home" not in cmd:
                continue
            i = cmd.index("--home")
            if i + 1 < len(cmd) and _same_path(cmd[i + 1], home):
                out.append(int(p.info["pid"]))
        except (psutil.Error, ValueError, TypeError):
            continue
    return out


def engine_problem(hub: Any) -> Optional[str]:
    """A reason when the hub answers but has no inference engine (it would accept requests and serve nothing)."""
    try:
        from bot.modelmistress import client

        st = client.call("service.status", {}, hub=hub, timeout=10)
    except Exception:  # noqa: BLE001 - no status op, or it failed: nothing to say
        return None
    if not isinstance(st, dict) or "llama_server" not in st:
        return None                      # this hub version does not report it
    if not st.get("llama_server"):
        return (f"the ModelMistress hub {getattr(hub, 'version', '')} has no inference engine (llama-server not found): "
                "rebuild the module with --features bundled-engine")
    return None


def served_context(models: list[dict], model: str) -> Optional[int]:
    """The per-slot context `model` is served with, from the hub's /v1/models `serving` data (None when not reported)."""
    for m in models or []:
        if m.get("id") != model:
            continue
        sv = m.get("serving") if isinstance(m.get("serving"), dict) else {}
        per = sv.get("n_ctx_per_slot")
        if per is None and sv.get("n_ctx") and sv.get("slots"):
            per = int(sv["n_ctx"]) // max(1, int(sv["slots"]))
        try:
            return int(per) if per else None
        except (TypeError, ValueError):
            return None
    return None


def context_problem(models: list[dict], model: str, need: int) -> Optional[str]:
    """A reason when `model`'s per-slot context is below `need` tokens (an agent's requests would be refused mid-task)."""
    per = served_context(models, model)
    if per is not None and per < need:
        return (f"{model} is served with {per} context tokens per request (n_ctx / slots) but agent work needs at least {need}: "
                "raise ctx_size (or lower parallel) in the hub's settings")
    return None
