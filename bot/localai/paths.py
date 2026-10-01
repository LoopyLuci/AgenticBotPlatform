"""Where the local AI runtime keeps things.

    <home>/models/blobs/sha256-<hex>      model files pulled or imported (content-addressed: one copy per content)
    <home>/models/manifests/<name>/<tag>.json   what a model name is: its weights, projector, adapters, template,
                                          system prompt, parameters, and where each file lives (a blob, or a
                                          referenced path elsewhere)
    <home>/engines/<build>/               llama.cpp builds (llama-server, llama-quantize, ...)
    <home>/train/<run>/                   fine-tuning runs (data, checkpoints, logs, exports)
    <home>/venv-train/                    the training environment (PyTorch for the GPU)
    <home>/state.json, logs/, run/        settings, process logs and pid files

<home> is ABP_LOCALAI_HOME, else E:\\ABP-LocalAI where an E: drive exists (this project's fast drive), else
<data>/localai.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

CPU_THREADS = 4          # cap for llama.cpp's CPU work and quantizing (see the package docstring)


def home() -> Path:
    env = os.environ.get("ABP_LOCALAI_HOME", "").strip()
    if env:
        p = Path(env)
    elif sys.platform == "win32" and Path("E:/").exists():
        p = Path("E:/ABP-LocalAI")
    else:
        from bot.envfile import PROJECT_ROOT
        p = PROJECT_ROOT / "data" / "localai"
    p.mkdir(parents=True, exist_ok=True)
    return p


def sub(*parts: str) -> Path:
    p = home().joinpath(*parts)
    p.mkdir(parents=True, exist_ok=True)
    return p


def cpu_threads() -> int:
    """CPU threads for ABP's heavy work right now: CPU_THREADS, fewer while the stability policy sees risk
    (bot/neurallab/systune.py: recent machine-check errors, unexpected power losses, an unusual load pattern)."""
    try:
        from bot.neurallab import systune
        return max(1, min(CPU_THREADS, int(systune.cpu_policy()["threads"])))
    except Exception:                                     # noqa: BLE001 - no policy: the fixed cap
        return CPU_THREADS


def guard(pid: int) -> None:
    """Keep one of ABP's heavy child processes off processors with a machine-check history (no-op otherwise)."""
    try:
        from bot.neurallab import systune
        systune.guard_process(pid)
    except Exception:                                     # noqa: BLE001
        pass


class LocalAIError(Exception):
    """A problem a person can act on; shown as is (and as Ollama-style {"error": ...} on the API)."""
