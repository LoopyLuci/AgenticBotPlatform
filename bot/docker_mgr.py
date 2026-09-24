"""Portainer-style container management over the local `docker` CLI: containers,
images, volumes, networks, Compose stacks, registries, system/prune, events,
stats, logs and non-interactive exec.

Safety: argv lists only (never a shell); every name/id/ref is validated and
may not start with "-"; a stuck daemon can't hang callers (hard timeouts, and
the whole process tree is killed on timeout); registry passwords go over
stdin, never argv, and are never returned or logged.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Optional

import psutil

_NO_WINDOW = 0x08000000 if os.name == "nt" else 0
DEFAULT_TIMEOUT = 30.0
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]{0,127}$")
_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-/:@]{0,254}$")
_PORT_RE = re.compile(r"^(\d{1,5}:)?\d{1,5}(/(tcp|udp))?$|^\d{1,3}(\.\d{1,3}){3}:\d{1,5}:\d{1,5}(/(tcp|udp))?$")
_ENV_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=[^\x00]*$")
_STACK_RE = re.compile(r"^[a-z0-9][a-z0-9_\-]{0,62}$")
_SINCE_RE = re.compile(r"^\d+[smhd]?$|^\d{4}-\d{2}-\d{2}(T[\d:.Z+\-]+)?$")


class DockerError(Exception):
    pass


def is_installed() -> bool:
    return bool(shutil.which("docker"))


def _name(value: Any, what: str = "name") -> str:
    s = str(value).strip()
    if not _NAME_RE.match(s):
        raise DockerError(f"invalid {what}: {s!r}")
    return s


def _ref(value: Any, what: str = "reference") -> str:
    s = str(value).strip()
    if not _REF_RE.match(s):
        raise DockerError(f"invalid {what}: {s!r}")
    return s


def _run(args: list[str], timeout: float = DEFAULT_TIMEOUT, stdin: Optional[str] = None,
         cwd: Optional[str] = None) -> tuple[bool, str]:
    if not is_installed():
        return False, "Docker is not installed on this machine"
    try:
        proc = subprocess.Popen(
            ["docker", *args], stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, cwd=cwd,
            creationflags=_NO_WINDOW,
        )
    except OSError as exc:
        return False, str(exc)
    try:
        out = proc.communicate(input=stdin, timeout=timeout)[0] or ""
    except subprocess.TimeoutExpired:
        try:
            parent = psutil.Process(proc.pid)
            for p in parent.children(recursive=True) + [parent]:
                p.kill()
        except psutil.Error:
            proc.kill()
        return False, f"docker {args[0]} timed out after {timeout:.0f}s - the Docker daemon may be stopped or unresponsive"
    return proc.returncode == 0, out.strip()


def _need(args: list[str], **kw) -> str:
    ok, out = _run(args, **kw)
    if not ok:
        raise DockerError(out)
    return out


def _lines_json(args: list[str], **kw) -> list[dict]:
    out = _need(args, **kw)
    rows = []
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def _inspect(kind: str, ident: str) -> Any:
    data = json.loads(_need([kind, "inspect", ident]) or "[]")
    if not data:
        raise DockerError(f"no such {kind}: {ident}")
    return data[0]


def _done(out: str) -> dict:
    return {"ok": True, "output": out}


# ------------------------------------------------------------------- system
def info() -> dict:
    """Never raises: an unreachable daemon is a normal, reportable state."""
    if not is_installed():
        return {"installed": False, "running": False}
    ok, out = _run(["info", "--format", "{{json .}}"], timeout=12)
    if not ok:
        return {"installed": True, "running": False, "error": out}
    data = json.loads(out)
    return {"installed": True, "running": True, "server_version": data.get("ServerVersion"),
            "os": data.get("OperatingSystem"), "containers": data.get("Containers"),
            "running_containers": data.get("ContainersRunning"), "images": data.get("Images"),
            "cpus": data.get("NCPU"), "memory": data.get("MemTotal"), "raw": data}


def disk_usage() -> list[dict]:
    return _lines_json(["system", "df", "--format", "{{json .}}"])


def prune(kind: str, all_: bool = False, volumes: bool = False) -> dict:
    if kind not in ("container", "image", "volume", "network", "builder", "system"):
        raise DockerError("kind must be container, image, volume, network, builder or system")
    args = [kind, "prune", "-f"]
    if all_ and kind in ("image", "system", "builder"):
        args.append("-a")
    if volumes and kind == "system":
        args.append("--volumes")
    return _done(_need(args, timeout=300))


def events(since: str = "10m", limit: int = 200) -> list[dict]:
    if not _SINCE_RE.match(since):
        raise DockerError("since must look like 10m, 2h, or a timestamp")
    rows = _lines_json(["events", "--since", since, "--until", "0s", "--format", "{{json .}}"], timeout=20)
    return rows[-max(1, min(int(limit), 1000)):]


# --------------------------------------------------------------- containers
def containers(all_: bool = True) -> list[dict]:
    return _lines_json(["ps", *(["-a"] if all_ else []), "--no-trunc", "--format", "{{json .}}"])


def container(ident: str) -> Any:
    return _inspect("container", _name(ident, "container"))


def container_action(ident: str, action: str) -> dict:
    actions = {"start", "stop", "restart", "pause", "unpause", "kill", "rm", "remove"}
    if action not in actions:
        raise DockerError(f"action must be one of {sorted(actions)}")
    cid = _name(ident, "container")
    args = ["rm", "-f", cid] if action in ("rm", "remove") else [action, cid]
    return _done(_need(args, timeout=120))


def container_rename(ident: str, new: str) -> dict:
    return _done(_need(["rename", _name(ident, "container"), _name(new)]))


def container_update(ident: str, *, restart: Optional[str] = None, memory: Optional[str] = None,
                     cpus: Optional[str] = None) -> dict:
    args = ["update"]
    if restart:
        if not re.match(r"^(no|always|unless-stopped|on-failure(:\d+)?)$", restart):
            raise DockerError("bad restart policy")
        args += ["--restart", restart]
    if memory:
        if not re.match(r"^\d+[bkmg]?$", memory.lower()):
            raise DockerError("bad memory limit")
        args += ["--memory", memory]
    if cpus:
        if not re.match(r"^\d+(\.\d+)?$", str(cpus)):
            raise DockerError("bad cpu limit")
        args += ["--cpus", str(cpus)]
    if len(args) == 1:
        raise DockerError("nothing to update")
    return _done(_need([*args, _name(ident, "container")]))


def container_logs(ident: str, tail: int = 200, since: Optional[str] = None, timestamps: bool = False) -> dict:
    args = ["logs", "--tail", str(max(1, min(int(tail), 5000)))]
    if since:
        if not _SINCE_RE.match(since):
            raise DockerError("bad since")
        args += ["--since", since]
    if timestamps:
        args.append("-t")
    return {"logs": _need([*args, _name(ident, "container")], timeout=30)}


def container_stats(ident: Optional[str] = None) -> list[dict]:
    args = ["stats", "--no-stream", "--format", "{{json .}}"]
    if ident:
        args.append(_name(ident, "container"))
    return _lines_json(args, timeout=30)


def container_top(ident: str) -> dict:
    return {"processes": _need(["top", _name(ident, "container")])}


def container_exec(ident: str, command: list[str], *, user: Optional[str] = None, workdir: Optional[str] = None,
                   timeout: float = 60.0) -> dict:
    """Run one non-interactive command (argv list, no shell) inside a container."""
    if not command or not all(isinstance(c, str) for c in command):
        raise DockerError("command must be a non-empty list of strings")
    args = ["exec"]
    if user:
        args += ["-u", _name(user, "user")]
    if workdir:
        if not workdir.startswith("/") or "\x00" in workdir:
            raise DockerError("workdir must be an absolute path")
        args += ["-w", workdir]
    ok, out = _run([*args, _name(ident, "container"), *command], timeout=min(timeout, 300))
    return {"ok": ok, "output": out}


def container_create(*, image: str, name: Optional[str] = None, command: Optional[list[str]] = None,
                     ports: Optional[list[str]] = None, env: Optional[list[str]] = None,
                     volumes: Optional[list[str]] = None, network: Optional[str] = None,
                     restart: str = "no", labels: Optional[dict] = None, privileged: bool = False,
                     memory: Optional[str] = None, cpus: Optional[str] = None, hostname: Optional[str] = None,
                     user: Optional[str] = None, workdir: Optional[str] = None,
                     cap_add: Optional[list[str]] = None, devices: Optional[list[str]] = None,
                     start: bool = True, pull: bool = False) -> dict:
    """Deploy a container (Portainer's "Add container")."""
    args = ["run" if start else "create"]
    if start:
        args.append("-d")
    if name:
        args += ["--name", _name(name)]
    for p in ports or []:
        if not _PORT_RE.match(p):
            raise DockerError(f"bad port mapping {p!r} (use [host:]container[/proto])")
        args += ["-p", p]
    for e in env or []:
        if not _ENV_RE.match(e):
            raise DockerError(f"bad env var {e!r} (use KEY=value)")
        args += ["-e", e]
    for v in volumes or []:
        if not re.match(r"^[^\x00\-][^\x00]*:[^\x00]+$", v):
            raise DockerError(f"bad volume {v!r} (use source:target[:ro])")
        args += ["-v", v]
    if network:
        args += ["--network", _name(network, "network")]
    if not re.match(r"^(no|always|unless-stopped|on-failure(:\d+)?)$", restart):
        raise DockerError("bad restart policy")
    args += ["--restart", restart]
    for k, v in (labels or {}).items():
        if not re.match(r"^[A-Za-z0-9_.\-/]{1,100}$", str(k)) or "\x00" in str(v):
            raise DockerError(f"bad label {k!r}")
        args += ["--label", f"{k}={v}"]
    if privileged:
        args.append("--privileged")
    if memory:
        if not re.match(r"^\d+[bkmg]?$", memory.lower()):
            raise DockerError("bad memory limit")
        args += ["--memory", memory]
    if cpus:
        if not re.match(r"^\d+(\.\d+)?$", str(cpus)):
            raise DockerError("bad cpu limit")
        args += ["--cpus", str(cpus)]
    if hostname:
        args += ["--hostname", _name(hostname, "hostname")]
    if user:
        args += ["--user", _ref(user, "user")]
    if workdir:
        if not workdir.startswith("/"):
            raise DockerError("workdir must be an absolute path")
        args += ["--workdir", workdir]
    for c in cap_add or []:
        if not re.match(r"^[A-Z_]{2,40}$", c):
            raise DockerError(f"bad capability {c!r}")
        args += ["--cap-add", c]
    for d in devices or []:
        if not re.match(r"^/dev/[A-Za-z0-9_/.\-]+(:[A-Za-z0-9_/.\-]+)?(:[rwm]{1,3})?$", d):
            raise DockerError(f"bad device {d!r}")
        args += ["--device", d]
    if pull:
        args += ["--pull", "always"]
    args.append(_ref(image, "image"))
    if command:
        args += [str(c) for c in command]
    return {"ok": True, "id": _need(args, timeout=600).splitlines()[-1]}


def container_files(ident: str, path: str = "/") -> dict:
    """List a directory inside a container (for a file-browser view)."""
    if not path.startswith("/") or "\x00" in path:
        raise DockerError("path must be absolute")
    return container_exec(ident, ["ls", "-la", "--", path], timeout=20)


def container_copy(ident: str, container_path: str, host_dir: str, *, to_container: bool = False) -> dict:
    if not container_path.startswith("/"):
        raise DockerError("container path must be absolute")
    if not Path(host_dir).exists():
        raise DockerError("host path does not exist")
    cid = _name(ident, "container")
    pair = [host_dir, f"{cid}:{container_path}"] if to_container else [f"{cid}:{container_path}", host_dir]
    return _done(_need(["cp", *pair], timeout=300))


def container_commit(ident: str, repository: str) -> dict:
    return _done(_need(["commit", _name(ident, "container"), _ref(repository, "repository")]))


# ------------------------------------------------------------------- images
def images() -> list[dict]:
    return _lines_json(["images", "--no-trunc", "--format", "{{json .}}"])


def image(ref: str) -> Any:
    return _inspect("image", _ref(ref, "image"))


def image_history(ref: str) -> list[dict]:
    return _lines_json(["history", "--no-trunc", "--format", "{{json .}}", _ref(ref, "image")])


def image_pull(ref: str) -> dict:
    return _done(_need(["pull", _ref(ref, "image")], timeout=1800))


def image_remove(ref: str, force: bool = False) -> dict:
    return _done(_need(["rmi", *(["-f"] if force else []), _ref(ref, "image")], timeout=120))


def image_tag(src: str, dest: str) -> dict:
    return _done(_need(["tag", _ref(src, "image"), _ref(dest, "tag")]))


def image_push(ref: str) -> dict:
    return _done(_need(["push", _ref(ref, "image")], timeout=1800))


def image_build(context_dir: str, tag: str, dockerfile: Optional[str] = None,
                build_args: Optional[dict] = None, no_cache: bool = False) -> dict:
    if not Path(context_dir).is_dir():
        raise DockerError("build context must be an existing directory")
    args = ["build", "-t", _ref(tag, "tag")]
    if dockerfile:
        df = Path(dockerfile)
        if not df.is_absolute():
            df = Path(context_dir) / df
        if not df.is_file():
            raise DockerError("Dockerfile not found")
        args += ["-f", str(df)]
    for k, v in (build_args or {}).items():
        if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", str(k)):
            raise DockerError(f"bad build arg {k!r}")
        args += ["--build-arg", f"{k}={v}"]
    if no_cache:
        args.append("--no-cache")
    return _done(_need([*args, context_dir], timeout=3600))


def image_search(term: str, limit: int = 25) -> list[dict]:
    return _lines_json(["search", "--limit", str(max(1, min(int(limit), 100))), "--format", "{{json .}}",
                        _ref(term, "search term")], timeout=30)


# ------------------------------------------------------------------ volumes
def volumes() -> list[dict]:
    return _lines_json(["volume", "ls", "--format", "{{json .}}"])


def volume(name: str) -> Any:
    return _inspect("volume", _name(name, "volume"))


def volume_create(name: str, driver: str = "local", labels: Optional[dict] = None) -> dict:
    args = ["volume", "create", "--driver", _name(driver, "driver")]
    for k, v in (labels or {}).items():
        args += ["--label", f"{_name(k, 'label')}={v}"]
    return _done(_need([*args, _name(name, "volume")]))


def volume_remove(name: str, force: bool = False) -> dict:
    return _done(_need(["volume", "rm", *(["-f"] if force else []), _name(name, "volume")]))


# ----------------------------------------------------------------- networks
def networks() -> list[dict]:
    return _lines_json(["network", "ls", "--no-trunc", "--format", "{{json .}}"])


def network(name: str) -> Any:
    return _inspect("network", _name(name, "network"))


def network_create(name: str, driver: str = "bridge", subnet: Optional[str] = None,
                   gateway: Optional[str] = None, internal: bool = False) -> dict:
    import ipaddress
    args = ["network", "create", "--driver", _name(driver, "driver")]
    if subnet:
        args += ["--subnet", str(ipaddress.ip_network(subnet, strict=False))]
    if gateway:
        args += ["--gateway", str(ipaddress.ip_address(gateway))]
    if internal:
        args.append("--internal")
    return _done(_need([*args, _name(name, "network")]))


def network_remove(name: str) -> dict:
    return _done(_need(["network", "rm", _name(name, "network")]))


def network_connect(network_name: str, ident: str, connect: bool = True) -> dict:
    return _done(_need(["network", "connect" if connect else "disconnect", _name(network_name, "network"),
                        _name(ident, "container")]))


# ------------------------------------------------------------ compose stacks
def _stack_dir(name: str) -> Path:
    from bot import envfile
    return Path(envfile.PROJECT_ROOT) / "data" / "stacks" / _stack_name(name)


def _stack_name(name: str) -> str:
    if not _STACK_RE.match(name):
        raise DockerError("stack names use lowercase letters, digits, - and _")
    return name


def stacks() -> list[dict]:
    return json.loads(_need(["compose", "ls", "-a", "--format", "json"]) or "[]")


def stack_deploy(name: str, compose_yaml: str, env: Optional[dict] = None) -> dict:
    """Store the compose file under data/stacks/<name>/ (so it can be edited and
    redeployed later, like Portainer) and `up -d` it."""
    n = _stack_name(name)
    if "services" not in compose_yaml:
        raise DockerError("compose file has no 'services' section")
    d = _stack_dir(n)
    d.mkdir(parents=True, exist_ok=True)
    (d / "compose.yaml").write_text(compose_yaml, encoding="utf-8")
    if env is not None:
        lines = []
        for k, v in env.items():
            if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", str(k)) or "\n" in str(v):
                raise DockerError(f"bad env entry {k!r}")
            lines.append(f"{k}={v}")
        (d / ".env").write_text("\n".join(lines) + "\n", encoding="utf-8")
    ok, out = _run(["compose", "-p", n, "-f", "compose.yaml", "up", "-d", "--remove-orphans"], timeout=1800, cwd=str(d))
    return {"ok": ok, "output": out}


def stack_get(name: str) -> dict:
    d = _stack_dir(name)
    f = d / "compose.yaml"
    if not f.is_file():
        raise DockerError("no stored compose file for that stack (it wasn't deployed from ABP)")
    env = (d / ".env")
    return {"name": name, "compose": f.read_text(encoding="utf-8"),
            "env_keys": [ln.split("=", 1)[0] for ln in env.read_text().splitlines() if "=" in ln] if env.exists() else []}


def stack_action(name: str, action: str) -> dict:
    n = _stack_name(name)
    table = {"start": ["start"], "stop": ["stop"], "restart": ["restart"], "pull": ["pull"],
             "down": ["down"], "down-volumes": ["down", "-v"], "up": ["up", "-d", "--remove-orphans"]}
    if action not in table:
        raise DockerError(f"action must be one of {sorted(table)}")
    d = _stack_dir(n)
    args = ["compose", "-p", n] + (["-f", "compose.yaml"] if (d / "compose.yaml").is_file() else []) + table[action]
    ok, out = _run(args, timeout=1800, cwd=str(d) if d.is_dir() else None)
    return {"ok": ok, "output": out}


def stack_services(name: str) -> list[dict]:
    return _lines_json(["compose", "-p", _stack_name(name), "ps", "-a", "--format", "json"])


def stack_logs(name: str, tail: int = 200) -> dict:
    return {"logs": _need(["compose", "-p", _stack_name(name), "logs", "--no-color", "--tail",
                           str(max(1, min(int(tail), 5000)))])}


# --------------------------------------------------------------- registries
def registry_login(server: str, username: str, password: str) -> dict:
    """The password goes over stdin, not argv, and is never echoed."""
    if not re.match(r"^[A-Za-z0-9.\-:/]{1,200}$", server) or server.startswith("-"):
        raise DockerError("bad registry address")
    ok, out = _run(["login", "--username", _ref(username, "username"), "--password-stdin", server],
                   stdin=password, timeout=60)
    return {"ok": ok, "output": "" if ok and "Succeeded" not in out else out.replace(password, "***")}


def registry_logout(server: str) -> dict:
    if not re.match(r"^[A-Za-z0-9.\-:/]{1,200}$", server) or server.startswith("-"):
        raise DockerError("bad registry address")
    return _done(_need(["logout", server]))


def registries() -> list[str]:
    """Registries with stored credentials (names only - never the secrets)."""
    cfg = Path(os.path.expanduser("~")) / ".docker" / "config.json"
    try:
        data = json.loads(cfg.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return sorted((data.get("auths") or {}).keys())


# ---------------------------------------------------------------- app templates
TEMPLATES = [
    {"id": "nginx", "title": "Nginx", "image": "nginx:stable", "ports": ["8080:80"]},
    {"id": "postgres", "title": "PostgreSQL", "image": "postgres:16", "ports": ["5432:5432"],
     "env": ["POSTGRES_PASSWORD=change-me"], "volumes": ["pgdata:/var/lib/postgresql/data"]},
    {"id": "redis", "title": "Redis", "image": "redis:7", "ports": ["6379:6379"]},
    {"id": "mariadb", "title": "MariaDB", "image": "mariadb:11", "ports": ["3306:3306"],
     "env": ["MARIADB_ROOT_PASSWORD=change-me"], "volumes": ["mariadb:/var/lib/mysql"]},
    {"id": "mongo", "title": "MongoDB", "image": "mongo:7", "ports": ["27017:27017"], "volumes": ["mongo:/data/db"]},
    {"id": "uptime-kuma", "title": "Uptime Kuma", "image": "louislam/uptime-kuma:1", "ports": ["3001:3001"],
     "volumes": ["uptime-kuma:/app/data"]},
    {"id": "adminer", "title": "Adminer", "image": "adminer", "ports": ["8081:8080"]},
    {"id": "ollama", "title": "Ollama", "image": "ollama/ollama", "ports": ["11434:11434"],
     "volumes": ["ollama:/root/.ollama"]},
]


def templates() -> list[dict]:
    return TEMPLATES


def deploy_template(template_id: str, name: Optional[str] = None, overrides: Optional[dict] = None) -> dict:
    t = next((x for x in TEMPLATES if x["id"] == template_id), None)
    if not t:
        raise DockerError(f"no such template: {template_id}")
    spec = {k: v for k, v in t.items() if k not in ("id", "title")}
    spec.update(overrides or {})
    return container_create(name=name or template_id, restart="unless-stopped", **spec)
