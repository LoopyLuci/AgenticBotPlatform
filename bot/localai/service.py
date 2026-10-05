"""The local AI runtime inside ABP: its model server kept running, fine-tuning runs exported when they finish, and
ABP's model store made visible to the modules that run models (Kestrion reads it through ABP_MODELS_DIR).

    server_status / server_start / server_stop      the Ollama-compatible server (bot/localai/server.py), a process
                                                     of its own (bot/hosting/procs.py), on settings' port (11436)
    overview()                                       everything a page shows: server, engines, models, running,
                                                     mesh-llm, training environment, runs, GPUs
    run_forever(stop_event)                          ABP's background task (bot/main.py)
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
import time

from bot.localai import engine, mesh, models, train
from bot.localai.paths import LocalAIError, home, sub

logger = logging.getLogger(__name__)


def export_store_env() -> str:
    """ABP_MODELS_DIR for the processes ABP starts (module hubs fill {abp_models} from it)."""
    p = str(models.store_root())
    os.environ["ABP_MODELS_DIR"] = p
    return p


def server_status() -> dict:
    from bot.hosting import procs
    st = engine.settings()
    port = int(st.get("port", 11436))
    s = procs.status("localai", home=home())
    out = {**s, "port": port, "bind": st.get("bind", "127.0.0.1"), "url": f"http://127.0.0.1:{port}"}
    if s["running"]:
        try:
            import httpx
            out["version"] = httpx.get(f"http://127.0.0.1:{port}/api/version", timeout=3).json().get("version")
        except Exception as e:  # noqa: BLE001
            out["error"] = str(e)
    return out


def server_start() -> dict:
    from bot.envfile import CODE_ROOT
    from bot.hosting import procs
    st = engine.settings()
    env = {"PYTHONPATH": str(CODE_ROOT) + os.pathsep + os.environ.get("PYTHONPATH", ""), "ABP_LOCALAI_HOME": str(home())}
    return procs.start("localai", [sys.executable, "-m", "bot.localai.server", "--port", str(st.get("port", 11436)),
                                   "--bind", st.get("bind", "127.0.0.1")], env=env, cwd=str(CODE_ROOT), home=home())


def server_stop() -> bool:
    from bot.hosting import procs
    engine.unload()
    return procs.stop("localai", home=home())


def overview() -> dict:
    try:
        eng = engine.engine()
    except LocalAIError:
        eng = None
    return {"server": server_status(), "settings": engine.settings(), "engine": eng, "engines": engine.installed(),
            "gpus": engine.gpus(), "models": models.listing(), "stores": models.extra_stores(), "running": _running(),
            "mesh": mesh.status(), "home": str(home()), "train_env": {"installed": train.python().exists(), "path": str(train.venv())},
            "runs": train.runs()[:20], "datasets": train.datasets()}


def _running() -> list[dict]:
    """Models loaded in the server process (it owns the runners): asked over its API."""
    st = server_status()
    if not st["running"]:
        return []
    try:
        import httpx
        return httpx.get(f"{st['url']}/api/ps", timeout=5).json().get("models", [])
    except Exception:  # noqa: BLE001
        return []


_last: dict[str, float] = {}


def tick() -> None:
    st = engine.settings()
    now = time.time()
    if st.get("autostart", True) and engine.installed() and not server_status()["running"] and now - _last.get("restart", 0) > 120:
        _last["restart"] = now
        try:
            server_start()
            logger.info("local AI server started on port %s", st.get("port", 11436))
        except Exception as e:  # noqa: BLE001
            logger.warning("could not start the local AI server: %s", e)
    try:
        for r in train.tick(log=logger.info):           # finished fine-tunes: GGUF, quantized, into the store
            logger.info("fine-tune exported: %s", r.get("name") or r)
    except Exception:  # noqa: BLE001
        logger.exception("exporting fine-tuning runs failed")


async def run_forever(stop_event: asyncio.Event) -> None:
    export_store_env()
    sub("logs")
    try:
        await asyncio.wait_for(stop_event.wait(), timeout=30)
        return
    except asyncio.TimeoutError:
        pass
    while not stop_event.is_set():
        try:
            await asyncio.to_thread(tick)
        except Exception:  # noqa: BLE001
            logger.exception("local AI tick failed")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=60)
        except asyncio.TimeoutError:
            pass
