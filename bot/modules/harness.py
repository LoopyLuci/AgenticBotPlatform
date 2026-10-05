"""Installing, updating, building, running and checking modules, as their manifests describe.

Every module stays its own program in its own checkout. ABP clones it (or uses a developer's working copy next to
ABP's folder), pulls updates with fast-forward only, and never overwrites uncommitted work. Builds, updates and
pipeline runs are background jobs with a streamed log (one at a time per module). Modules with an adapter (the three
ABP drove before this framework) go through their own code.

Every process here is in a sandbox_ns cell (bot/sandbox_ns): a build or a pipeline run gets one cell for the whole
job, so the timeout and a stop take the compiler and everything it started with them, and a module's hub - which is
meant to outlive ABP - gets a persistent "daemon" cell that records it and bounds it without stopping it. The short
git calls (`_run`/`_git`: rev-parse, log, status, a version probe) stay plain subprocess: they start no children of
their own, they are buffered rather than streamed, and the windowless guard already covers them.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Optional

from bot.modules import adapters, client, registry
from bot.modules.client import ModuleError
from bot.modules.manifest import Manifest, this_os
from bot.sandbox_ns import guard
from bot.sandbox_ns.cell import Cell, cell_for, new_cell
from bot.sandbox_ns.spawn import spawn as ns_spawn

OWNER = "modules.harness"

NO_WINDOW = 0x08000000 if os.name == "nt" else 0
# A hidden console of its own (not DETACHED_PROCESS: a process with no console makes every console program it
# starts open a visible window), in its own process group. It still outlives ABP.
DETACHED = (0x00000200 | NO_WINDOW) if os.name == "nt" else 0
NEW_CONSOLE = 0x00000010 if os.name == "nt" else 0


def _m(mid: str) -> Manifest:
    try:
        return registry.get(mid)
    except registry.UnknownModule as e:
        raise ModuleError(str(e.args[0]), code="not_found", status=404) from None


def _adapter(m: Manifest) -> Optional[adapters.Adapter]:
    return adapters.get(m.adapter)


def checkout_dir(m: Manifest) -> Path:
    a = _adapter(m)
    return Path(a.install_dir()) if a else registry.install_dir(m)


def _run(args: list, cwd: Optional[Path] = None, timeout: float = 120, env: Optional[dict] = None) -> subprocess.CompletedProcess:
    return subprocess.run([str(a) for a in args], cwd=str(cwd) if cwd else None, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=timeout, creationflags=NO_WINDOW,
                          stdin=subprocess.DEVNULL, env=env)


def _git(d: Path, *args: str, timeout: float = 120) -> str:
    try:
        r = _run(["git", "-C", str(d), *args], timeout=timeout)
    except FileNotFoundError:
        raise ModuleError("git is not installed", code="not_installed") from None
    if r.returncode != 0:
        raise ModuleError((r.stderr or r.stdout).strip()[-600:] or f"git {' '.join(args)} failed")
    return r.stdout.strip()


# ---- the toolchain ---------------------------------------------------------------------------------------------------
def _version_tuple(text: str) -> tuple[int, ...]:
    mt = re.search(r"(\d+(?:\.\d+)+|\d+)", text or "")
    return tuple(int(x) for x in mt.group(1).split(".")) if mt else ()


def _which(tool: str) -> Optional[str]:
    found = shutil.which(tool)
    if not found and tool in ("cargo", "rustc"):
        p = Path.home() / ".cargo" / "bin" / (tool + (".exe" if os.name == "nt" else ""))
        found = str(p) if p.is_file() else None
    return found


def toolchain(m: Manifest) -> list[dict]:
    """Each tool the module needs: found? which version? new enough?"""
    out = []
    for req in m.requires:
        path = _which(req.tool)
        row: dict[str, Any] = {"tool": req.tool, "min": req.min, "url": req.url, "found": bool(path), "ok": False}
        if path:
            try:
                cmd = [path, "--version"] if not path.lower().endswith((".cmd", ".bat")) else ["cmd", "/c", path, "--version"]
                v = _run(cmd, timeout=30)
                row["version"] = ((v.stdout or v.stderr).strip().splitlines() or [""])[0][:120]
            except (OSError, subprocess.TimeoutExpired):
                row["version"] = ""
            row["ok"] = not req.min or _version_tuple(row["version"]) >= _version_tuple(req.min)
        out.append(row)
    return out


def _require_toolchain(m: Manifest, log: Callable[[str], None]) -> None:
    missing = [t for t in toolchain(m) if not t["ok"]]
    for t in missing:
        what = f"{t['tool']} {t['min']}+" if t["min"] else t["tool"]
        have = f" (found {t.get('version') or 'an unknown version'})" if t["found"] else ""
        log(f"needs {what}{have}: {t['url'] or 'install it and try again'}")
    if missing:
        raise ModuleError(f"{m.name} needs: " + ", ".join(t["tool"] for t in missing), code="not_installed")


# ---- what is installed -------------------------------------------------------------------------------------------------
def built(m: Manifest) -> dict[str, bool]:
    return {o: Path(registry.expand(m, o)).exists() for o in m.build_outputs}


def install_info(mid: str, *, fetch: bool = False) -> dict:
    m = _m(mid)
    a = _adapter(m)
    if a:
        info = dict(a.install_info(fetch=fetch))
        info.setdefault("ready", bool(info.get("installed")))
        return info
    d = registry.install_dir(m)
    outs = built(m)
    info: dict[str, Any] = {"path": str(d), "installed": registry.is_checkout(m, d),
                            "developer_checkout": d.parent == registry.abp_root().parent,
                            "built": outs, "ready": bool(outs) and all(outs.values()) if m.build_steps else registry.is_checkout(m, d),
                            "target_dir": str(registry.target_dir(m)) if m.build_steps else None}
    if not info["installed"] or not (d / ".git").exists():
        return info
    try:
        info["commit"] = _git(d, "rev-parse", "--short", "HEAD")
        info["subject"] = _git(d, "log", "-1", "--format=%s").lstrip("﻿")
        info["branch"] = _git(d, "rev-parse", "--abbrev-ref", "HEAD")
        porcelain = [x for x in _git(d, "status", "--porcelain").splitlines() if x.strip()]
        # Only changes to tracked files block an update: a fast-forward pull leaves untracked files alone (and git
        # refuses by itself if one would be overwritten).
        info["changed_files"] = len([x for x in porcelain if not x.startswith("??")])
        info["untracked_files"] = len(porcelain) - info["changed_files"]
        if fetch:
            _git(d, "fetch", "--quiet", "origin", timeout=90)
        up = _git(d, "rev-parse", "--abbrev-ref", "@{upstream}")
        ahead, behind = _git(d, "rev-list", "--left-right", "--count", f"HEAD...{up}").split()
        info.update(upstream=up, ahead=int(ahead), behind=int(behind))
    except ModuleError as e:
        info["git_error"] = str(e)
    return info


# ---- background jobs ---------------------------------------------------------------------------------------------------
_jobs: dict[str, dict] = {}
_jobs_lock = threading.Lock()


def jobs(mid: Optional[str] = None) -> list[dict]:
    rows = [dict(j) for j in _jobs.values() if mid is None or j["module"] == mid]
    if mid:
        m = _m(mid)
        a = _adapter(m)
        if a:
            try:
                rows += [{**j, "module": mid} for j in a.jobs()]
            except ModuleError:
                pass
    return sorted(rows, key=lambda j: j.get("started", 0), reverse=True)[:20]


def job(job_id: str) -> dict:
    j = _jobs.get(job_id)
    if j is None:
        raise ModuleError(f"no job {job_id!r}", code="not_found", status=404)
    return dict(j)


def _start_job(m: Manifest, kind: str, fn: Callable[[Callable[[str], None]], Any]) -> dict:
    with _jobs_lock:
        if any(j["state"] == "running" and j["module"] == m.id for j in _jobs.values()):
            raise ModuleError(f"{m.name} already has a job running", code="busy", status=409)
        j = {"id": uuid.uuid4().hex[:10], "module": m.id, "kind": kind, "state": "running", "log": [],
             "started": time.time()}
        _jobs[j["id"]] = j
        for old in sorted(_jobs.values(), key=lambda x: x["started"])[:-100]:
            _jobs.pop(old["id"], None)

    def log(line: str) -> None:
        j["log"] = (j["log"] + [line])[-400:]

    def go() -> None:
        try:
            j["result"] = fn(log)
            j["state"] = "done"
        except Exception as e:  # noqa: BLE001
            log(f"failed: {e}")
            j.update(state="failed", error=str(e)[:2000])
        j["finished"] = time.time()
    threading.Thread(target=go, name=f"module-{m.id}-{kind}", daemon=True).start()
    return dict(j)


def _stream(m: Manifest, cmd: list[str], cwd: Path, env: dict, log: Callable[[str], None], timeout: float,
            keep: Callable[[str], bool] = lambda line: True, cell: Optional[Cell] = None) -> None:
    """Run a command, logging its output as it comes; on timeout, kill its whole process tree.

    `cell` is the build cell the caller's job made (one per build, one per pipeline run), so a
    timeout stops the compiler and anything it started - the cell, not the one pid we happen to
    know. Without one the command is spawned with the 'build' preset on its own, which still
    records it and bounds it."""
    exe = _which(cmd[0]) or cmd[0]
    argv = [exe, *cmd[1:]]
    if os.name == "nt" and exe.lower().endswith((".cmd", ".bat")):
        argv = ["cmd", "/c", *argv]
    log("$ " + " ".join(cmd))
    label = f"{m.id}: {' '.join(cmd)[:60]}"
    try:
        proc = ns_spawn(argv, cell=cell, preset="build", cwd=str(cwd), env=env, stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
                        stdin=subprocess.DEVNULL, name=label, owner=OWNER)
    except FileNotFoundError:
        raise ModuleError(f"{cmd[0]} is not installed", code="not_installed") from None
    # The timer is kept rather than handed to spawn()'s own watchdog because the log is read here,
    # line by line, and it must stop the tree whether or not the pipe ever closes.
    stop = (lambda: cell.kill(f"`{' '.join(cmd)}` passed its {timeout:g}s timeout")) if cell is not None \
        else (lambda: _kill_tree(proc.pid))
    timer = threading.Timer(timeout, stop)
    timer.start()
    tail: list[str] = []
    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            line = line.rstrip()
            tail = (tail + [line])[-40:]
            if line and keep(line):
                log(line[:400])
        code = proc.wait()
    finally:
        timer.cancel()
    if code != 0:
        raise ModuleError(f"`{' '.join(cmd)}` failed (exit {code}):\n" + "\n".join(tail[-25:]))


def _kill_tree(pid: int) -> None:
    try:
        import psutil
        p = psutil.Process(pid)
        for c in p.children(recursive=True):
            c.kill()
        p.kill()
    except Exception:  # noqa: BLE001
        pass


def _build_env(m: Manifest) -> dict:
    env = {**os.environ, **{k: registry.expand(m, v) for k, v in m.build_env.items()}}
    if registry._cfg().get("build_cache"):
        env["CARGO_TARGET_DIR"] = str(registry.target_dir(m))
    return env


def _workspace(m: Manifest) -> Path:
    d = registry.install_dir(m)
    return d / m.subdir if m.subdir else d


def _build(m: Manifest, log: Callable[[str], None]) -> None:
    if not m.build_steps:
        log("this module has no build steps yet")
        return
    _require_toolchain(m, log)
    env = _build_env(m)
    ws = _workspace(m)
    noisy = re.compile(r"^\s*(Compiling|Checking|Downloaded|Downloading|Fresh|Blocking|Updating crates)\b")
    # One cell for the whole build: the steps belong together, so one kill (the timeout above, or
    # the diagnostics page) stops whichever one is running and everything under it.
    with cell_for("build", name=f"{m.id} build", owner=OWNER) as cell:
        for step in m.build_steps:
            _stream(m, registry.expand_cmd(m, step), ws, env, log, timeout=7200,
                    keep=lambda line: not noisy.match(line), cell=cell)
    missing = [o for o, ok in built(m).items() if not ok]
    if missing:
        raise ModuleError("the build finished but these are missing: " + ", ".join(registry.expand(m, o) for o in missing))
    log("built")


def _clone(m: Manifest, log: Callable[[str], None]) -> None:
    d = registry.install_dir(m)
    if registry.is_checkout(m, d):
        return
    if d.exists() and any(d.iterdir()):
        raise ModuleError(f"{d} exists but is not a {m.name} checkout; move it away or set modules.{m.id}.path")
    log(f"cloning {m.repo} ({m.branch}) into {d}")
    d.parent.mkdir(parents=True, exist_ok=True)
    try:
        r = _run(["git", "clone", "--branch", m.branch, m.repo, str(d)], timeout=3600)
    except FileNotFoundError:
        raise ModuleError("git is not installed", code="not_installed") from None
    if r.returncode != 0:
        raise ModuleError(r.stderr.strip()[-600:] or "git clone failed")
    registry.modules(refresh=True)


def setup(mid: str) -> dict:
    """Clone the module if it is not here, then build it (in the background)."""
    m = _m(mid)
    a = _adapter(m)
    if a:
        return a.setup()

    def go(log):
        _clone(m, log)
        m2 = registry.get(m.id)      # the repo may bring its own manifest
        _build(m2, log)
        return install_info(m.id)
    return _start_job(m, "setup", go)


def build(mid: str) -> dict:
    m = _m(mid)
    a = _adapter(m)
    if a:
        return a.update()
    if not registry.is_checkout(m, registry.install_dir(m)):
        raise ModuleError(f"{m.name} is not installed yet (setup clones it)", code="not_installed")
    return _start_job(m, "build", lambda log: (_build(m, log), install_info(m.id))[1])


def _hub_is_ours(m: Manifest) -> Optional[client.Hub]:
    """The running hub, if it runs from this checkout or its build (so updating would replace what it runs)."""
    if m.hub is None:
        return None
    hub = client.find(m)
    if hub is None or not hub.pid:
        return None
    try:
        import psutil
        exe = Path(psutil.Process(hub.pid).exe()).resolve()
        roots = [registry.install_dir(m).resolve(), registry.target_dir(m).resolve()]
        return hub if any(r == exe or r in exe.parents for r in roots) else None
    except Exception:  # noqa: BLE001
        return None


def update(mid: str) -> dict:
    """Pull the latest (fast-forward only) and rebuild, in the background. Refuses over uncommitted work."""
    m = _m(mid)
    a = _adapter(m)
    if a:
        return a.update()
    d = registry.install_dir(m)
    if not registry.is_checkout(m, d):
        raise ModuleError(f"{m.name} is not installed yet (setup clones it)", code="not_installed")

    def go(log):
        before = install_info(m.id, fetch=True)
        if before.get("changed_files"):
            raise ModuleError(f"{d} has {before['changed_files']} uncommitted change(s); commit or stash them first "
                              "(ABP never overwrites work in progress)")
        if before.get("git_error"):
            raise ModuleError(before["git_error"])
        if not before.get("behind") and before.get("ready"):
            log("already up to date")
            return before
        hub = _hub_is_ours(m)
        if hub:
            log("stopping the hub (what it runs is being replaced)")
            stop_hub(m.id)
        if before.get("behind"):
            log(f"pulling {before['behind']} new commit(s)")
            _git(d, "pull", "--ff-only", "--quiet", timeout=900)
        m2 = registry.modules(refresh=True).get(m.id, m)
        _build(m2, log)
        if hub:
            log("starting the hub again")
            start_hub(m.id)
        return install_info(m.id)
    return _start_job(m, "update", go)


# ---- the hub, windows, MCP, the pipeline -------------------------------------------------------------------------------
def hub_state(m: Manifest) -> dict:
    a = _adapter(m)
    if a:
        run = a.running()
        return {"running": run is not None, **(run or {})}
    if m.hub is None:
        return {"running": False, "available": False}
    hub = client.find(m)
    return {"running": hub is not None, **({"url": hub.url, "pid": hub.pid, "version": hub.version} if hub else {})}


def _modkit_env() -> dict:
    """abp_modkit ships inside ABP: a module whose hub is `{abp_python} -m abp_modkit` finds it on PYTHONPATH."""
    try:
        import abp_modkit
        root = str(Path(abp_modkit.__file__).resolve().parent.parent)      # where it really is (the app's bundle)
    except ImportError:
        root = str(registry.abp_root())
    cur = os.environ.get("PYTHONPATH", "")
    return {"PYTHONPATH": os.pathsep.join([root, cur]) if cur else root}


def abp_env(m: Manifest) -> dict:
    """For a module that uses ABP back ([abp] connect = true): where ABP is and a key of the module's own.

    The key is an integration key (bot/integrations.py) with the manifest's preset's scopes, minted once and kept
    in the module's data folder (mode 600); a revoked key is replaced. {} for modules that do not connect back."""
    if not m.abp_connect:
        return {}
    from bot import integrations
    preset = integrations.PRESETS.get(m.abp_preset)
    if preset is None:
        raise ModuleError(f"{m.name}: [abp] preset {m.abp_preset!r} is not one of {', '.join(integrations.PRESETS)}",
                          code="invalid", status=400)
    data = registry.data_dir(m)
    data.mkdir(parents=True, exist_ok=True)
    f = data / "abp-key"
    key = ""
    try:
        key = f.read_text(encoding="utf-8").strip()
    except OSError:
        pass
    if not key or integrations.scopes_for(key) is None:
        _, key = integrations.mint(f"module: {m.name}", preset["scopes"], preset=m.abp_preset)
        tmp = f.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(key)
        os.replace(tmp, f)
    return {"ABP_URL": f"http://127.0.0.1:{os.environ.get('DASHBOARD_PORT', '8787')}", "ABP_KEY": key}


_svc_cache: dict[str, tuple[float, dict]] = {}


def service_urls(m: Manifest, ttl: float = 5.0) -> dict:
    """Where a module's web UI and OpenAI-compatible API are right now: the URLs in its manifest, or ("service") the
    ones its abp_modkit hub reports while the project's server answers. {} when there are none (or it is down)."""
    want_web, want_ai = m.web, m.openai
    if not want_web and not want_ai:
        return {}
    out: dict[str, str] = {}
    if want_web and want_web != "service":
        out["web"] = want_web
    if want_ai and want_ai != "service":
        out["openai"] = want_ai
    if "service" not in (want_web, want_ai) or m.hub is None:
        return out
    hit = _svc_cache.get(m.id)
    if hit and time.monotonic() - hit[0] < ttl:
        return {**hit[1], **out}
    svc: dict = {}
    try:
        if client.find(m, timeout=1.0) is not None:
            st = (client.call(m, "service.status", {}, timeout=10) or {}).get("service") or {}
            if st.get("answers"):
                if want_web == "service" and st.get("web"):
                    svc["web"] = st["web"]
                if want_ai == "service" and st.get("openai"):
                    svc["openai"] = st["openai"]
    except Exception:  # noqa: BLE001 - a module that does not answer has no URLs
        svc = {}
    _svc_cache[m.id] = (time.monotonic(), svc)
    return {**svc, **out}


# Called with the module id after a hub this harness started answers (bot/octopus/connectors.py hands it a session).
HUB_STARTED: list = []
#: The daemon cell per module id, for the hubs this process started. A hub is meant to outlive ABP,
#: so its cell is persistent: recorded, windowless and bounded, and only stop_hub() stops it.
_hub_cells: dict[str, Cell] = {}


def start_hub(mid: str) -> dict:
    # A module's hub is an auto-started service: a web UI and an
    # OpenAI-compatible server bound on real ports, from the machine's module
    # checkout. An agent's sandbox (ABP_SANDBOX_INSTANCE) must never bring one
    # up - it would fight the real instance for the same port - so this refuses
    # rather than letting "one hub per data dir" decide it.
    from bot import lease

    reason = lease.sandbox_blocked_reason()
    if reason:
        raise ModuleError(
            f"a sandboxed instance never starts a module service: {reason}",
            code="sandbox", status=409,
        )
    m = _m(mid)
    a = _adapter(m)
    if a:
        return a.start_hub()
    if m.hub is None or not m.hub.start:
        raise ModuleError(f"{m.name} has no hub to start yet", code="unsupported", status=400)
    found = client.find(m)
    if found:
        return {"running": True, "url": found.url, "pid": found.pid, "already": True}
    cmd = registry.expand_cmd(m, m.hub.start)
    exe = Path(cmd[0])
    if exe.is_absolute() and not exe.exists():
        raise ModuleError(f"{m.name} is not built yet ({exe} is missing; setup builds it)", code="not_installed")
    data = registry.data_dir(m)
    data.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "ABP_MODULE_ID": m.id, "ABP_MODULE_DATA": str(data), **_modkit_env(), **abp_env(m)}
    # A hub is a service that has to keep answering after ABP restarts, so it gets a persistent cell:
    # the reaper leaves it alone (its owner stops it), and close_cells() only releases it.
    cell = new_cell("daemon", name=f"{m.id} hub", owner=OWNER)
    try:
        with open(data / "hub.log", "ab") as out:
            ns_spawn([_which(cmd[0]) or cmd[0], *cmd[1:]], cell=cell, cwd=str(_workspace(m)), env=env,
                     stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT, creationflags=DETACHED,
                     start_new_session=os.name != "nt", name=f"{m.id} hub", owner=OWNER)
    except OSError as exc:
        cell.close()
        raise ModuleError(f"cannot start {m.name}'s hub ({cmd[0]}): {exc}", code="not_installed") from None
    _hub_cells[m.id] = cell
    deadline = time.time() + m.hub.start_timeout_s
    while time.time() < deadline:
        found = client.find(m)
        if found:
            for hook in HUB_STARTED:
                try:
                    hook(m.id)
                except Exception:  # noqa: BLE001 - a hook never fails a start
                    pass
            return {"running": True, "url": found.url, "pid": found.pid, "already": False}
        time.sleep(0.4)
    # It never answered: take the cell with it, rather than leave a hub nothing is watching.
    cell.kill(f"{m.name}'s hub did not start within {m.hub.start_timeout_s:.0f}s")
    _hub_cells.pop(m.id, None)
    raise ModuleError(f"{m.name}'s hub did not start within {m.hub.start_timeout_s:.0f}s (see {data / 'hub.log'})",
                      code="timeout")


def _release_hub_cell(mid: str) -> Optional[Cell]:
    """Let go of a hub's daemon cell. A persistent cell's close only releases the job handle - the
    hub itself is somebody else's business now (stopped, or another ABP's)."""
    cell = _hub_cells.pop(mid, None)
    if cell is not None:
        cell.close()
    return cell


def stop_hub(mid: str) -> dict:
    m = _m(mid)
    a = _adapter(m)
    if a:
        return a.stop_hub()
    hub = client.find(m) if m.hub else None
    if hub is None:
        _release_hub_cell(mid)
        return {"running": False}
    try:
        client.stop(m, hub)
    except ModuleError:
        pass
    for _ in range(80):
        if client.find(m, timeout=1.0) is None:
            _release_hub_cell(mid)
            return {"running": False}
        time.sleep(0.25)
    # Still answering: the cell is what stops it, tree and all, when this process started the hub.
    # A hub from an earlier ABP has no cell here, so its pid is the only handle there is.
    cell = _hub_cells.pop(m.id, None)
    if cell is not None:
        cell.kill(f"stopping {m.name}'s hub")
    elif hub.pid:
        _kill_tree(hub.pid)
    time.sleep(0.5)
    return {"running": client.find(m, timeout=1.0) is not None, "killed": True}


def _launch_visible(m: Manifest, cmd: list[str], console: bool) -> dict:
    cmd = registry.expand_cmd(m, cmd)
    exe = Path(cmd[0])
    if exe.is_absolute() and not exe.exists():
        raise ModuleError(f"{exe} is missing ({m.name} is not built yet)", code="not_installed")
    # A console for a person is the one thing the windowless guard (bot/sandbox_ns/guard.py)
    # takes away by default, so it is asked for explicitly here - inside guard.visible(), and
    # nowhere else in this file.
    flags = NEW_CONSOLE if console else 0
    with guard.visible():
        p = subprocess.Popen([_which(cmd[0]) or cmd[0], *cmd[1:]], cwd=str(_workspace(m)), creationflags=flags,
                             env={**os.environ, **abp_env(m)},
                             stdin=None if console else subprocess.DEVNULL, start_new_session=os.name != "nt")
    return {"opened": True, "pid": p.pid}


def open_gui(mid: str) -> dict:
    m = _m(mid)
    a = _adapter(m)
    if a:
        return a.open_window()
    if not m.gui:
        raise ModuleError(f"{m.name} has no window to open", code="unsupported", status=400)
    return _launch_visible(m, m.gui, console=False)


def open_tui(mid: str) -> dict:
    m = _m(mid)
    if not m.tui:
        raise ModuleError(f"{m.name} has no terminal UI", code="unsupported", status=400)
    return _launch_visible(m, m.tui, console=True)


def run_pipeline(mid: str) -> dict:
    """Run the module's own local CI/CD pipeline (a background job with its log)."""
    m = _m(mid)
    if not m.pipeline:
        raise ModuleError(f"{m.name} has no pipeline yet", code="unsupported", status=400)
    d = checkout_dir(m)
    if not d.is_dir():
        raise ModuleError(f"{m.name} is not installed yet", code="not_installed")
    env = {**_build_env(m), "NO_COLOR": "1"}

    def go(log):
        # The pipeline is one unit of work, so it gets its own cell: its timeout stops the whole
        # run rather than one step of it.
        with cell_for("build", name=f"{m.id} pipeline", owner=OWNER) as cell:
            _stream(m, registry.expand_cmd(m, m.pipeline), d, env, log, timeout=7200, cell=cell)

    return _start_job(m, "pipeline", go)


def register_mcp(mid: str) -> dict:
    """Add the module's own MCP server (stdio) to ABP's external MCP servers, as abp-<id>."""
    import json as _json
    m = _m(mid)
    a = _adapter(m)
    if a:
        return a.register_mcp()
    if not m.mcp_stdio:
        raise ModuleError(f"{m.name} has no MCP server yet", code="unsupported", status=400)
    from bot.storage import extensions as ext
    cmd = registry.expand_cmd(m, m.mcp_stdio)
    name = f"abp-{m.id}"
    if ext.get_external_mcp_server(name) is not None:
        ext.delete_external_mcp_server(name)
    ext.add_external_mcp_server(name, "stdio", command=cmd[0], args_json=_json.dumps(cmd[1:]),
                                env_json=_json.dumps({"ABP_MODULE_DATA": str(registry.data_dir(m)), **_modkit_env(),
                                                      **abp_env(m)}))
    return {"name": name, "command": cmd[0], "args": cmd[1:]}


# ---- operations (through the hub) --------------------------------------------------------------------------------------
def operations(mid: str, refresh: bool = False) -> list[dict]:
    m = _m(mid)
    a = _adapter(m)
    if a:
        return a.operations(refresh)
    if m.hub is None:
        raise ModuleError(f"{m.name} has no hub yet, so no operations", code="unsupported", status=400)
    return client.operations(m, refresh)


def operation(mid: str, op_id: str) -> dict:
    for o in operations(mid):
        if o["id"] == op_id:
            return o
    raise ModuleError(f"{_m(mid).name} has no operation {op_id!r}", code="not_found", status=404)


def call(mid: str, op_id: str, args: Optional[dict] = None, timeout: float = 900.0) -> Any:
    m = _m(mid)
    a = _adapter(m)
    if a:
        return a.call(op_id, args, timeout)
    if m.hub is None:
        raise ModuleError(f"{m.name} has no hub yet", code="unsupported", status=400)
    return client.call(m, op_id, args, timeout=timeout)


# ---- status ------------------------------------------------------------------------------------------------------------
def host_check(m: Manifest) -> dict:
    return {"os": this_os(), "supported": this_os() in m.host_os, "needs": m.host_needs}


def status(mid: str, *, fetch: bool = False) -> dict:
    m = _m(mid)
    out: dict[str, Any] = {"module": m.public(), "install": install_info(mid, fetch=fetch), "hub": hub_state(m),
                           "host": host_check(m), "jobs": [j for j in jobs(mid) if j.get("state") == "running"]}
    if m.web or m.openai:
        out["service"] = service_urls(m)
    if m.requires and not m.adapter:
        out["toolchain"] = toolchain(m)
    err = registry.manifest_errors().get(m.id)
    if err:
        out["manifest_error"] = err
    return out


def overview() -> list[dict]:
    """Every module in a line: what it is, installed? built? hub running? updates waiting? a job running?"""
    rows = []
    for m in registry.modules().values():
        row: dict[str, Any] = {**m.public()}
        try:
            info = install_info(m.id)
            row.update(installed=bool(info.get("installed")), ready=bool(info.get("ready")), path=info.get("path"),
                       commit=info.get("commit"), branch=info.get("branch"), behind=info.get("behind"),
                       ahead=info.get("ahead"), changed_files=info.get("changed_files"))
        except ModuleError as e:
            row.update(installed=False, error=str(e))
        try:
            row["hub"] = hub_state(m)
        except Exception:  # noqa: BLE001
            row["hub"] = {"running": False}
        row["host_supported"] = this_os() in m.host_os
        row["job"] = next((j["kind"] for j in _jobs.values() if j["module"] == m.id and j["state"] == "running"), None)
        rows.append(row)
    return rows
