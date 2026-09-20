"""Where run_shell commands run, and with what environment (roadmap P2).

    native_agent:
      sandbox:
        backend: local          # local | docker
        env:
          mode: secrets         # secrets | minimal | inherit
          allow: []             # names to keep even if they look secret (secrets/minimal) or to add (minimal)
          set: {}               # NAME: value  or  NAME: "${SERVER_ENV_VAR}" - injected into commands
        docker:
          image: python:3.11-slim
          network: none         # none | bridge | host is refused
          memory: 1g
          cpus: "2"
          pids: 256
          user: ""              # "" = the image default (on Linux/macOS hosts the caller's uid:gid)
          extra_args: []

**Environment.** A command used to inherit the server's whole environment, API keys
included: `env` or `echo $ANTHROPIC_API_KEY` would put them in the transcript. Now the
default `secrets` mode removes every variable whose name looks like a credential (see
secrets_guard.py). `minimal` keeps only a short list of harmless system variables plus
`allow`. `inherit` restores the old behaviour. `set` adds variables, and each value is
registered with secrets_guard so it is also redacted from any output.

**Backends.** `local` runs on the host as before (with the scrubbed environment). `docker`
runs each command in a fresh container: the workspace mounted at /workspace, no network by
default, memory / cpu / process limits, all Linux capabilities dropped, no privilege
escalation. The docker backend fails closed: if Docker is not installed or the daemon is
not running the command is refused, never quietly run on the host instead.

Not built: SSH and WSL backends (they would need the file tools to work on the remote
side too, which is the "cloud computer" of roadmap P6), Windows job objects, and
network egress control for the local backend (only the docker backend can cut the network).
This has been exercised against a stand-in `docker` program, not a real Docker daemon.
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import subprocess
import uuid
from pathlib import Path
from typing import Optional

from bot.agent_runtime import secrets_guard
from bot.agent_runtime.errors import ToolError

BACKENDS = ("local", "docker")
ENV_MODES = ("secrets", "minimal", "inherit")
_MINIMAL = {"PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "COMSPEC", "TEMP", "TMP", "TMPDIR", "HOME", "USERPROFILE",
            "HOMEDRIVE", "HOMEPATH", "APPDATA", "LOCALAPPDATA", "PROGRAMFILES", "PROGRAMFILES(X86)", "PROGRAMDATA",
            "USER", "USERNAME", "LOGNAME", "SHELL", "LANG", "LC_ALL", "TERM", "TZ", "PWD", "NUMBER_OF_PROCESSORS",
            "PROCESSOR_ARCHITECTURE", "OS"}
_DOCKER_NETWORKS = ("none", "bridge")
_INJECT = re.compile(r"^\$\{([A-Za-z_][A-Za-z0-9_]*)\}$")


def _config() -> dict:
    try:
        from bot.config import config

        return ((config.current.get("native_agent") or {}).get("sandbox")) or {}
    except Exception:  # noqa: BLE001
        return {}


def backend() -> str:
    name = str(_config().get("backend") or "local").lower()
    if name not in BACKENDS:
        raise ToolError(f"sandbox.backend must be one of {', '.join(BACKENDS)}, not {name!r}")
    return name


def _injected(cfg: dict, environ: dict) -> dict[str, str]:
    out: dict[str, str] = {}
    for name, value in (cfg.get("env") or {}).get("set", {}).items():
        value = str(value)
        m = _INJECT.match(value)
        if m:
            source = environ.get(m.group(1))
            if source is None:
                raise ToolError(f"sandbox.env.set.{name} refers to {m.group(1)}, which is not set on the server")
            value = source
        out[str(name)] = value
    return out


def build_env(environ: Optional[dict] = None, cfg: Optional[dict] = None) -> dict[str, str]:
    """The environment a command runs with."""
    environ = dict(os.environ if environ is None else environ)
    cfg = _config() if cfg is None else cfg
    env_cfg = cfg.get("env") or {}
    mode = str(env_cfg.get("mode") or "secrets").lower()
    if mode not in ENV_MODES:
        raise ToolError(f"sandbox.env.mode must be one of {', '.join(ENV_MODES)}, not {mode!r}")
    allow = {str(n) for n in (env_cfg.get("allow") or [])}
    if mode == "inherit":
        env = environ
    elif mode == "minimal":
        keep = {n.upper() for n in _MINIMAL} | {n.upper() for n in allow}
        env = {k: v for k, v in environ.items() if k.upper() in keep}
    else:
        env = {k: v for k, v in environ.items() if k in allow or not secrets_guard.is_secret_name(k)}
    injected = _injected(cfg, environ)
    for name, value in injected.items():
        secrets_guard.register(name, value)
    env.update(injected)
    return env


def _docker_argv(command: str, cwd: Path, workspace: Path, name: str, cfg: dict, env_names: list[str]) -> list[str]:
    d = cfg.get("docker") or {}
    network = str(d.get("network") or "none").lower()
    if network not in _DOCKER_NETWORKS:
        raise ToolError(f"sandbox.docker.network must be one of {', '.join(_DOCKER_NETWORKS)} (host is not allowed)")
    workspace = Path(workspace).resolve()
    try:
        rel = Path(cwd).resolve().relative_to(workspace).as_posix()
    except ValueError:
        raise ToolError("the working folder is outside the workspace")
    argv = ["docker", "run", "--rm", "-i", "--name", name,
            "--network", network, "--memory", str(d.get("memory") or "1g"), "--cpus", str(d.get("cpus") or "2"),
            "--pids-limit", str(int(d.get("pids") or 256)), "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
            "-v", f"{workspace}:/workspace", "-w", "/workspace" if rel in ("", ".") else f"/workspace/{rel}"]
    user = str(d.get("user") or "")
    if not user and hasattr(os, "getuid"):
        user = f"{os.getuid()}:{os.getgid()}"
    if user:
        argv += ["--user", user]
    for n in env_names:
        argv += ["-e", n]              # the value is taken from the docker client's own environment, not the command line
    argv += [str(a) for a in (d.get("extra_args") or [])]
    argv += [str(d.get("image") or "python:3.11-slim"), "sh", "-c", command]
    return argv


async def start(command: str, cwd: Path, workspace: Path) -> asyncio.subprocess.Process:
    """Start a command in the configured sandbox; stdout and stderr arrive on one pipe."""
    cfg = _config()
    which = backend()
    pipes = dict(stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    spawn = {} if os.name == "nt" else {"start_new_session": True}
    try:
        if which == "local":
            return await asyncio.create_subprocess_shell(command, cwd=str(cwd), env=build_env(cfg=cfg), **pipes, **spawn)
        docker = shutil.which("docker")
        if not docker:
            raise ToolError("sandbox.backend is docker but the docker command was not found; the command was not run")
        client_env = build_env(cfg=cfg)                       # what the container is given, minus the host's secrets
        injected = list(_injected(cfg, dict(os.environ)))
        name = f"abp-{uuid.uuid4().hex[:12]}"
        argv = _docker_argv(command, cwd, workspace, name, cfg, injected)
        argv[0] = docker                                      # the resolved path: a bare name may not resolve on Windows
        proc = await asyncio.create_subprocess_exec(*argv, env=client_env, **pipes, **spawn)
        proc.abp_container = name                            # so kill() can remove the container, not just the client
        return proc
    except OSError as exc:
        raise ToolError(f"could not start the command: {exc}") from exc


def kill(proc) -> None:
    """Stop the command and everything it started; for docker, remove the container too."""
    name = getattr(proc, "abp_container", None)
    if name:
        try:
            subprocess.run([shutil.which("docker") or "docker", "rm", "-f", name], capture_output=True, timeout=30)
        except (OSError, subprocess.SubprocessError):
            pass
    pid = getattr(proc, "pid", None)
    if pid is None:
        return
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, timeout=15)
        else:
            import signal

            os.killpg(os.getpgid(pid), signal.SIGKILL)
    except (OSError, subprocess.SubprocessError):
        try:
            proc.kill()
        except (ProcessLookupError, OSError):
            pass
