"""Keeping a ModelMistress hub up: start it at boot, restart it with back-off, never fight one the owner started.

The hub lives in the OS ModelMistress home (`client.mm_home()`), the same one the desktop app and a headless
`model-mistress serve` use, so all of them share one hub and one model store. Controlled by `modelmistress.autostart`
(default true). A sandboxed instance never starts it. A healthy hub that is already running is reused, never doubled.
"""
from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import threading
import time
from typing import Callable, Optional

from bot.modelmistress import client, guard

logger = logging.getLogger(__name__)

CHECK_S = 30.0
BACKOFF_START_S = 30.0
BACKOFF_MAX_S = 900.0

_lock = threading.Lock()
_state: dict = {"up": None, "since": 0.0, "last_start": 0.0, "last_error": "", "starts": 0,
                "backoff_s": BACKOFF_START_S, "next_try": 0.0}


def autostart_enabled() -> bool:
    if os.environ.get("ABP_NO_MODELMISTRESS") or os.environ.get("ABP_NO_MODULE_PROVIDERS"):
        return False
    return client._cfg().get("autostart", True) is not False


def _sandboxed() -> Optional[str]:
    from bot import lease

    return lease.sandbox_blocked_reason()


def _mark(up: bool) -> None:
    with _lock:
        if _state["up"] is up:
            return
        _state["up"], _state["since"] = up, time.time()
    logger.info("ModelMistress hub is %s", "up" if up else "down")


def _start(cell=None):
    """Spawn the module's hub detached, with --home at the shared MM home. With no `cell` it gets a persistent daemon cell
    (it outlives ABP); a caller that owns the hub's lifetime passes its own. Returns the cell."""
    from bot.modules import harness as mh
    from bot.modules import registry
    from bot.sandbox_ns.cell import new_cell
    from bot.sandbox_ns.spawn import spawn as ns_spawn

    m = registry.get(client.MODULE_ID)
    if m.hub is None or not m.hub.start:
        raise RuntimeError("the ModelMistress module has no hub to start")
    cmd = registry.expand_cmd(m, m.hub.start)
    home = client.mm_home()
    why = guard.virtualized(home)
    if why:
        raise RuntimeError(f"not starting a hub from here: {why}")
    if os.path.isabs(cmd[0]) and not os.path.exists(cmd[0]):
        raise RuntimeError(f"ModelMistress is not built yet ({cmd[0]} is missing)")
    home.mkdir(parents=True, exist_ok=True)
    if "--home" in cmd:
        cmd[cmd.index("--home") + 1] = str(home)
    else:
        cmd[1:1] = ["--home", str(home)]
    exe = mh._which(cmd[0]) or cmd[0]
    env = {**os.environ, "ABP_MODULE_ID": m.id, "ABP_MODULE_DATA": str(home), **mh._modkit_env(), **mh.abp_env(m)}
    ws = mh._workspace(m)
    cwd = ws if ws.is_dir() else home                # the built target can live apart from a checkout (another worktree's)
    own = cell is None
    cell = new_cell("daemon", name=f"{m.id} hub", owner=mh.OWNER) if own else cell
    try:
        with open(home / "hub.log", "ab") as out:
            ns_spawn([exe, *cmd[1:]], cell=cell, cwd=str(cwd), env=env, stdin=subprocess.DEVNULL,
                     stdout=out, stderr=subprocess.STDOUT, creationflags=mh.DETACHED,
                     start_new_session=os.name != "nt", name=f"{m.id} hub", owner=mh.OWNER)
    except OSError:
        cell.close()
        raise
    if own:
        mh._hub_cells[m.id] = cell
    return cell


def ensure(*, refresh: bool = False, wait_s: float = 45.0) -> Optional[client.Hub]:
    """The running hub, starting one when none answers and autostart is on. A healthy hub is reused, never doubled."""
    hub = client.find(refresh=refresh)
    if hub:
        _mark(True)
        return hub
    if not autostart_enabled() or _sandboxed():
        return None
    # A hub already runs for this home (still starting, or one this process cannot reach): never start a second one.
    running = guard.hub_processes(client.mm_home())
    if running:
        deadline = time.time() + wait_s
        while time.time() < deadline:
            hub = client.find(refresh=True)
            if hub:
                _mark(True)
                return hub
            time.sleep(1.0)
        why = guard.virtualized(client.mm_home())
        _state["last_error"] = (f"a hub (pid {', '.join(map(str, running))}) already runs for {client.mm_home()} but this process "
                                f"cannot reach it" + (f": {why}" if why else " (its control file is missing or stale)"))
        logger.warning("ModelMistress: %s - not starting a second hub", _state["last_error"])
        return None
    with _lock:
        if time.time() - _state["last_start"] < 5:        # a start is already in flight
            return None
        _state["last_start"], _state["starts"] = time.time(), _state["starts"] + 1
    try:
        _start()
        _state["last_error"] = ""
    except Exception as e:  # noqa: BLE001
        _state["last_error"] = str(e)[:300]
        logger.warning("could not start the ModelMistress hub: %s", e)
        return None
    deadline = time.time() + wait_s
    while time.time() < deadline:
        hub = client.find(refresh=True)
        if hub:
            _mark(True)
            return hub
        time.sleep(0.5)
    _state["last_error"] = f"the hub did not answer within {wait_s:.0f}s (see hub.log in {client.mm_home()})"
    return None


def start_transient(wait_s: float = 45.0) -> tuple[Optional[client.Hub], str, Optional[Callable[[], None]]]:
    """A hub for one short job while nothing else keeps one up (the pipeline's diff-lint runs after it stopped live ABP, whose
    hub went with it): (hub, "", stop) with a hub this call started, else (None, why not, None). The same guards as ensure():
    never in a sandbox or with autostart off, never a second hub for the home (one still starting or one this process cannot
    reach counts), never from a virtualized view of the home, and a hub without an inference engine is stopped and refused.
    Its cell is kill-on-close, not a daemon's: `stop()` ends it (graceful, then the whole tree), and so does this process's
    exit. Never raises."""
    try:
        if not autostart_enabled():
            return None, "modelmistress.autostart is off", None
        why = _sandboxed()
        if why:
            return None, f"a sandboxed instance never starts a hub ({why})", None
        home = client.mm_home()
        running = guard.hub_processes(home)
        if running:
            why = guard.virtualized(home)
            return None, (f"a hub (pid {', '.join(map(str, running))}) already runs for {home} but this process cannot reach it"
                          + (f": {why}" if why else "")), None
        why = guard.virtualized(home)
        if why:
            return None, f"not starting a hub from here: {why}", None
        from dataclasses import replace

        from bot.modules import harness as mh
        from bot.sandbox_ns.cell import new_cell
        from bot.sandbox_ns.policy import policy_for

        policy = replace(policy_for("daemon"), name="transient-hub", persistent=False, kill_on_close=True)
        cell = new_cell("daemon", name=f"{client.MODULE_ID} hub (transient)", owner=mh.OWNER, policy=policy)
        try:
            _start(cell)
        except Exception as exc:  # noqa: BLE001
            cell.kill("the transient hub did not start")
            return None, f"could not start a hub: {exc}"[:300], None
    except Exception as exc:  # noqa: BLE001 - a guard that cannot be asked is not a go
        return None, f"could not start a hub ({type(exc).__name__}: {exc})"[:300], None
    hub: Optional[client.Hub] = None
    stopped = threading.Event()

    def stop() -> None:
        if stopped.is_set():
            return
        stopped.set()
        if hub is not None:
            try:
                from bot.modules import registry

                m = registry.get(client.MODULE_ID)
                path = (m.hub.api_base + m.hub.stop) if m.hub and m.hub.stop else "/v1/service/stop"
                client.request("POST", path, body={}, hub=hub, timeout=10)
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline and client.find(refresh=True) is not None:
                    time.sleep(0.25)
            except Exception as exc:  # noqa: BLE001 - the cell below stops it anyway
                logger.info("ModelMistress: graceful stop of the transient hub failed: %s", exc)
        cell.kill("the transient hub's job is done")
        client.forget()

    deadline = time.monotonic() + max(1.0, wait_s)
    while time.monotonic() < deadline:
        hub = client.find(refresh=True)
        if hub is not None:
            break
        time.sleep(0.5)
    if hub is None:
        stop()
        return None, f"the hub it started did not answer within {wait_s:.0f}s (see hub.log in {client.mm_home()})", None
    problem = guard.engine_problem(hub)
    if problem:
        stop()
        return None, problem, None
    logger.info("ModelMistress: started a transient hub at %s", hub.url)
    return hub, "", stop


def tick() -> None:
    """One watchdog pass: note up/down; when down, autostart is on and the back-off allows, (re)start the hub."""
    hub = client.find(refresh=True)
    if hub:
        with _lock:
            _state["backoff_s"], _state["next_try"] = BACKOFF_START_S, 0.0
            fresh = _state.get("engine_checked") != (hub.url, hub.version)
        _mark(True)
        if fresh:                                          # once per hub (a restart has a new url): can it serve models?
            problem = guard.engine_problem(hub)
            with _lock:
                _state["engine_checked"], _state["engine_problem"] = (hub.url, hub.version), problem or ""
            if problem:
                logger.warning("ModelMistress: %s", problem)
        return
    _mark(False)
    if not autostart_enabled() or _sandboxed() or time.time() < _state["next_try"]:
        return
    with _lock:
        _state["next_try"] = time.time() + _state["backoff_s"]
        _state["backoff_s"] = min(_state["backoff_s"] * 2, BACKOFF_MAX_S)
    if ensure(refresh=True):
        _mark(True)
    else:
        logger.warning("ModelMistress hub is down and could not be restarted (%s)", _state["last_error"])


async def run_forever(stop_event: asyncio.Event) -> None:
    """Started with the app: starts the hub at boot (when none runs), then watches it."""
    while not stop_event.is_set():
        try:
            await asyncio.to_thread(tick)
        except Exception:  # noqa: BLE001
            logger.exception("ModelMistress watchdog pass failed")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=CHECK_S)
        except asyncio.TimeoutError:
            pass


def status() -> dict:
    hub = client.find()
    with _lock:
        s = dict(_state)
    return {"up": hub is not None, "url": hub.url if hub else None, "version": hub.version if hub else None,
            "autostart": autostart_enabled(), "home": str(client.mm_home()),
            "last_restart": s["last_start"] or None, "restarts": s["starts"], "last_error": s["last_error"] or None,
            "since": s["since"] or None, "degraded": s.get("engine_problem") or None}
