"""Where run_shell commands run, and with what environment (roadmap P2).

    native_agent:
      sandbox:
        backend: local          # local | docker | ssh | wsl | windows_job
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
        ssh:
          host: ""              # required
          port: 22
          user: ""
          identity_file: ""
          remote_workspace_root: ""   # required; the command's cwd, translated onto the remote host
          connect_timeout: 10
          extra_args: []
        wsl:
          distro: ""            # "" = the default distro
          extra_args: []
        windows_job:
          memory_mb: 0          # 0 = unlimited
          active_process_limit: 0   # 0 = unlimited

**Environment.** A command used to inherit the server's whole environment, API keys
included: `env` or `echo $ANTHROPIC_API_KEY` would put them in the transcript. Now the
default `secrets` mode removes every variable whose name looks like a credential (see
secrets_guard.py). `minimal` keeps only a short list of harmless system variables plus
`allow`. `inherit` restores the old behaviour. `set` adds variables, and each value is
registered with secrets_guard so it is also redacted from any output.

**Backends.**
* `local` runs on the host as before (with the scrubbed environment). No filesystem or
  network confinement beyond the workspace guard on the file tools.
* `docker` runs each command in a fresh container: the workspace mounted at /workspace,
  no network by default, memory / cpu / process limits, all Linux capabilities dropped,
  no privilege escalation. Fails closed: if Docker is not installed or the daemon is not
  running the command is refused, never quietly run on the host instead.
* `ssh` runs the command on a configured remote host over an existing, already-trusted
  SSH connection (a host key already in `known_hosts`, key-based auth - this backend
  never prompts for or handles a password). Fails closed if `ssh` is missing, the host
  isn't configured, or the connection fails. **Honest limit:** only the *command* runs
  remotely - `read_file`/`write_file` and the other file tools still operate on the
  local workspace, so local and remote file state can only stay in sync if the caller
  keeps them in sync (for example, a `remote_workspace_root` that's already synced by
  some other means). This is the same class of gap the docker backend's bind mount
  avoids by construction; ssh does not have an equivalent here (that's roadmap P6's
  "cloud computer": the file tools would need to work remotely too).
* `wsl` runs the command inside a WSL2 distro on the same machine, via `wsl.exe`. The
  workspace path is translated onto the distro's default drive-automount path
  (`/mnt/<drive>/...`); this assumes the distro hasn't turned automount off. Fails
  closed if `wsl.exe` or the named distro isn't available.
* `windows_job` runs locally, like `local`, but binds the process to a real Win32 Job
  Object (`win_job.py`) with kill-on-close set, so the whole tree is guaranteed to die
  when the command is stopped - not a `taskkill /T /F` tree-walk, which can lose a race
  against a process that forks quickly or deliberately detaches. Windows-only; refused
  on any other OS. Like `local`, it does not confine the filesystem or the network.

Not built: network egress control for the local/windows_job backends (only the docker
backend can cut the network) - investigated for this round; a Windows Firewall rule
would need this process to run elevated (rejected: the rest of the app deliberately
never does, see firewall.py), and a real non-elevated per-process block needs an
AppContainer, which needs bypassing Python's subprocess/asyncio plumbing for process
creation - a larger, separate piece of work than this pass, tracked in
docs/agents/ROADMAP.md rather than attempted half-built here.
"""

from __future__ import annotations

import asyncio
import os
import re
import shlex
import shutil
import subprocess
import uuid
from pathlib import Path
from typing import Optional

from bot.agent_runtime import secrets_guard, win_job
from bot.agent_runtime.errors import ToolError

BACKENDS = ("local", "docker", "ssh", "wsl", "windows_job")
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
    if name == "windows_job" and not win_job.is_supported():
        raise ToolError("sandbox.backend is windows_job, which only works on Windows")
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


def _remote_dir(cwd: Path, workspace: Path, root: str) -> str:
    """The remote-side (ssh/wsl) equivalent of `cwd`, given the remote's root for the
    workspace. Refuses a `cwd` outside the workspace, same as the docker backend does."""
    workspace = Path(workspace).resolve()
    try:
        rel = Path(cwd).resolve().relative_to(workspace).as_posix()
    except ValueError:
        raise ToolError("the working folder is outside the workspace")
    root = root.rstrip("/") or "."
    return root if rel in ("", ".") else f"{root}/{rel}"


def _ssh_argv(command: str, cwd: Path, workspace: Path, cfg: dict, pidfile: str) -> list[str]:
    s = cfg.get("ssh") or {}
    host = str(s.get("host") or "")
    if not host:
        raise ToolError("sandbox.backend is ssh but sandbox.ssh.host is not set; the command was not run")
    root = str(s.get("remote_workspace_root") or "")
    if not root:
        raise ToolError("sandbox.backend is ssh but sandbox.ssh.remote_workspace_root is not set; the command was not run")
    remote_dir = _remote_dir(cwd, workspace, root)
    remote_cmd = f"cd {shlex.quote(remote_dir)} && echo $$ > {shlex.quote(pidfile)} && {command}"
    user = str(s.get("user") or "")
    target = f"{user}@{host}" if user else host
    argv = ["ssh", "-o", "BatchMode=yes", "-o", f"ConnectTimeout={int(s.get('connect_timeout') or 10)}",
            "-p", str(int(s.get("port") or 22))]
    identity = str(s.get("identity_file") or "")
    if identity:
        argv += ["-i", identity]
    argv += [str(a) for a in (s.get("extra_args") or [])]
    argv += [target, remote_cmd]
    return argv


def _win_to_wsl_path(p: Path) -> str:
    p = Path(p).resolve()
    drive = p.drive.rstrip(":").lower()
    rest = p.as_posix()[len(p.drive):].lstrip("/")
    return f"/mnt/{drive}/{rest}" if rest else f"/mnt/{drive}"


def _wsl_argv(command: str, cwd: Path, workspace: Path, cfg: dict, pidfile: str) -> list[str]:
    w = cfg.get("wsl") or {}
    remote_dir = _win_to_wsl_path(cwd)
    remote_cmd = f"cd {shlex.quote(remote_dir)} && echo $$ > {shlex.quote(pidfile)} && {command}"
    argv = ["wsl.exe"]
    distro = str(w.get("distro") or "")
    if distro:
        argv += ["-d", distro]
    argv += ["--"] + [str(a) for a in (w.get("extra_args") or [])] + ["sh", "-c", remote_cmd]
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

        if which == "windows_job":
            proc = await asyncio.create_subprocess_shell(command, cwd=str(cwd), env=build_env(cfg=cfg), **pipes)
            j = cfg.get("windows_job") or {}
            try:
                job = win_job.create(memory_mb=int(j.get("memory_mb") or 0),
                                      active_process_limit=int(j.get("active_process_limit") or 0))
                win_job.assign(job, proc.pid)
            except OSError as exc:
                try:
                    proc.kill()
                except (ProcessLookupError, OSError):
                    pass
                raise ToolError(f"could not confine the command to a job object: {exc}") from exc
            proc.abp_job = job
            return proc

        if which == "docker":
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

        if which == "ssh":
            ssh = shutil.which("ssh")
            if not ssh:
                raise ToolError("sandbox.backend is ssh but the ssh command was not found; the command was not run")
            root = str((cfg.get("ssh") or {}).get("remote_workspace_root") or "")
            name = f"abp-{uuid.uuid4().hex[:12]}"
            pidfile = f"{root.rstrip('/')}/.{name}.pid" if root else f"/tmp/.{name}.pid"
            argv = _ssh_argv(command, cwd, workspace, cfg, pidfile)
            argv[0] = ssh
            proc = await asyncio.create_subprocess_exec(*argv, env=build_env(cfg=cfg), **pipes, **spawn)
            proc.abp_remote_kill = _ssh_kill_argv(argv, pidfile)
            return proc

        if which == "wsl":
            wsl = shutil.which("wsl.exe") or shutil.which("wsl")
            if not wsl:
                raise ToolError("sandbox.backend is wsl but wsl.exe was not found; the command was not run")
            name = f"abp-{uuid.uuid4().hex[:12]}"
            remote_dir = _win_to_wsl_path(cwd)
            pidfile = f"{remote_dir}/.{name}.pid"
            argv = _wsl_argv(command, cwd, workspace, cfg, pidfile)
            argv[0] = wsl
            proc = await asyncio.create_subprocess_exec(*argv, env=build_env(cfg=cfg), **pipes)
            proc.abp_remote_kill = _wsl_kill_argv(argv, pidfile)
            return proc

        raise ToolError(f"sandbox.backend {which!r} is not implemented")
    except OSError as exc:
        raise ToolError(f"could not start the command: {exc}") from exc


def _ssh_kill_argv(start_argv: list[str], pidfile: str) -> list[str]:
    # start_argv is [ssh, ...opts..., target, remote_cmd] - reuse everything up to (not
    # including) the remote command, then swap in a best-effort remote kill.
    target = start_argv[-2]
    kill_cmd = f"kill -9 -$(cat {shlex.quote(pidfile)}) 2>/dev/null; kill -9 $(cat {shlex.quote(pidfile)}) 2>/dev/null; " \
               f"rm -f {shlex.quote(pidfile)}"
    return start_argv[:-2] + [target, kill_cmd]


def _wsl_kill_argv(start_argv: list[str], pidfile: str) -> list[str]:
    # start_argv is [wsl.exe, [-d, distro,] --, ...extra_args, sh, -c, remote_cmd]
    kill_cmd = f"kill -9 -$(cat {shlex.quote(pidfile)}) 2>/dev/null; kill -9 $(cat {shlex.quote(pidfile)}) 2>/dev/null; " \
               f"rm -f {shlex.quote(pidfile)}"
    return start_argv[:-1] + [kill_cmd]


def kill(proc) -> None:
    """Stop the command and everything it started; for docker, remove the container
    too; for ssh/wsl, best-effort kill the remote side too; for windows_job, terminate
    the job (which is by itself enough to guarantee the whole tree is gone)."""
    name = getattr(proc, "abp_container", None)
    if name:
        try:
            subprocess.run([shutil.which("docker") or "docker", "rm", "-f", name], capture_output=True, timeout=30)
        except (OSError, subprocess.SubprocessError):
            pass
    remote_kill = getattr(proc, "abp_remote_kill", None)
    if remote_kill:
        try:
            subprocess.run(remote_kill, capture_output=True, timeout=15)
        except (OSError, subprocess.SubprocessError):
            pass
    job = getattr(proc, "abp_job", None)
    if job:
        win_job.terminate(job)
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
