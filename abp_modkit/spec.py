"""The operations spec, ``abp-ops.toml``: what a project can do, as ABP operations.

    [service]                    # the project's own server, if it has one (all optional)
    id = "vmstream"
    name = "VMStream"
    base_url = "http://127.0.0.1:18431"   # where its HTTP API answers ("" = no HTTP API)
    health = "/health"                    # probed for service.status ("" = no probe)
    start = ["{python}", "-m", "uvicorn", "vmstream.app:app", "--port", "18431"]   # how to start it ([] = not managed)
                                          # (a process with no base_url - a chat bot, a worker - is managed too:
                                          # "running" is then "its process is alive")
    cwd = "."                             # relative to the project
    env = { LOG_LEVEL = "info", BOT_TOKEN = "{secret:BOT_TOKEN}", KEY = "{secret?:KEY}" }
                                          # {secret:X}: $X, or what service.set_secret stored (required);
                                          # {secret?:X}: the same, but optional
    ready_timeout_s = 60
    auth = "none"                         # or "bearer-env:NAME": send $NAME (or the hub's secret NAME) as a Bearer token
    web = "/"                             # its web UI, a path on base_url ("" = none); ABP can show it in a pane
    openai = "/v1"                        # an OpenAI-compatible API under base_url ("" = none); ABP offers it as a provider

    [[op]]                       # one of the project's HTTP routes
    id = "streams.get"
    kind = "http"
    method = "GET"
    path = "/api/streams/{id}"   # {id}, :id and [id] become required inputs
    summary = "..."

    [[op]]                       # a command: a CLI subcommand, a script, an npm script, a make target
    id = "cli.scan"
    kind = "cmd"
    argv = ["{python}", "-m", "tool", "scan", "{target}"]
    cwd = "."
    timeout_s = 600
    background = false           # true: runs as a job (jobs.get follows it)
    extra_args = true            # the caller may append arguments ("args": [...])
    mutating = true
    summary = "..."
    [op.inputs]
    target = { type = "string", required = true, description = "what to scan" }

Placeholders in argv, cwd, env and start: {project} the project folder, {python} its .venv's python (or the one
running the hub), {venv_bin} its .venv's scripts folder, {exe} ".exe" on Windows, {bat} ".bat" on Windows, {data}
the hub's data folder, {port} the service port (from base_url), {target} the cargo target dir (ABP passes it),
{pwsh} PowerShell 7 or Windows PowerShell, and any input by name. Every command also gets its inputs as JSON in
$ABP_OP_ARGS, so a script can read them without parsing arguments.
"""
from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE")
KINDS = ("http", "cmd")
_PARAM = re.compile(r":([A-Za-z_][A-Za-z0-9_]*)\??|\{([A-Za-z_][A-Za-z0-9_]*)\}|\[([A-Za-z_][A-Za-z0-9_]*)\]|<(?:\w+:)?([A-Za-z_][A-Za-z0-9_]*)>")
_ID = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,120}$")
_BUILTIN_PH = {"project", "python", "venv_bin", "exe", "bat", "data", "port", "target", "pwsh"}


class SpecError(ValueError):
    pass


@dataclass
class Op:
    id: str
    kind: str = "http"
    summary: str = ""
    # http
    method: str = "GET"
    path: str = ""
    # cmd
    argv: list[str] = field(default_factory=list)
    cwd: str = "."
    timeout_s: float = 300.0
    background: bool = False
    extra_args: bool = False
    inputs: dict[str, dict] = field(default_factory=dict)
    env: dict[str, str] = field(default_factory=dict)      # cmd: more environment ({secret:NAME} allowed)
    mutating_flag: bool | None = None
    destructive_flag: bool | None = None

    @property
    def params(self) -> list[str]:
        if self.kind == "http":
            return [next(g for g in m if g) for m in _PARAM.findall(self.path)]
        return [k for k, v in self.inputs.items() if v.get("required")]

    @property
    def mutating(self) -> bool:
        if self.mutating_flag is not None:
            return self.mutating_flag
        return self.method != "GET" if self.kind == "http" else True

    @property
    def destructive(self) -> bool:
        if self.destructive_flag is not None:
            return self.destructive_flag
        return self.kind == "http" and self.method == "DELETE"


@dataclass
class Service:
    id: str
    name: str
    base_url: str = ""
    health: str = ""
    start: list[str] = field(default_factory=list)
    cwd: str = "."
    env: dict[str, str] = field(default_factory=dict)
    ready_timeout_s: float = 60.0
    auth: str = "none"
    web: str = ""
    openai: str = ""
    description: str = ""


@dataclass
class Spec:
    service: Service
    ops: list[Op] = field(default_factory=list)

    @property
    def id(self) -> str:
        return self.service.id

    def op(self, oid: str) -> Op | None:
        return next((o for o in self.ops if o.id == oid), None)


def _strs(v: Any, where: str) -> list[str]:
    if v is None:
        return []
    if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
        raise SpecError(f"{where} must be a list of strings")
    return list(v)


def parse(d: dict, where: str = "abp-ops.toml") -> Spec:
    s = d.get("service") or {}
    if not s.get("id") or not s.get("name"):
        raise SpecError(f"{where}: [service] needs id and name")
    auth = str(s.get("auth") or "none")
    if auth != "none" and not auth.startswith("bearer-env:"):
        raise SpecError(f"{where}: service.auth is none or bearer-env:NAME")
    svc = Service(id=str(s["id"]), name=str(s["name"]), base_url=str(s.get("base_url") or "").rstrip("/"),
                  health=str(s.get("health") or ""), start=_strs(s.get("start"), "service.start"),
                  cwd=str(s.get("cwd") or "."), env={str(k): str(v) for k, v in (s.get("env") or {}).items()},
                  ready_timeout_s=float(s.get("ready_timeout_s") or 60), auth=auth, web=str(s.get("web") or ""),
                  openai=str(s.get("openai") or ""), description=str(s.get("description") or ""))
    if (svc.web or svc.openai or svc.health) and not svc.base_url:
        raise SpecError(f"{where}: service.web, openai and health need service.base_url")
    ops, seen = [], set()
    for o in d.get("op") or []:
        kind = str(o.get("kind") or ("cmd" if "argv" in o else "http"))
        oid = str(o.get("id") or "")
        if kind not in KINDS:
            raise SpecError(f"{where}: op {oid}: kind is http or cmd")
        if not _ID.match(oid) or oid.split(".")[0] in ("service", "jobs", "api", "project"):
            raise SpecError(f"{where}: op id {oid!r} is empty, has odd characters or uses a built-in group")
        if oid in seen:
            raise SpecError(f"{where}: duplicate op id {oid}")
        seen.add(oid)
        op = Op(id=oid, kind=kind, summary=str(o.get("summary") or ""),
                mutating_flag=o.get("mutating") if isinstance(o.get("mutating"), bool) else None,
                destructive_flag=o.get("destructive") if isinstance(o.get("destructive"), bool) else None)
        if kind == "http":
            op.method, op.path = str(o.get("method") or "GET").upper(), str(o.get("path") or "")
            if op.method not in METHODS or not op.path.startswith("/"):
                raise SpecError(f"{where}: op {oid}: bad {op.method} {op.path}")
            if not svc.base_url:
                raise SpecError(f"{where}: op {oid} is an HTTP route but service.base_url is not set")
        else:
            op.argv = _strs(o.get("argv"), f"op {oid}.argv")
            if not op.argv:
                raise SpecError(f"{where}: op {oid}: argv is empty")
            op.cwd = str(o.get("cwd") or ".")
            op.timeout_s = float(o.get("timeout_s") or 300)
            op.background = bool(o.get("background", False))
            op.extra_args = bool(o.get("extra_args", False))
            inputs = o.get("inputs") or {}
            if not isinstance(inputs, dict) or not all(isinstance(v, dict) for v in inputs.values()):
                raise SpecError(f"{where}: op {oid}.inputs must be a table of tables")
            op.inputs = {str(k): dict(v) for k, v in inputs.items()}
            env = o.get("env") or {}
            if not isinstance(env, dict):
                raise SpecError(f"{where}: op {oid}.env must be a table")
            op.env = {str(k): str(v) for k, v in env.items()}
            for ph in re.findall(r"\{([A-Za-z_][A-Za-z0-9_]*)\}", " ".join(op.argv + [op.cwd])):
                if ph not in _BUILTIN_PH and ph not in op.inputs:
                    raise SpecError(f"{where}: op {oid} uses {{{ph}}}, which is neither an input nor a placeholder")
        ops.append(op)
    return Spec(svc, ops)


def load(path: str | Path) -> Spec:
    p = Path(path)
    try:
        return parse(tomllib.loads(p.read_text(encoding="utf-8")), str(p))
    except tomllib.TOMLDecodeError as e:
        raise SpecError(f"{p}: {e}") from None


def q(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, list):
        return "[" + ", ".join(q(x) for x in v) + "]"
    if isinstance(v, dict):
        return "{ " + ", ".join(f"{_key(k)} = {q(x)}" for k, x in v.items()) + " }"
    return '"' + str(v).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n") + '"'


def _key(k: str) -> str:
    return k if re.fullmatch(r"[A-Za-z0-9_-]+", k) else q(k)


def dump(spec: Spec, header: str = "") -> str:
    s = spec.service
    out = [header or "# What this project can do, as ABP operations (abp_modkit). Edit freely: adopting it again",
           "# (`python -m abp_modkit adopt .`) keeps edited summaries, settings and ops you added, and picks up new",
           "# routes and commands.", "", "[service]"]
    for k in ("id", "name", "description", "base_url", "health", "start", "cwd", "env", "ready_timeout_s", "auth", "web",
              "openai"):
        v = getattr(s, k)
        if v in ("", [], {}) and k not in ("id", "name"):
            continue
        if k == "cwd" and v == ".":
            continue
        if k == "ready_timeout_s" and v == 60.0:
            continue
        if k == "auth" and v == "none":
            continue
        out.append(f"{k} = {q(int(v) if isinstance(v, float) and v.is_integer() else v)}")
    for o in spec.ops:
        out += ["", "[[op]]", f"id = {q(o.id)}", f"kind = {q(o.kind)}"]
        if o.summary:
            out.append(f"summary = {q(o.summary)}")
        if o.kind == "http":
            out += [f"method = {q(o.method)}", f"path = {q(o.path)}"]
        else:
            out.append(f"argv = {q(o.argv)}")
            if o.cwd != ".":
                out.append(f"cwd = {q(o.cwd)}")
            if o.timeout_s != 300:
                out.append(f"timeout_s = {int(o.timeout_s)}")
            if o.background:
                out.append("background = true")
            if o.extra_args:
                out.append("extra_args = true")
            if o.env:
                out.append(f"env = {q(o.env)}")
        if o.mutating_flag is not None:
            out.append(f"mutating = {q(o.mutating_flag)}")
        if o.destructive_flag is not None:
            out.append(f"destructive = {q(o.destructive_flag)}")
        if o.inputs:
            out.append("[op.inputs]")
            for k, v in o.inputs.items():
                out.append(f"{_key(k)} = {q(v)}")
    return "\n".join(out) + "\n"
