"""Installing, building, updating, starting and checking TransferDaemon from ABP.

TransferDaemon stays its own program: its own checkout and Rust workspace, built with cargo into its own target
folder. ABP runs the binaries it built: ``transferd`` (the daemon, with the control hub), ``transferd-ui``,
``transferd-tui``, ``transferd-cli`` and the relays (``relayd``, ``relayd-ws``, ``dhtd``).

Where the checkout is, first match wins: $ABP_TRANSFERDAEMON_DIR, transferdaemon.path in config/backends.yaml, a
TransferDaemon folder next to ABP's (a developer's working copy), data/modules/TransferDaemon (cloned by setup).
"""
from __future__ import annotations

import os
import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Optional

from bot.transferdaemon import client
from bot.transferdaemon.client import DaemonError

REPO_URL = "https://github.com/LoopyLuci/TransferDaemon.git"
NO_WINDOW = 0x08000000 if os.name == "nt" else 0
EXE = ".exe" if os.name == "nt" else ""
# What ABP builds: the daemon, both user interfaces, the CLI (also the MCP server) and the relays.
PACKAGES = ["transferd", "transferd-ui", "transferd-tui", "transferd-cli", "relayd", "transferd-relay"]
BINARIES = ["transferd", "transferd-ui", "transferd-tui", "transferd-cli", "relayd", "relayd-ws", "dhtd"]


def _cfg() -> dict:
    return client._cfg()


def _abp_root() -> Path:
    from bot.envfile import PROJECT_ROOT
    return Path(PROJECT_ROOT)


def _is_checkout(p: Path) -> bool:
    return (p / "transferdaemon" / "Cargo.toml").is_file() and (p / "transferdaemon" / "crates" / "transferd").is_dir()


def install_dir() -> Path:
    for candidate in (os.environ.get("ABP_TRANSFERDAEMON_DIR"), _cfg().get("path")):
        if candidate:
            return Path(candidate).expanduser()
    sibling = _abp_root().parent / "TransferDaemon"
    if _is_checkout(sibling):
        return sibling
    return _abp_root() / "data" / "modules" / "TransferDaemon"


def workspace() -> Path:
    return install_dir() / "transferdaemon"


def bin_dir() -> Path:
    """The release build if it has the control hub, else the debug build (a developer's working copy)."""
    target = workspace() / "target"
    profile = str(_cfg().get("profile") or "")
    if profile in ("release", "debug"):
        return target / profile
    release, debug = target / "release", target / "debug"
    def cli(d: Path) -> float:
        p = d / f"transferd-cli{EXE}"
        return p.stat().st_mtime if p.is_file() else 0.0
    return release if cli(release) >= cli(debug) and cli(release) > 0 else (debug if cli(debug) > 0 else release)


def binary(name: str) -> Path:
    return bin_dir() / f"{name}{EXE}"


def _run(args: list, cwd: Optional[Path] = None, timeout: float = 1800) -> subprocess.CompletedProcess:
    return subprocess.run([str(a) for a in args], cwd=str(cwd) if cwd else None, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=timeout, creationflags=NO_WINDOW,
                          stdin=subprocess.DEVNULL, shell=False)


def _git(d: Path, *args: str, timeout: float = 120) -> str:
    r = _run(["git", "-C", str(d), *args], timeout=timeout)
    if r.returncode != 0:
        raise DaemonError((r.stderr or r.stdout).strip()[-600:] or f"git {' '.join(args)} failed")
    return r.stdout.strip()


def _cargo() -> str:
    cargo = shutil.which("cargo") or str(Path.home() / ".cargo" / "bin" / f"cargo{EXE}")
    if not Path(cargo).is_file() and not shutil.which("cargo"):
        raise DaemonError("Rust (cargo) is not installed: https://rustup.rs", code="not_installed")
    return cargo


def install_info(*, fetch: bool = False) -> dict:
    d = install_dir()
    built = {b: binary(b).is_file() for b in BINARIES}
    info: dict[str, Any] = {
        "path": str(d), "installed": _is_checkout(d), "developer_checkout": d.parent == _abp_root().parent,
        "bin_dir": str(bin_dir()), "built": built, "ready": built.get("transferd", False) and built.get("transferd-cli", False),
        "cargo": bool(shutil.which("cargo") or (Path.home() / ".cargo" / "bin" / f"cargo{EXE}").is_file()),
        "data_dir": str(client.data_dir()),
    }
    if info["ready"]:
        info["built_at"] = binary("transferd").stat().st_mtime
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
    except DaemonError as e:
        info["git_error"] = str(e)
    return info


_jobs: dict[str, dict] = {}


def jobs() -> list[dict]:
    return sorted((dict(j) for j in _jobs.values()), key=lambda j: j["started"], reverse=True)[:20]


def _start_job(kind: str, fn: Callable[[Callable[[str], None]], Any]) -> dict:
    if any(j["state"] == "running" for j in _jobs.values()):
        raise DaemonError("an install or update is already running", code="busy")
    job = {"id": uuid.uuid4().hex[:10], "kind": kind, "state": "running", "log": [], "started": time.time()}
    _jobs[job["id"]] = job

    def log(line: str) -> None:
        job["log"] = (job["log"] + [line])[-300:]

    def go() -> None:
        try:
            job["result"] = fn(log)
            job["state"] = "done"
        except Exception as e:  # noqa: BLE001
            log(f"failed: {e}")
            job.update(state="failed", error=str(e)[:1500])
        job["finished"] = time.time()
    threading.Thread(target=go, name=f"td-{kind}", daemon=True).start()
    return dict(job)


def _build(log: Callable[[str], None]) -> None:
    """cargo build --release of the daemon, the interfaces, the CLI and the relays, streaming cargo's progress."""
    cargo = _cargo()
    args = [cargo, "build", "--release"] + [x for p in PACKAGES for x in ("-p", p)]
    log("building (" + " ".join(["cargo", "build", "--release"] + [x for p in PACKAGES for x in ("-p", p)]) + ")")
    proc = subprocess.Popen(args, cwd=str(workspace()), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                            encoding="utf-8", errors="replace", creationflags=NO_WINDOW, stdin=subprocess.DEVNULL)
    tail: list[str] = []
    assert proc.stdout is not None
    for line in proc.stdout:
        line = line.rstrip()
        tail = (tail + [line])[-40:]
        if line.lstrip().startswith(("Compiling transferd", "Compiling relayd", "Finished", "error", "warning: unused")):
            log(line.strip())
    if proc.wait() != 0:
        raise DaemonError("cargo build failed:\n" + "\n".join(tail[-25:]))


def _setup(log: Callable[[str], None]) -> dict:
    d = install_dir()
    if not _is_checkout(d):
        if d.exists() and any(d.iterdir()):
            raise DaemonError(f"{d} exists but is not a TransferDaemon checkout")
        log(f"cloning {REPO_URL} into {d}")
        d.parent.mkdir(parents=True, exist_ok=True)
        r = _run(["git", "clone", "--depth", "50", REPO_URL, str(d)], timeout=1800)
        if r.returncode != 0:
            raise DaemonError(r.stderr.strip()[-600:] or "git clone failed")
    _build(log)
    log("installed")
    return install_info()


def setup() -> dict:
    """Clone TransferDaemon if it is not here and build it (in the background: a first build takes a while)."""
    return _start_job("setup", _setup)


def _stop_ours(log: Callable[[str], None]) -> bool:
    """Stop the daemon (and its window/TUI) if it runs from this build, so the binaries can be replaced."""
    hub = client.find()
    if hub is None:
        return False
    try:
        import psutil
        exe = Path(psutil.Process(hub.pid).exe()).resolve()
        if bin_dir().resolve() not in exe.parents:
            return False
    except Exception:  # noqa: BLE001
        return False
    log("closing the window and terminal UI, stopping the daemon (their binaries are being replaced)")
    for op, args in (("gui.window", {"action": "close"}), ("tui.quit", {})):
        try:
            client.call(op, args, timeout=15)
        except DaemonError:
            pass
    stop_daemon()
    return True


def _update(log: Callable[[str], None]) -> dict:
    d = install_dir()
    before = install_info(fetch=True)
    if before.get("changed_files"):
        raise DaemonError(f"{d} has {before['changed_files']} uncommitted change(s); commit or stash them first "
                          "(ABP never overwrites work in progress)")
    if not before.get("behind") and before.get("ready"):
        log("already up to date")
        return before
    restart = _stop_ours(log)
    if before.get("behind"):
        log(f"pulling {before['behind']} new commit(s)")
        _git(d, "pull", "--ff-only", "--quiet", timeout=600)
    _build(log)
    if restart:
        log("starting the daemon again")
        start_daemon()
    return install_info()


def update() -> dict:
    """Pull the latest TransferDaemon and rebuild (in the background). Refuses while there is uncommitted work."""
    return _start_job("update", _update)


def start_daemon(wait_s: float = 90.0) -> dict:
    """Start the daemon (no console window) if it is not running. Its identity unlocks itself (the protected phrase
    cache), so contacts and relays come back without anyone typing the phrase."""
    found = client.find()
    if found:
        return {"running": True, "url": found.url, "pid": found.pid, "already": True}
    exe = binary("transferd")
    if not exe.is_file():
        raise DaemonError("TransferDaemon is not built yet (setup builds it)", code="not_installed")
    env = dict(os.environ)
    for key in ("bind", "relays", "dht_bootstrap"):
        val = _cfg().get(key)
        if val:
            env[{"bind": "TRANSFERD_BIND_ADDR", "relays": "TRANSFERD_RELAY_ADDR",
                 "dht_bootstrap": "TRANSFERD_DHT_BOOTSTRAP"}[key]] = ",".join(val) if isinstance(val, list) else str(val)
    client.data_dir().mkdir(parents=True, exist_ok=True)
    flags = (0x00000200 | NO_WINDOW) if os.name == "nt" else 0     # a hidden console its children inherit, own group
    with open(client.data_dir() / "transferd.log", "ab") as out:
        subprocess.Popen([str(exe)], cwd=str(bin_dir()), env=env, stdin=subprocess.DEVNULL, stdout=out,
                         stderr=subprocess.STDOUT, creationflags=flags, start_new_session=os.name != "nt")
    deadline = time.time() + wait_s
    while time.time() < deadline:
        found = client.find()
        if found:
            return {"running": True, "url": found.url, "pid": found.pid, "already": False}
        time.sleep(0.5)
    raise DaemonError(f"the daemon did not start within {wait_s:.0f}s (see {client.data_dir() / 'transferd.log'})", code="timeout")


def stop_daemon() -> dict:
    hub = client.find()
    if hub is None:
        return {"running": False}
    client.call("daemon.stop", {}, timeout=20)
    for _ in range(60):
        if client.find() is None:
            return {"running": False}
        time.sleep(0.25)
    return {"running": client.find() is not None}


def open_window(wait_s: float = 60.0) -> dict:
    client.require()
    return client.call("gui.launch", {"wait_s": wait_s}, timeout=wait_s + 30)


def open_tui(headless: bool = True, width: int = 120, height: int = 40) -> dict:
    client.require()
    return client.call("tui.launch", {"headless": headless, "width": width, "height": height}, timeout=90)


def status() -> dict:
    out: dict[str, Any] = {"install": install_info(), "jobs": [j for j in jobs() if j["state"] == "running"]}
    hub = client.find()
    out["daemon"] = {"running": hub is not None, **({"url": hub.url, "pid": hub.pid, "version": hub.version} if hub else {})}
    if hub:
        try:
            out["status"] = client.call("daemon.status", {}, timeout=20)
        except DaemonError as e:
            out["status_error"] = str(e)
    return out


def summary() -> dict:
    """A short status for the agent: installed? built? running? who am I? how many contacts?"""
    s = status()
    inst = s["install"]
    st = s.get("status") or {}
    return {
        "installed": inst["installed"], "built": inst["ready"], "path": inst["path"], "commit": inst.get("commit"),
        "updates_waiting": inst.get("behind", 0), "running": s["daemon"]["running"],
        "identity": st.get("identity"), "contacts": st.get("contacts"), "transfers": st.get("transfers"),
        "window_open": bool(st.get("gui")), "terminal_ui_open": bool(st.get("tui")), "relays": st.get("relays"),
        "jobs": s["jobs"],
    }


MCP_NAME = "transferdaemon"


def register_mcp(compact: bool = True) -> dict:
    """Add TransferDaemon's own MCP server (transferd-cli mcp) to ABP's external MCP servers."""
    import json as _json
    from bot.storage import extensions as ext
    cli = binary("transferd-cli")
    if not cli.is_file():
        raise DaemonError("transferd-cli is not built yet (setup builds it)", code="not_installed")
    args = ["mcp", *(["--tools", "compact"] if compact else [])]
    env = {"TRANSFERD_DATA_DIR": os.environ["TRANSFERD_DATA_DIR"]} if os.environ.get("TRANSFERD_DATA_DIR") else {}
    if ext.get_external_mcp_server(MCP_NAME) is not None:
        ext.delete_external_mcp_server(MCP_NAME)
    ext.add_external_mcp_server(MCP_NAME, "stdio", command=str(cli), args_json=_json.dumps(args), env_json=_json.dumps(env))
    return {"name": MCP_NAME, "command": str(cli), "args": args}
