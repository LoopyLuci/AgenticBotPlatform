"""Installing, updating, starting and checking VM-Harness from ABP.

VM-Harness stays its own program: its own git checkout, its own virtualenv, its own window. ABP finds a checkout (or
clones one), keeps it up to date from the repo, starts its hub and window, and reports on all of it.

Where the checkout is, first match wins:
    $ABP_VM_HARNESS_DIR
    vm_harness.path in config/backends.yaml
    a VM-Harness folder next to ABP's own (a developer's working copy, e.g. Z:/Projects/VM-Harness)
    data/modules/VM-Harness (cloned there by setup)
"""
from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Optional

from bot.vm_harness import client
from bot.vm_harness.client import HarnessError
from bot.sandbox_ns import spawn

REPO_URL = "https://github.com/LoopyLuci/VM-Harness.git"
NO_WINDOW = 0x08000000 if os.name == "nt" else 0
# A hidden console of its own (not DETACHED_PROCESS: a process with no console makes every console
# program it starts open a visible window), in its own process group.
DETACHED_HIDDEN_CONSOLE = (0x00000200 | NO_WINDOW) if os.name == "nt" else 0


def _abp_root() -> Path:
    from bot.envfile import PROJECT_ROOT
    return Path(PROJECT_ROOT)


def _is_checkout(p: Path) -> bool:
    return (p / "src" / "vm_harness" / "__main__.py").is_file()


def install_dir() -> Path:
    """Where VM-Harness is (or will be installed)."""
    cfg = client._cfg()
    for candidate in (os.environ.get("ABP_VM_HARNESS_DIR"), cfg.get("path")):
        if candidate:
            return Path(candidate).expanduser()
    root = _abp_root()
    for sibling in (root.parent / "VM-Harness", root.parent / "vm-harness"):
        if _is_checkout(sibling):
            return sibling
    return root / "data" / "modules" / "VM-Harness"


def python_exe(d: Optional[Path] = None, *, windowless: bool = False) -> Path:
    d = d or install_dir()
    if os.name == "nt":
        return d / ".venv" / "Scripts" / ("pythonw.exe" if windowless else "python.exe")
    return d / ".venv" / "bin" / "python"


def _run(args: list[str], cwd: Optional[Path] = None, timeout: float = 600) -> subprocess.CompletedProcess:
    return subprocess.run([str(a) for a in args], cwd=str(cwd) if cwd else None, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=timeout, creationflags=NO_WINDOW,
                          stdin=subprocess.DEVNULL)


def _git(d: Path, *args: str, timeout: float = 120) -> str:
    r = _run(["git", "-C", str(d), *args], timeout=timeout)
    if r.returncode != 0:
        raise HarnessError((r.stderr or r.stdout).strip()[-600:] or f"git {' '.join(args)} failed")
    return r.stdout.strip()


# ---- what is installed ---------------------------------------------------------------------------------------------
def install_info(*, fetch: bool = False) -> dict:
    d = install_dir()
    info: dict[str, Any] = {"path": str(d), "installed": _is_checkout(d), "venv": python_exe(d).is_file(),
                            "has_hub": (d / "src" / "vm_harness" / "control" / "hub.py").is_file(),
                            "developer_checkout": d.parent == _abp_root().parent}
    if not info["installed"] or not (d / ".git").exists():
        return info
    try:
        info["commit"] = _git(d, "rev-parse", "--short", "HEAD")
        info["branch"] = _git(d, "rev-parse", "--abbrev-ref", "HEAD")
        info["subject"] = _git(d, "log", "-1", "--format=%s")
        info["changed_files"] = len([l for l in _git(d, "status", "--porcelain").splitlines() if l.strip()])
        if fetch:
            _git(d, "fetch", "--quiet", "origin", timeout=60)
        upstream = _git(d, "rev-parse", "--abbrev-ref", "@{upstream}")
        counts = _git(d, "rev-list", "--left-right", "--count", f"HEAD...{upstream}").split()
        info.update(upstream=upstream, ahead=int(counts[0]), behind=int(counts[1]))
    except HarnessError as e:
        info["git_error"] = str(e)
    return info


# ---- background jobs (setup, update) ---------------------------------------------------------------------------------
_jobs: dict[str, dict] = {}


def jobs() -> list[dict]:
    return sorted((dict(j) for j in _jobs.values()), key=lambda j: j["started"], reverse=True)[:20]


def _start_job(kind: str, fn: Callable[[Callable[[str], None]], Any]) -> dict:
    running = [j for j in _jobs.values() if j["state"] == "running"]
    if running:
        raise HarnessError(f"{running[0]['kind']} is already running", code="busy")
    job = {"id": uuid.uuid4().hex[:10], "kind": kind, "state": "running", "log": [], "started": time.time(), "result": None}
    _jobs[job["id"]] = job

    def log(line: str) -> None:
        job["log"] = (job["log"] + [line])[-200:]

    def run() -> None:
        try:
            job["result"] = fn(log)
            job["state"] = "done"
        except Exception as e:  # noqa: BLE001
            log(f"failed: {e}")
            job.update(state="failed", error=str(e)[:1000])
        job["finished"] = time.time()

    threading.Thread(target=run, name=f"vmh-{kind}", daemon=True).start()
    return dict(job)


def _base_python() -> str:
    """A Python 3.10+ to build VM-Harness's venv with (ABP's own interpreter, not its venv's)."""
    base = getattr(sys, "_base_executable", "") or sys.executable
    return base if Path(base).is_file() else sys.executable


def _setup(log: Callable[[str], None]) -> dict:
    d = install_dir()
    if not _is_checkout(d):
        if d.exists() and any(d.iterdir()):
            raise HarnessError(f"{d} exists but is not a VM-Harness checkout")
        log(f"cloning {REPO_URL} into {d}")
        d.parent.mkdir(parents=True, exist_ok=True)
        r = _run(["git", "clone", "--depth", "50", REPO_URL, str(d)], timeout=1800)
        if r.returncode != 0:
            raise HarnessError(r.stderr.strip()[-600:] or "git clone failed")
    if not python_exe(d).is_file():
        log("creating its virtual environment")
        r = _run([_base_python(), "-m", "venv", str(d / ".venv")], timeout=600)
        if r.returncode != 0:
            raise HarnessError(r.stderr.strip()[-600:] or "venv creation failed")
    log("installing its dependencies (this takes a few minutes the first time)")
    r = _run([python_exe(d), "-m", "pip", "install", "--disable-pip-version-check", "-q", "-e", "."], cwd=d, timeout=3600)
    if r.returncode != 0:
        raise HarnessError(r.stderr.strip()[-1000:] or "pip install failed")
    log("installed")
    return install_info()


def setup() -> dict:
    """Clone VM-Harness if it is not here, create its venv, install its dependencies (in the background)."""
    return _start_job("setup", _setup)


def _update(log: Callable[[str], None]) -> dict:
    d = install_dir()
    before = install_info(fetch=True)
    if before.get("changed_files"):
        raise HarnessError(f"{d} has {before['changed_files']} uncommitted change(s); commit or stash them first "
                           "(ABP never overwrites work in progress)")
    if not before.get("behind"):
        log("already up to date")
        return before
    hub = client.find()
    was_running = bool(hub and not hub.remote)
    if was_running:
        log("stopping the hub")
        stop_hub()
    log(f"pulling {before['behind']} new commit(s)")
    _git(d, "pull", "--ff-only", "--quiet", timeout=600)
    log("updating dependencies")
    r = _run([python_exe(d), "-m", "pip", "install", "--disable-pip-version-check", "-q", "-e", "."], cwd=d, timeout=3600)
    if r.returncode != 0:
        raise HarnessError(r.stderr.strip()[-1000:] or "pip install failed")
    if was_running:
        log("starting the hub again")
        start_hub()
    after = install_info()
    log(f"now at {after.get('commit')}: {after.get('subject')}")
    return after


def update() -> dict:
    """Pull the latest VM-Harness from its repo and reinstall (in the background). Refuses if there is local work."""
    return _start_job("update", _update)


# ---- the hub and the window ------------------------------------------------------------------------------------------
def start_hub(wait_s: float = 45.0) -> dict:
    found = client.find()
    if found:
        return {"running": True, "url": found.url, "pid": found.pid, "already": True}
    d = install_dir()
    if not python_exe(d).is_file():
        raise HarnessError("VM-Harness is not installed yet (setup installs it)", code="not_installed")
    exe = python_exe(d, windowless=True)
    exe = exe if exe.is_file() else python_exe(d)
    client.vmh_home().mkdir(parents=True, exist_ok=True)
    with open(client.vmh_home() / "hub.log", "ab") as out:
        # preset "daemon": windowless with a hidden console its children inherit, its own process
        # group, below-normal priority, recorded as persistent - the hub outlives ABP on purpose.
        spawn.spawn([str(exe), "-m", "vm_harness", "serve"], preset="daemon", name="vm-harness-hub",
                    owner="vm_harness", cwd=d, env={**os.environ, "PYTHONPATH": os.pathsep.join([str(d / "src"), str(d)])},
                    stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,
                    creationflags=DETACHED_HIDDEN_CONSOLE)
    deadline = time.time() + wait_s
    while time.time() < deadline:
        found = client.find()
        if found:
            return {"running": True, "url": found.url, "pid": found.pid, "already": False}
        time.sleep(0.4)
    raise HarnessError(f"the hub did not start within {wait_s:.0f}s (see {client.vmh_home() / 'hub.log'})", code="timeout")


def stop_hub() -> dict:
    hub = client.find()
    if hub is None:
        return {"running": False}
    client.request("POST", "/v1/service/stop", body={}, hub=hub, timeout=10)
    for _ in range(40):
        if client.find() is None:
            break
        time.sleep(0.25)
    return {"running": client.find() is not None}


def open_window(wait_s: float = 60.0) -> dict:
    client.require()
    return client.call("gui.launch", {"wait_s": wait_s}, timeout=wait_s + 15)


# ---- status ----------------------------------------------------------------------------------------------------------
def status(*, backends: bool = True) -> dict:
    out: dict[str, Any] = {"install": install_info(), "jobs": [j for j in jobs() if j["state"] == "running"]}
    hub = client.find()
    out["hub"] = {"running": hub is not None, **({"url": hub.url, "pid": hub.pid, "version": hub.version,
                                                  "remote": hub.remote, "window": hub.gui} if hub else {})}
    if hub and backends:
        try:
            out["backends"] = client.call("host.backends", timeout=60)
        except HarnessError as e:
            out["backends_error"] = str(e)
    return out


def summary() -> dict:
    s = status()
    b = s.get("backends") or {}
    return {"installed": s["install"]["installed"], "path": s["install"]["path"], "commit": s["install"].get("commit"),
            "behind": s["install"].get("behind"), "hub": s["hub"],
            "hypervisors": sorted(n for n, i in (b.get("hypervisors") or {}).items() if i.get("available")),
            "containers": sorted(n for n, i in (b.get("containers") or {}).items() if i.get("available"))}


# ---- MCP -------------------------------------------------------------------------------------------------------------
MCP_NAME = "vm-harness"


def mcp_command(compact: bool = True) -> tuple[str, list[str]]:
    d = install_dir()
    return str(python_exe(d)), ["-m", "vm_harness", "mcp", *(["--tools", "compact"] if compact else [])]


def register_mcp(compact: bool = True) -> dict:
    """Add VM-Harness's own MCP server to ABP's external MCP servers (for other MCP-aware tools and agents).
    compact: 3 tools (search, describe, call) rather than one per operation, which keeps agents' context small."""
    import json as _json
    from bot.storage import extensions as ext
    command, args = mcp_command(compact)
    if ext.get_external_mcp_server(MCP_NAME) is not None:
        ext.delete_external_mcp_server(MCP_NAME)
    ext.add_external_mcp_server(MCP_NAME, "stdio", command=command, args_json=_json.dumps(args))
    return {"name": MCP_NAME, "command": command, "args": args}
