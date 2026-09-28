"""Installing, updating, starting and checking Hermes Manager from ABP.

Hermes Manager stays its own program: its own checkout, its Node dependencies and build, its Electron window. Its
bridge runs under Hermes's own Python (the Hermes venv has every package it needs), found the way the app finds it.

Where the checkout is, first match wins: $ABP_HERMES_MANAGER_DIR, hermes_manager.path in config/backends.yaml, a
Hermes-Manager folder next to ABP's (a developer's working copy), data/modules/Hermes-Manager (cloned by setup).
"""
from __future__ import annotations

import os
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Optional

from bot.hermes_manager import client
from bot.hermes_manager.client import ManagerError

REPO_URL = "https://github.com/LoopyLuci/Hermes-Manager.git"
NO_WINDOW = 0x08000000 if os.name == "nt" else 0


def _cfg() -> dict:
    try:
        from bot.config import config
        return dict((config.current or {}).get("hermes_manager") or {})
    except Exception:  # noqa: BLE001
        return {}


def _abp_root() -> Path:
    from bot.envfile import PROJECT_ROOT
    return Path(PROJECT_ROOT)


def _is_checkout(p: Path) -> bool:
    return (p / "src" / "bridge" / "hermes_manager_bridge" / "__main__.py").is_file()


def install_dir() -> Path:
    for candidate in (os.environ.get("ABP_HERMES_MANAGER_DIR"), _cfg().get("path")):
        if candidate:
            return Path(candidate).expanduser()
    sibling = _abp_root().parent / "Hermes-Manager"
    if _is_checkout(sibling):
        return sibling
    return _abp_root() / "data" / "modules" / "Hermes-Manager"


def hermes_home() -> Optional[Path]:
    env = os.environ.get("HERMES_HOME", "").strip()
    for p in ([Path(env)] if env else []) + [Path(os.environ.get(v, "")) / "hermes" for v in ("LOCALAPPDATA", "APPDATA")
                                             if os.environ.get(v)]:
        if (p / "hermes-agent").is_dir() or (p / "logs").is_dir():
            return p
    return None


def hermes_python() -> Optional[Path]:
    """Hermes's venv Python (preferred: it has the bridge's dependencies), else its store Python."""
    home = hermes_home()
    if home is None:
        return None
    for py in sorted((home / "installs").glob("*/environments/*/venv/Scripts/python.exe")) + \
            sorted((home / "installs").glob("*/environments/*/venv/bin/python")):
        return py
    for py in sorted((home / "tools").glob("python-*/python.exe")):
        return py
    return None


def _run(args: list, cwd: Optional[Path] = None, timeout: float = 1800) -> subprocess.CompletedProcess:
    return subprocess.run([str(a) for a in args], cwd=str(cwd) if cwd else None, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=timeout, creationflags=NO_WINDOW,
                          stdin=subprocess.DEVNULL, shell=False)


def _git(d: Path, *args: str, timeout: float = 120) -> str:
    r = _run(["git", "-C", str(d), *args], timeout=timeout)
    if r.returncode != 0:
        raise ManagerError((r.stderr or r.stdout).strip()[-600:] or f"git {' '.join(args)} failed")
    return r.stdout.strip()


def _npm() -> str:
    import shutil
    npm = shutil.which("npm") or shutil.which("npm.cmd")
    if not npm:
        raise ManagerError("npm is not installed (Node.js 22+ is needed to build Hermes Manager)", code="not_installed")
    return npm


def install_info(*, fetch: bool = False) -> dict:
    d = install_dir()
    info: dict[str, Any] = {
        "path": str(d), "installed": _is_checkout(d), "developer_checkout": d.parent == _abp_root().parent,
        "built": (d / "out" / "main" / "index.js").is_file(),
        "electron": (d / "node_modules" / "electron" / "dist").is_dir(),
        "hermes_home": str(hermes_home() or ""), "hermes_python": str(hermes_python() or ""),
    }
    if not info["installed"] or not (d / ".git").exists():
        return info
    try:
        info["commit"] = _git(d, "rev-parse", "--short", "HEAD")
        info["subject"] = _git(d, "log", "-1", "--format=%s")
        info["branch"] = _git(d, "rev-parse", "--abbrev-ref", "HEAD")
        info["changed_files"] = len([x for x in _git(d, "status", "--porcelain").splitlines() if x.strip()])
        if fetch:
            _git(d, "fetch", "--quiet", "origin", timeout=60)
        up = _git(d, "rev-parse", "--abbrev-ref", "@{upstream}")
        ahead, behind = _git(d, "rev-list", "--left-right", "--count", f"HEAD...{up}").split()
        info.update(upstream=up, ahead=int(ahead), behind=int(behind))
    except ManagerError as e:
        info["git_error"] = str(e)
    return info


_jobs: dict[str, dict] = {}


def jobs() -> list[dict]:
    return sorted((dict(j) for j in _jobs.values()), key=lambda j: j["started"], reverse=True)[:20]


def _start_job(kind: str, fn: Callable[[Callable[[str], None]], Any]) -> dict:
    if any(j["state"] == "running" for j in _jobs.values()):
        raise ManagerError("an install or update is already running", code="busy")
    job = {"id": uuid.uuid4().hex[:10], "kind": kind, "state": "running", "log": [], "started": time.time()}
    _jobs[job["id"]] = job

    def log(line: str) -> None:
        job["log"] = (job["log"] + [line])[-200:]

    def go() -> None:
        try:
            job["result"] = fn(log)
            job["state"] = "done"
        except Exception as e:  # noqa: BLE001
            log(f"failed: {e}")
            job.update(state="failed", error=str(e)[:1000])
        job["finished"] = time.time()
    threading.Thread(target=go, name=f"hm-{kind}", daemon=True).start()
    return dict(job)


def _build(d: Path, log: Callable[[str], None]) -> None:
    npm = _npm()
    log("installing Node dependencies (npm install)")
    r = _run([npm, "install", "--no-audit", "--no-fund"], cwd=d, timeout=3600)
    if r.returncode != 0:
        raise ManagerError(r.stderr.strip()[-1200:] or "npm install failed")
    electron = d / "node_modules" / "electron"
    if (electron / "install.js").is_file() and not (electron / "dist").is_dir():
        log("downloading the Electron runtime")
        r = _run(["node", str(electron / "install.js")], cwd=d, timeout=1800)
        if r.returncode != 0:
            raise ManagerError(r.stderr.strip()[-800:] or "Electron download failed")
    log("building (npm run build)")
    r = _run([npm, "run", "build"], cwd=d, timeout=1800)
    if r.returncode != 0:
        raise ManagerError((r.stderr or r.stdout).strip()[-1200:] or "build failed")


def _setup(log: Callable[[str], None]) -> dict:
    d = install_dir()
    if not _is_checkout(d):
        if d.exists() and any(d.iterdir()):
            raise ManagerError(f"{d} exists but is not a Hermes Manager checkout")
        log(f"cloning {REPO_URL} into {d}")
        d.parent.mkdir(parents=True, exist_ok=True)
        r = _run(["git", "clone", "--depth", "50", REPO_URL, str(d)], timeout=1800)
        if r.returncode != 0:
            raise ManagerError(r.stderr.strip()[-600:] or "git clone failed")
    _build(d, log)
    if hermes_python() is None:
        log("note: no Hermes install was found, so the bridge has no Python to run on until Hermes is installed")
    log("installed")
    return install_info()


def setup() -> dict:
    """Clone Hermes Manager if it is not here, install its dependencies and build it (in the background)."""
    return _start_job("setup", _setup)


def _update(log: Callable[[str], None]) -> dict:
    d = install_dir()
    before = install_info(fetch=True)
    if before.get("changed_files"):
        raise ManagerError(f"{d} has {before['changed_files']} uncommitted change(s); commit or stash them first "
                           "(ABP never overwrites work in progress)")
    if not before.get("behind"):
        log("already up to date")
        return before
    b = client.find()
    ours = bool(b and b.owner == "abp")
    if ours:
        log("stopping the bridge ABP started")
        stop_bridge()
    log(f"pulling {before['behind']} new commit(s)")
    _git(d, "pull", "--ff-only", "--quiet", timeout=600)
    _build(d, log)
    if ours:
        start_bridge()
    return install_info()


def update() -> dict:
    """Pull the latest Hermes Manager and rebuild (in the background). Refuses while there is uncommitted work."""
    return _start_job("update", _update)


def start_bridge(wait_s: float = 45.0) -> dict:
    """Start a headless bridge (no window) that ABP, the MCP server and a later window all share."""
    found = client.find()
    if found:
        return {"running": True, "url": found.url, "pid": found.pid, "owner": found.owner, "already": True}
    d = install_dir()
    py = hermes_python()
    if not _is_checkout(d):
        raise ManagerError("Hermes Manager is not installed yet (setup installs it)", code="not_installed")
    if py is None:
        raise ManagerError("no Hermes install found: the bridge runs on Hermes's own Python", code="not_installed")
    import secrets
    home = hermes_home()
    paths = [str(d / "src" / "bridge")] + ([str(home / "hermes-agent")] if home and (home / "hermes-agent").is_dir() else [])
    env = {**os.environ, "HM_BRIDGE_TOKEN": secrets.token_hex(32), "PYTHONPATH": os.pathsep.join(paths)}
    client.hm_home().mkdir(parents=True, exist_ok=True)
    flags = (0x00000008 | 0x00000200 | NO_WINDOW) if os.name == "nt" else 0
    with open(client.hm_home() / "bridge.log", "ab") as out:
        subprocess.Popen([str(py), "-m", "hermes_manager_bridge", "--discovery", "--owner", "abp",
                          *(["--hermes-home", str(home)] if home else [])], cwd=str(d / "src" / "bridge"), env=env,
                         stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT, creationflags=flags,
                         start_new_session=os.name != "nt")
    deadline = time.time() + wait_s
    while time.time() < deadline:
        found = client.find()
        if found:
            return {"running": True, "url": found.url, "pid": found.pid, "owner": found.owner, "already": False}
        time.sleep(0.4)
    raise ManagerError(f"the bridge did not start within {wait_s:.0f}s (see {client.hm_home() / 'bridge.log'})", code="timeout")


def stop_bridge() -> dict:
    """Stop the bridge if ABP started it. A bridge the app started is the app's: close the app instead."""
    b = client.find()
    if b is None:
        return {"running": False}
    if b.owner not in ("abp", "mcp", "bridge"):
        raise ManagerError("that bridge belongs to the Hermes Manager window; close the window to stop it", code="not_ours")
    try:
        import psutil
        psutil.Process(b.pid).terminate()
    except Exception as e:  # noqa: BLE001
        raise ManagerError(f"could not stop the bridge: {e}") from None
    for _ in range(40):
        if client.find() is None:
            break
        time.sleep(0.25)
    return {"running": client.find() is not None}


def open_window(wait_s: float = 60.0) -> dict:
    client.require()
    return client.call("gui.launch", {"wait_s": wait_s})


def status() -> dict:
    out: dict[str, Any] = {"install": install_info(), "jobs": [j for j in jobs() if j["state"] == "running"]}
    b = client.find()
    out["bridge"] = {"running": b is not None, **({"url": b.url, "pid": b.pid, "owner": b.owner, "window": b.gui} if b else {})}
    if b:
        try:
            out["health"] = client.request("GET", "/api/v1/health", bridge=b, timeout=20)
            out["window"] = client.request("GET", "/api/v1/gui/status", bridge=b, timeout=10)
        except ManagerError as e:
            out["health_error"] = str(e)
    return out


MCP_NAME = "hermes-manager"


def register_mcp(compact: bool = True) -> dict:
    """Add Hermes Manager's own MCP server to ABP's external MCP servers."""
    import json as _json
    from bot.storage import extensions as ext
    py = hermes_python()
    if py is None:
        raise ManagerError("no Hermes install found: its MCP server runs on Hermes's own Python", code="not_installed")
    args = ["-m", "hermes_manager_bridge.mcp", *(["--tools", "compact"] if compact else [])]
    env = {"PYTHONPATH": str(install_dir() / "src" / "bridge")}
    if ext.get_external_mcp_server(MCP_NAME) is not None:
        ext.delete_external_mcp_server(MCP_NAME)
    ext.add_external_mcp_server(MCP_NAME, "stdio", command=str(py), args_json=_json.dumps(args), env_json=_json.dumps(env))
    return {"name": MCP_NAME, "command": str(py), "args": args, "env": env}
