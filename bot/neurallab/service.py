"""The Neural Lab inside ABP: telemetry recorded, system models adopted and retrained, and an overview for pages.

    run_forever(stop_event)   ABP's background task: the telemetry recorder (every 10 s), systune.tick every 5
                              minutes (adopt finished system-model runs; retrain when their data has grown)
    overview()                runs, designs, the projects (BrainBuilder, KotMoE, Amethyst, Kestrion), system models
"""
from __future__ import annotations

import asyncio
import logging
import time

from bot.neurallab import interop, lab, spec, systune, telemetry

logger = logging.getLogger(__name__)
_recorder: telemetry.Recorder | None = None


def settings() -> dict:
    from bot.localai import engine
    st = engine.settings()
    return {"telemetry": st.get("lab_telemetry", True), "telemetry_interval_s": st.get("lab_telemetry_interval_s", 10),
            "auto_retrain": st.get("lab_auto_retrain", True)}


def overview() -> dict:
    return {"runs": lab.runs(30), "designs": lab.designs(), "projects": interop.projects(), "ops": spec.ops_catalog(),
            "systune": systune.status(), "settings": settings(), "recording": bool(_recorder and _recorder.thread
                                                                                    and _recorder.thread.is_alive())}


def start_recorder() -> None:
    global _recorder
    st = settings()
    if not st["telemetry"]:
        return
    if _recorder is None:
        _recorder = telemetry.Recorder(float(st["telemetry_interval_s"]))
    _recorder.start()


async def run_forever(stop_event: asyncio.Event) -> None:
    try:
        await asyncio.wait_for(stop_event.wait(), timeout=40)
        return
    except asyncio.TimeoutError:
        pass
    await asyncio.to_thread(start_recorder)
    last = 0.0
    while not stop_event.is_set():
        if time.time() - last >= 300 and settings()["auto_retrain"]:
            last = time.time()
            try:
                for msg in await asyncio.to_thread(systune.tick, logger.info):
                    logger.info("system models: %s", msg)
            except Exception:  # noqa: BLE001
                logger.exception("system models tick failed")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=60)
        except asyncio.TimeoutError:
            pass
    if _recorder:
        _recorder.stop()
