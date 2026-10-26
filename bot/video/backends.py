"""Render backends: one interface, chosen per workflow template (`"backend": ...`).

    sdcpp    stable-diffusion.cpp's sd-cli on Vulkan (the route V0 proved). GGML_VK_DISABLE_COOPMAT=1 is always set: with
             KHR_coopmat on, sd.cpp dies on the first diffusion step on AMD's driver.
    fake     a test pattern (ffmpeg lavfi testsrc, or OpenCV when ffmpeg is missing): no model, no GPU. For tests and
             for checking the queue, library and provenance end to end.
    comfyui  reserved: ComfyUI's HTTP API (graph JSON in, files out). Not built yet; says so.

A backend gets a RenderContext and returns the output file. It reports progress through ctx.progress, stops when
ctx.cancelled() turns true, and must not run longer than ctx.timeout_s. `needs_gpu` tells the queue whether to take
the GPU lock and make room in VRAM first."""
from __future__ import annotations

import os
import re
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

SDCPP_ENV = {"GGML_VK_DISABLE_COOPMAT": "1"}


class RenderError(RuntimeError):
    pass


class Cancelled(RenderError):
    pass


@dataclass
class RenderContext:
    job_id: str
    workflow: dict
    params: dict
    workdir: Path
    timeout_s: float = 3600.0
    progress: Callable[[float, str], None] = lambda f, m: None
    cancelled: Callable[[], bool] = lambda: False
    log: list = field(default_factory=list)


class Backend:
    name = ""
    title = ""
    needs_gpu = True

    def available(self) -> tuple[bool, str]:
        return False, "not implemented"

    def render(self, ctx: RenderContext) -> Path:
        raise RenderError(f"{self.name}: render is not implemented")

    def describe(self) -> dict:
        ok, why = self.available()
        return {"name": self.name, "title": self.title, "needs_gpu": self.needs_gpu, "available": ok, "why": why}


class FakeBackend(Backend):
    """A labelled test pattern; `frame_delay` (seconds per frame) lets tests hold a job in `rendering`."""
    name = "fake"
    title = "Test pattern (no model, no GPU)"
    needs_gpu = False
    frame_delay = 0.0

    def available(self) -> tuple[bool, str]:
        return True, ""

    def render(self, ctx: RenderContext) -> Path:
        from bot.video import media
        p = ctx.params
        out = ctx.workdir / "output.webm"
        if media.ffmpeg_path() and not self.frame_delay:
            ctx.progress(0.1, "ffmpeg testsrc")
            media.testsrc(out, seconds=p["seconds"], fps=p["fps"], width=p["width"], height=p["height"],
                          text=f"ABP test {ctx.job_id}")
            ctx.progress(1.0, "done")
            return out
        return self._opencv(ctx, out)

    def _opencv(self, ctx: RenderContext, out: Path) -> Path:
        import cv2
        import numpy as np
        p = ctx.params
        w, h, fps = p["width"], p["height"], p["fps"]
        n = max(1, int(round(p["seconds"] * fps)))
        wr = cv2.VideoWriter(str(out), cv2.VideoWriter_fourcc(*"VP90"), fps, (w, h))
        if not wr.isOpened():
            raise RenderError("could not open the WebM writer")
        bars = np.array([[192, 192, 192], [0, 192, 192], [192, 192, 0], [0, 192, 0], [192, 0, 192], [0, 0, 192],
                         [192, 0, 0]], np.uint8)
        start = time.monotonic()
        try:
            for i in range(n):
                if ctx.cancelled():
                    raise Cancelled("cancelled")
                if time.monotonic() - start > ctx.timeout_s:
                    raise RenderError(f"timed out after {int(ctx.timeout_s)} s")
                img = np.repeat(np.repeat(bars[None, :, :], h, 0), max(1, w // len(bars) + 1), 1)[:, :w].copy()
                x = int((i / max(1, n - 1)) * (w - 20))
                img[h // 3: h // 3 + 20, x: x + 20] = 255
                cv2.putText(img, f"{i + 1}/{n}", (8, 24), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 1, cv2.LINE_AA)
                wr.write(img)
                ctx.progress((i + 1) / n, f"frame {i + 1}/{n}")
                if self.frame_delay:
                    time.sleep(self.frame_delay)
        finally:
            wr.release()
        return out


_STEP = re.compile(r"\|\s*(\d+)\s*/\s*(\d+)\b")


def parse_progress(line: str) -> tuple[int, int] | None:
    """sd.cpp prints its sampler progress as `|=====>     | 3/20 - 12.31s/it`."""
    m = _STEP.search(line)
    if not m:
        return None
    a, b = int(m.group(1)), int(m.group(2))
    return (a, b) if b > 0 and a <= b else None


class SdCppBackend(Backend):
    name = "sdcpp"
    title = "stable-diffusion.cpp (Vulkan)"
    needs_gpu = True

    def binary(self) -> str:
        from bot.video import jobs
        return str(jobs.settings().get("sdcpp_bin") or "").strip()

    def available(self) -> tuple[bool, str]:
        b = self.binary()
        if not b:
            return False, "set the path of sd-cli (stable-diffusion.cpp, Vulkan build) in the Video Studio settings"
        if not Path(b).is_file():
            return False, f"{b}: no such file"
        return True, ""

    def command(self, ctx: RenderContext, files: dict[str, str], out: Path) -> list[str]:
        from bot.video import workflows
        return [self.binary(), *workflows.render_args(ctx.workflow, ctx.params, files, str(out))]

    @staticmethod
    def env() -> dict:
        return {**os.environ, **SDCPP_ENV}

    def render(self, ctx: RenderContext) -> Path:
        from bot.video import registry
        ok, why = self.available()
        if not ok:
            raise RenderError(why)
        ctx.progress(0.0, "verifying the model files")
        files = registry.resolve(ctx.workflow["model"]) if ctx.workflow.get("model") else {}
        out = ctx.workdir / "output.webm"
        cmd = self.command(ctx, files, out)
        ctx.progress(0.01, "starting sd.cpp")
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace",
                                cwd=str(ctx.workdir), env=self.env(),
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        stop = threading.Event()

        def watch() -> None:            # cancel and timeout, while the reader blocks on output
            start = time.monotonic()
            while not stop.wait(1.0):
                if ctx.cancelled() or time.monotonic() - start > ctx.timeout_s:
                    proc.kill()
                    return
        threading.Thread(target=watch, daemon=True).start()
        start = time.monotonic()
        try:
            assert proc.stdout is not None
            for line in proc.stdout:
                line = line.rstrip()
                if line:
                    ctx.log.append(line)
                    del ctx.log[:-200]
                got = parse_progress(line)
                if got:
                    ctx.progress(0.02 + 0.93 * got[0] / got[1], f"step {got[0]}/{got[1]}")
            rc = proc.wait()
        finally:
            stop.set()
        if ctx.cancelled():
            raise Cancelled("cancelled")
        if time.monotonic() - start > ctx.timeout_s:
            raise RenderError(f"timed out after {int(ctx.timeout_s)} s")
        if rc != 0 or not out.is_file():
            tail = " | ".join(ctx.log[-5:])
            raise RenderError(f"sd.cpp exited with {rc}" + (f": {tail}" if tail else ""))
        return out


class ComfyUIBackend(Backend):
    name = "comfyui"
    title = "ComfyUI (not built yet)"
    needs_gpu = True

    def available(self) -> tuple[bool, str]:
        return False, "the ComfyUI backend is not built yet; sd.cpp is the working route"


BACKENDS: dict[str, Backend] = {b.name: b for b in (SdCppBackend(), FakeBackend(), ComfyUIBackend())}


def get(name: str) -> Backend:
    if name not in BACKENDS:
        raise ValueError(f"no backend {name!r}; known: {', '.join(BACKENDS)}")
    return BACKENDS[name]


def listing() -> list[dict]:
    return [b.describe() for b in BACKENDS.values()]
