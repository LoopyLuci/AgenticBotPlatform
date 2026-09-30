"""The module manifest, ``abp-module.toml``: what a module is, how to build it, how to run and reach its control hub.

Version 1. Every section but [module] is optional: a module with only [module] and [checkout] can still be cloned,
updated and shown; each further section turns on more (build, hub, MCP, windows, pipeline).

    [module]    id, name, repo, branch, api, area, description, adapter
    [checkout]  marker (paths that must exist to call a folder this module's checkout), subdir (the workspace),
                dir (where the checkout is, for an overlay made for one particular clone)
    [toolchain] require = [{tool, min, url}]
    [build]     steps (commands run in the workspace), outputs (paths that exist once built), env
    [hub]       start, control_file, api_base, health, operations, call, stop, start_timeout_s
    [mcp]       stdio (the command that serves MCP over stdio)
    [ui]        gui, tui (commands), panel ("operations", or "custom:<name>"), web (a URL, or "service": the web UI
                of the server its abp_modkit hub runs, shown in a pane)
    [provider]  openai (a base URL, or "service": the OpenAI-compatible API of the server its hub runs), key_env
    [abp]       connect (true: the module uses ABP back; ABP gives it ABP_URL and ABP_KEY, a key of its own with the
                scopes of `preset`, default "companion-app"), preset
    [host]      os, needs (e.g. "kvm", "whp", "gpu", "android-sdk", "macos-peer")
    [pipeline]  run (its local CI/CD pipeline)

Commands and paths may use these placeholders:
    {repo} the checkout, {ws} the workspace (repo/subdir), {exe} ".exe" on Windows, {target} the cargo target dir,
    {data} the module's data folder under ABP, {home} the user's home, {localappdata} LocalAppData (Windows) or the
    XDG data dir, {venv_python} the python of the checkout's own .venv, {abp_python} the python ABP runs on and
    {abp_root} ABP's folder (so a module can use abp_modkit, which ships with ABP: `python -m abp_modkit adopt`),
    {overlay} the folder holding this manifest when it is an overlay (see below), else the checkout.

An overlay is a manifest (and its abp-ops.toml) kept on ABP's side instead of in the repo, so a third-party repo
becomes a module with its checkout left exactly as upstream has it: ABP ships some in catalog/<id>/, and users make
more in data/module-overlays/<id>/ (`python -m abp_modkit adopt <checkout> --overlay <folder>`, or the Module Hub).
"""
from __future__ import annotations

import os
import re
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

API_VERSION = 1
MANIFEST_FILE = "abp-module.toml"
_ID_RE = re.compile(r"^[a-z][a-z0-9-]{1,40}$")
_PLACEHOLDER_RE = re.compile(r"\{([a-z_]+)\}")
PLACEHOLDERS = {"repo", "ws", "exe", "target", "data", "home", "localappdata", "venv_python", "abp_python", "abp_root",
                "overlay"}
OS_NAMES = {"windows", "linux", "macos"}


class ManifestError(ValueError):
    pass


@dataclass
class Requirement:
    tool: str
    min: str = ""
    url: str = ""


@dataclass
class Hub:
    start: list[str] = field(default_factory=list)
    control_file: str = ""
    api_base: str = "/v1"
    health: str = "/health"
    operations: str = "/operations"
    call: str = "/call/{op}"
    stop: str = "/service/stop"
    start_timeout_s: float = 60.0


@dataclass
class Manifest:
    id: str
    name: str
    repo: str
    branch: str = "main"
    api: int = API_VERSION
    area: str = "tools"
    description: str = ""
    adapter: str = ""                      # the name of an adapter in adapters.py (modules ABP drove before M0)
    marker: list[str] = field(default_factory=list)
    subdir: str = ""
    requires: list[Requirement] = field(default_factory=list)
    build_steps: list[list[str]] = field(default_factory=list)
    build_outputs: list[str] = field(default_factory=list)
    build_env: dict[str, str] = field(default_factory=dict)
    hub: Optional[Hub] = None
    mcp_stdio: list[str] = field(default_factory=list)
    gui: list[str] = field(default_factory=list)
    tui: list[str] = field(default_factory=list)
    panel: str = "operations"
    web: str = ""                          # a URL, or "service" (asked of the abp_modkit hub while it runs)
    openai: str = ""                       # the same, for an OpenAI-compatible API ABP offers as a provider
    openai_key_env: str = ""
    abp_connect: bool = False              # the module uses ABP back: it gets ABP_URL and ABP_KEY (its own scoped key)
    abp_preset: str = "companion-app"
    host_os: list[str] = field(default_factory=lambda: sorted(OS_NAMES))
    host_needs: list[str] = field(default_factory=list)
    pipeline: list[str] = field(default_factory=list)
    source: str = "builtin"                # "builtin", or the path of the repo's abp-module.toml
    checkout_dir: str = ""                 # [checkout] dir: where its checkout is (an overlay made for one clone)
    overlay: str = ""                      # the overlay folder holding this manifest (set by the registry), or ""

    def public(self) -> dict[str, Any]:
        """What the dashboard and the agent see."""
        return {
            "id": self.id, "name": self.name, "repo": self.repo, "branch": self.branch, "area": self.area,
            "description": self.description, "source": self.source, "adapter": self.adapter or None,
            "can_build": bool(self.build_steps) or bool(self.adapter), "has_hub": self.hub is not None or bool(self.adapter),
            "has_mcp": bool(self.mcp_stdio), "has_gui": bool(self.gui), "has_tui": bool(self.tui),
            "has_pipeline": bool(self.pipeline), "panel": self.panel, "has_web": bool(self.web),
            "has_provider": bool(self.openai), "uses_abp": self.abp_connect, "host": {"os": self.host_os, "needs": self.host_needs},
            "overlay": self.overlay or None,
            "requires": [r.__dict__ for r in self.requires],
        }


def _str_list(v: Any, where: str) -> list[str]:
    if v is None:
        return []
    if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
        raise ManifestError(f"{where} must be a list of strings")
    return list(v)


def _check_placeholders(values: list[str], where: str) -> None:
    for v in values:
        for name in _PLACEHOLDER_RE.findall(v):
            if name not in PLACEHOLDERS and name != "op":
                raise ManifestError(f"{where}: unknown placeholder {{{name}}} (known: {', '.join(sorted(PLACEHOLDERS))})")


def parse(data: dict, source: str = "builtin") -> Manifest:
    """Validate a manifest (as parsed TOML, or the same shape as a dict) and return it."""
    mod = data.get("module")
    if not isinstance(mod, dict):
        raise ManifestError("[module] is required")
    mid = str(mod.get("id") or "")
    if not _ID_RE.match(mid):
        raise ManifestError(f"module.id {mid!r} must be lowercase letters, digits and dashes (2-41 characters)")
    for key in ("name", "repo"):
        if not isinstance(mod.get(key), str) or not mod[key].strip():
            raise ManifestError(f"module.{key} is required")
    api = int(mod.get("api", API_VERSION))
    if api > API_VERSION:
        raise ManifestError(f"module.api {api} is newer than this ABP understands ({API_VERSION}); update ABP")
    m = Manifest(id=mid, name=mod["name"].strip(), repo=mod["repo"].strip(), branch=str(mod.get("branch") or "main"),
                 api=api, area=str(mod.get("area") or "tools"), description=str(mod.get("description") or ""),
                 adapter=str(mod.get("adapter") or ""), source=source)

    co = data.get("checkout") or {}
    m.marker = _str_list(co.get("marker"), "checkout.marker")
    m.subdir = str(co.get("subdir") or "")
    if ".." in Path(m.subdir).parts or Path(m.subdir).is_absolute():
        raise ManifestError("checkout.subdir must be a relative path inside the repo")
    m.checkout_dir = str(co.get("dir") or "")

    for r in (data.get("toolchain") or {}).get("require") or []:
        if not isinstance(r, dict) or not r.get("tool"):
            raise ManifestError("toolchain.require entries need a tool")
        m.requires.append(Requirement(str(r["tool"]), str(r.get("min") or ""), str(r.get("url") or "")))

    b = data.get("build") or {}
    steps = b.get("steps") or []
    if not isinstance(steps, list) or not all(isinstance(s, list) and s and all(isinstance(x, str) for x in s) for s in steps):
        raise ManifestError("build.steps must be a list of commands, each a non-empty list of strings")
    m.build_steps = [list(s) for s in steps]
    m.build_outputs = _str_list(b.get("outputs"), "build.outputs")
    env = b.get("env") or {}
    if not isinstance(env, dict):
        raise ManifestError("build.env must be a table")
    m.build_env = {str(k): str(v) for k, v in env.items()}

    h = data.get("hub")
    if h is not None:
        if not isinstance(h, dict):
            raise ManifestError("[hub] must be a table")
        m.hub = Hub(start=_str_list(h.get("start"), "hub.start"), control_file=str(h.get("control_file") or ""))
        for key in ("api_base", "health", "operations", "call", "stop"):
            if key in h:
                setattr(m.hub, key, str(h[key]))
        m.hub.start_timeout_s = float(h.get("start_timeout_s") or 60)
        if not m.hub.control_file:
            raise ManifestError("hub.control_file is required: the file the hub writes with its url and token")
        if "{op}" not in m.hub.call:
            raise ManifestError("hub.call must contain {op}")

    m.mcp_stdio = _str_list((data.get("mcp") or {}).get("stdio"), "mcp.stdio")
    ui = data.get("ui") or {}
    m.gui = _str_list(ui.get("gui"), "ui.gui")
    m.tui = _str_list(ui.get("tui"), "ui.tui")
    m.panel = str(ui.get("panel") or "operations")
    m.web = str(ui.get("web") or "")
    prov = data.get("provider") or {}
    m.openai = str(prov.get("openai") or "")
    m.openai_key_env = str(prov.get("key_env") or "")
    back = data.get("abp") or {}
    m.abp_connect = bool(back.get("connect", False))
    m.abp_preset = str(back.get("preset") or "companion-app")
    for key, v in (("ui.web", m.web), ("provider.openai", m.openai)):
        if v and v != "service" and not v.startswith(("http://", "https://")):
            raise ManifestError(f"{key} is a URL or \"service\"")
    host = data.get("host") or {}
    if "os" in host:
        m.host_os = _str_list(host["os"], "host.os")
        bad = set(m.host_os) - OS_NAMES
        if bad:
            raise ManifestError(f"host.os: unknown {sorted(bad)} (known: {sorted(OS_NAMES)})")
    m.host_needs = _str_list(host.get("needs"), "host.needs")
    m.pipeline = _str_list((data.get("pipeline") or {}).get("run"), "pipeline.run")

    _check_placeholders([*m.build_outputs, *m.mcp_stdio, *m.gui, *m.tui, *m.pipeline,
                         *[x for s in m.build_steps for x in s], *m.build_env.values(),
                         *((m.hub.start + [m.hub.control_file, m.hub.call]) if m.hub else [])], f"{m.id}")
    return m


def load(path: Path) -> Manifest:
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as e:
        raise ManifestError(f"{path}: {e}") from None
    return parse(data, source=str(path))


def this_os() -> str:
    return {"win32": "windows", "darwin": "macos"}.get(sys.platform, "linux")


def localappdata() -> Path:
    if sys.platform == "win32":
        return Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support"
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")


def expand(value: str, values: dict[str, str]) -> str:
    """Fill in the placeholders (unknown ones are left as they are; {op} is filled in by the client)."""
    return _PLACEHOLDER_RE.sub(lambda mt: values.get(mt.group(1), mt.group(0)), value)
