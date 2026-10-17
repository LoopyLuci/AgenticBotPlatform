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
from typing import Optional

from bot.modelmistress import client

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


def _start() -> None:
    """Spawn the module's hub detached, with --home at the shared MM home."""
    from bot.modules import harness as mh
    from bot.modules import registry
    from bot.sandbox_ns.cell import new_cell
    from bot.sandbox_ns.spawn import spawn as ns_spawn

    m = registry.get(client.MODULE_ID)
    if m.hub is None or not m.hub.start:
        raise RuntimeError("the ModelMistress module has no hub to start")
    cmd = registry.expand_cmd(m, m.hub.start)
    home = client.mm_home()
    if os.path.isabs(cmd[0]) and not os.path.exists(cmd[0]):
        raise RuntimeError(f"ModelMistress is not built yet ({cmd[0]} is missing)")
    home.mkdir(parents=True, exist_ok=True)
    if "--home" in cmd:
        cmd[cmd.index("--home") + 1] = str(home)
    else:
        cmd[1:1] = ["--home", str(home)]
    exe = mh._which(cmd[0]) or cmd[0]
    env = {**os.environ, "ABP_MODULE_ID": m.id, "ABP_MODULE_DATA": str(home), **mh._modkit_env(), **mh.abp_env(m)}
    cell = new_cell("daemon", name=f"{m.id} hub", owner=mh.OWNER)
    try:
        with open(home / "hub.log", "ab") as out:
            ns_spawn([exe, *cmd[1:]], cell=cell, cwd=str(mh._workspace(m)), env=env, stdin=subprocess.DEVNULL,
                     stdout=out, stderr=subprocess.STDOUT, creationflags=mh.DETACHED,
                     start_new_session=os.name != "nt", name=f"{m.id} hub", owner=mh.OWNER)
    except OSError:
        cell.close()
        raise
    mh._hub_cells[m.id] = cell


def ensure(*, refresh: bool = False, wait_s: float = 45.0) -> Optional[client.Hub]:
    """The running hub, starting one when none answers and autostart is on. A healthy hub is reused, never doubled."""
    hub = client.find(refresh=refresh)
    if hub:
        _mark(True)
        return hub
    if not autostart_enabled() or _sandboxed():
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


def tick() -> None:
    """One watchdog pass: note up/down; when down, autostart is on and the back-off allows, (re)start the hub."""
    if client.find(refresh=True):
        with _lock:
            _state["backoff_s"], _state["next_try"] = BACKOFF_START_S, 0.0
        _mark(True)
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
            "since": s["since"] or None}
