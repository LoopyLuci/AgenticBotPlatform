"""Which modules ABP knows, and where each one's checkout is.

The built-in list covers the LoopyLuci modules. A module's own ``abp-module.toml`` (at its repo root) replaces the
built-in manifest once the repo ships one, and more modules can be added in config/backends.yaml:

    modules:
      build_cache: E:/abp-build        # optional: cargo target dirs go here (fast storage), one folder per module
                                       # ($ABP_MODULE_BUILD_CACHE overrides it)
      brainbuilder: {path: D:/src/BrainBuilder, enabled: true}
      extra:
        - {id: my-tool, name: My Tool, repo: https://github.com/me/my-tool.git}

Where a module's checkout is, first match wins: $ABP_MODULE_<ID>_DIR (dashes as underscores), modules.<id>.path, a
folder with the module's name next to ABP's (a developer's working copy), data/modules/<Name> (cloned by ABP).

Any project can become a module: `python -m abp_modkit adopt <folder> --register` writes its abp-module.toml and adds
the folder to modules.projects, where ABP finds it (its id comes from its own manifest):

    modules:
      projects: [Z:/Projects/VMStream, X:/Projects/CompressionAgent]
"""
from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path
from typing import Any, Optional

from bot.modules import manifest as mf
from bot.modules.manifest import Manifest, ManifestError

GH = "https://github.com/LoopyLuci/"

# The built-in manifests, in the same shape as abp-module.toml. A repo's own abp-module.toml takes over from these.
BUILTIN: list[dict[str, Any]] = [
    {"module": {"id": "vm-harness", "name": "VM-Harness", "repo": GH + "VM-Harness.git", "branch": "master",
                "area": "infrastructure", "adapter": "vm_harness",
                "description": "Virtual machines and containers on every hypervisor, with its own window."},
     "checkout": {"marker": ["src/vm_harness"]}, "pipeline": {"run": ["{venv_python}", "ci/pipeline.py"]}},
    {"module": {"id": "hermes-manager", "name": "Hermes-Manager", "repo": GH + "Hermes-Manager.git", "area": "agents",
                "adapter": "hermes_manager",
                "description": "Hermes's gateway, logs, config and backups, with its own window."},
     "checkout": {"marker": ["package.json", "scripts/pipeline.mjs"]}, "pipeline": {"run": ["node", "scripts/pipeline.mjs"]}},
    {"module": {"id": "transferdaemon", "name": "TransferDaemon", "repo": GH + "TransferDaemon.git", "area": "network",
                "adapter": "transferdaemon",
                "description": "End-to-end encrypted messages and files between devices; window, terminal UI, relays."},
     "checkout": {"marker": ["transferdaemon/Cargo.toml"], "subdir": "transferdaemon"}},
    {"module": {"id": "modelmistress", "name": "ModelMistress", "repo": GH + "ModelMistress.git", "area": "models",
                "description": "Loading and hosting models: an OpenAI-compatible server with a router and backends."},
     "checkout": {"marker": ["Cargo.toml", "cli/Cargo.toml", "src/Cargo.toml"]},
     "toolchain": {"require": [{"tool": "cargo", "min": "1.85", "url": "https://rustup.rs"}]},
     "build": {"steps": [["cargo", "build", "--release", "-p", "model-mistress"]],
               "outputs": ["{target}/release/model-mistress{exe}"]},
     "hub": {"start": ["{target}/release/model-mistress{exe}", "--home", "{data}", "serve"],
             "control_file": "{data}/control.json", "start_timeout_s": 60},
     "mcp": {"stdio": ["{target}/release/model-mistress{exe}", "--home", "{data}", "mcp"]},
     "pipeline": {"run": ["python", "ci/pipeline.py"]}},
    {"module": {"id": "continuum", "name": "Continuum", "repo": GH + "Continuum.git", "area": "devices",
                "description": "Screen sharing and full remote control of devices, for people and agents."},
     "checkout": {"marker": ["Cargo.toml", "src/continuum-server/Cargo.toml"]},
     "toolchain": {"require": [{"tool": "cargo", "min": "1.75", "url": "https://rustup.rs"}]},
     "build": {"steps": [["cargo", "build", "--release", "-p", "continuum", "-p", "continuum-server",
                          "-p", "continuum-client", "-p", "relay-server"]],
               "outputs": ["{target}/release/continuum{exe}", "{target}/release/continuum-server{exe}"]},
     "ui": {"gui": ["{target}/release/continuum{exe}"]}},
    {"module": {"id": "tridentdroid", "name": "TridentDroid", "repo": GH + "TridentDroid.git", "area": "devices",
                "description": "Android emulation: devices on KVM or Windows' hypervisor, driven over gRPC."},
     "checkout": {"marker": ["Cargo.toml", "tridentd/Cargo.toml"]},
     "toolchain": {"require": [{"tool": "cargo", "min": "1.75", "url": "https://rustup.rs"}]},
     "build": {"steps": [["cargo", "build", "--release", "-p", "tridentd"]],
               "outputs": ["{target}/release/tridentd{exe}"]},
     "host": {"os": ["windows", "linux"], "needs": ["whp-or-kvm"]}},
    {"module": {"id": "brainbuilder", "name": "BrainBuilder", "repo": GH + "BrainBuilder.git", "area": "models",
                "description": "Building, training and running neural networks, visually or from a description."},
     "checkout": {"marker": ["Cargo.toml", "gui/package.json", "core/Cargo.toml"]},
     "toolchain": {"require": [{"tool": "cargo", "min": "1.75", "url": "https://rustup.rs"},
                               {"tool": "node", "min": "20", "url": "https://nodejs.org"}]},
     "pipeline": {"run": ["node", "scripts/ci/pipeline.mjs"]}},
    {"module": {"id": "cacheit", "name": "CacheIt", "repo": GH + "CacheIt.git", "area": "infrastructure",
                "description": "Tiered caching (RAM, NVMe, SSD) in front of storage, with write-back and a WAL."},
     "checkout": {"marker": ["Cargo.toml", "crates/cacheit-engine/Cargo.toml"]},
     "toolchain": {"require": [{"tool": "cargo", "min": "1.85", "url": "https://rustup.rs"}]},
     "build": {"steps": [["cargo", "build", "--release", "-p", "cacheit-daemon", "-p", "cacheit-cli",
                          "-p", "cacheit-tui", "-p", "cacheit-desktop"]],
               "outputs": ["{target}/release/cacheitd{exe}", "{target}/release/cacheit{exe}"]},
     "ui": {"gui": ["{target}/release/cacheit-gui{exe}"], "tui": ["{target}/release/cacheit-tui{exe}"]}},
    {"module": {"id": "wrightspace", "name": "Wrightspace", "repo": GH + "Wrightspace.git", "area": "building",
                "description": "Building applications for every platform and language, with agents; was WebBuilder."},
     "checkout": {"marker": ["package.json", "pnpm-workspace.yaml"]},
     "toolchain": {"require": [{"tool": "node", "min": "20", "url": "https://nodejs.org"},
                               {"tool": "pnpm", "url": "https://pnpm.io/installation"}]}},
]


def _cfg() -> dict:
    try:
        from bot.config import config
        return dict((config.current or {}).get("modules") or {})
    except Exception:  # noqa: BLE001
        return {}


def abp_root() -> Path:
    from bot.envfile import PROJECT_ROOT
    return Path(PROJECT_ROOT)


# Adopted projects (modules.projects): id -> folder, filled in as their manifests are read.
_project_dirs: dict[str, Path] = {}


def _project_manifests(taken: set[str]) -> tuple[list[Manifest], dict[str, str]]:
    out, errors = [], {}
    for raw in _cfg().get("projects") or []:
        d = Path(str(raw)).expanduser()
        f = d / mf.MANIFEST_FILE
        if not f.is_file():
            errors[f"project:{d.name}"] = f"{d}: no {mf.MANIFEST_FILE} (adopt it: python -m abp_modkit adopt {d})"
            continue
        try:
            m = mf.load(f)
        except ManifestError as e:
            errors[f"project:{d.name}"] = str(e)
            continue
        if m.id in taken:
            errors[m.id] = f"{d}: id {m.id!r} is already used by another module"
            continue
        _project_dirs[m.id] = d
        taken.add(m.id)
        out.append(m)
    return out, errors


def _builtin_manifests() -> list[Manifest]:
    out = [mf.parse(d) for d in BUILTIN]
    from bot.octopus import connectors   # the Octopus estate's connectors, one repo each
    out += [mf.parse(d) for d in connectors.manifests()]
    for extra in _cfg().get("extra") or []:
        if isinstance(extra, dict):
            try:
                out.append(mf.parse({"module": {k: extra.get(k) for k in ("id", "name", "repo", "branch", "area",
                                                                            "description")},
                                     "checkout": {"marker": extra.get("marker") or []}}, source="config"))
            except ManifestError:
                continue
    return out


def _mod_cfg(mid: str) -> dict:
    v = _cfg().get(mid)
    return dict(v) if isinstance(v, dict) else {}


def is_checkout(m: Manifest, d: Path) -> bool:
    if not d.is_dir():
        return False
    if (d / mf.MANIFEST_FILE).is_file():
        return True
    return bool(m.marker) and all((d / p).exists() for p in m.marker)


def install_dir(m: Manifest) -> Path:
    env = os.environ.get("ABP_MODULE_" + m.id.upper().replace("-", "_") + "_DIR")
    for candidate in (env, _mod_cfg(m.id).get("path")):
        if candidate:
            return Path(str(candidate)).expanduser()
    if m.id in _project_dirs:
        return _project_dirs[m.id]
    sibling = abp_root().parent / m.name
    if is_checkout(m, sibling):
        return sibling
    # A checkout named after its repo (abp-octopus-budget), next to ABP or in a folder listed in modules.search_paths.
    repo_name = m.repo.rstrip("/").rsplit("/", 1)[-1].removesuffix(".git") if m.repo else ""
    if repo_name:
        for folder in [abp_root().parent, *(_cfg().get("search_paths") or [])]:
            cand = Path(str(folder)).expanduser() / repo_name
            if is_checkout(m, cand):
                return cand
    return abp_root() / "data" / "modules" / m.name


def data_dir(m: Manifest) -> Path:
    return abp_root() / "data" / "module-data" / m.id


def target_dir(m: Manifest) -> Path:
    """The cargo target dir: <build_cache>/<id> when modules.build_cache is set (fast storage), else the workspace's."""
    cache = os.environ.get("ABP_MODULE_BUILD_CACHE") or _cfg().get("build_cache")
    if cache:
        return Path(str(cache)).expanduser() / m.id
    return install_dir(m) / m.subdir / "target" if m.subdir else install_dir(m) / "target"


def placeholders(m: Manifest) -> dict[str, str]:
    repo = install_dir(m)
    ws = repo / m.subdir if m.subdir else repo
    venv = repo / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    return {"repo": str(repo), "ws": str(ws), "exe": ".exe" if os.name == "nt" else "", "target": str(target_dir(m)),
            "data": str(data_dir(m)), "home": str(Path.home()), "localappdata": str(mf.localappdata()),
            "venv_python": str(venv), "abp_python": sys.executable, "abp_root": str(abp_root())}


def expand(m: Manifest, value: str) -> str:
    return mf.expand(value, placeholders(m))


def expand_cmd(m: Manifest, cmd: list[str]) -> list[str]:
    vals = placeholders(m)
    return [mf.expand(x, vals) for x in cmd]


_cache: dict[str, Any] = {"at": 0.0, "mods": {}, "errors": {}}
_lock = threading.Lock()


def _load() -> tuple[dict[str, Manifest], dict[str, str]]:
    mods: dict[str, Manifest] = {}
    errors: dict[str, str] = {}
    for m in _builtin_manifests():
        own = install_dir(m) / mf.MANIFEST_FILE
        if own.is_file():
            try:
                repo_m = mf.load(own)
                if repo_m.id != m.id:
                    raise ManifestError(f"its id is {repo_m.id!r}, expected {m.id!r}")
                repo_m.adapter = repo_m.adapter or m.adapter      # an adapter stays until the repo's hub replaces it
                m = repo_m
            except ManifestError as e:
                errors[m.id] = f"{own}: {e} (using the built-in manifest)"
        if _mod_cfg(m.id).get("enabled", True) is False:
            continue
        mods[m.id] = m
    projects, perrors = _project_manifests(set(mods))
    errors.update(perrors)
    for m in projects:
        if _mod_cfg(m.id).get("enabled", True) is not False:
            mods[m.id] = m
    return mods, errors


def modules(refresh: bool = False) -> dict[str, Manifest]:
    """Every known module by id (cached 10 s: reading manifests touches the disk)."""
    with _lock:
        if refresh or time.monotonic() - _cache["at"] > 10:
            _cache["mods"], _cache["errors"] = _load()
            _cache["at"] = time.monotonic()
        return dict(_cache["mods"])


def manifest_errors() -> dict[str, str]:
    modules()
    return dict(_cache["errors"])


class UnknownModule(KeyError):
    pass


def get(mid: str) -> Manifest:
    m = modules().get(str(mid).lower())
    if m is None:
        names = ", ".join(sorted(modules())) or "none"
        raise UnknownModule(f"no module {mid!r} (known: {names})")
    return m


def find(ref: str) -> Optional[Manifest]:
    """A module by id or name (case-insensitive)."""
    r = str(ref).strip().lower()
    for m in modules().values():
        if r in (m.id, m.name.lower()):
            return m
    return None
