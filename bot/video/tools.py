"""The agent's Video Studio tools (bot/video; renders on this machine, queued one at a time).

    video_generate   queue a clip from a prompt (asks first: it takes the GPU for minutes to hours)
    video_job        one job: status, progress and, when done, its clip (waits up to wait_s for it to finish)
    video_jobs       the recent jobs
    video_cancel     cancel a queued or running job (asks first)
    video_library    the clips made so far, or one clip with its lineage and provenance
    video_status     settings, workflows, backends, models (on disk and verified or not) and ffmpeg

A finished clip's `url` is a dashboard path (/api/video/assets/<id>/file) the chat can play."""
from __future__ import annotations

import asyncio
import json
import time
from typing import Any

from bot.video.views import asset_view, job_view

MAX_OUT = 12_000
WAIT_MAX_S = 120


def _out(value: Any) -> str:
    text = json.dumps(value, indent=1, default=str)
    return text if len(text) <= MAX_OUT else text[:MAX_OUT] + f"\n... ({len(text) - MAX_OUT} more characters)"


def _generate(inp: dict) -> dict:
    from bot.video import jobs
    params = dict(inp.get("params") or {})
    for k in ("prompt", "negative", "seconds", "fps", "width", "height", "steps", "seed", "image"):
        if inp.get(k) not in (None, ""):
            params.setdefault(k, inp[k])
    body = {"params": params}
    for k in ("workflow", "parent_id", "timeout_s"):
        if inp.get(k) not in (None, ""):
            body[k] = inp[k]
    j = jobs.submit(body, actor="agent")
    return {"queued": job_view(j), "next": "call video_job with this id (wait_s up to 120) to follow it"}


def _wait(job_id: str, wait_s: float) -> dict:
    from bot.video import jobs
    deadline = time.monotonic() + max(0.0, min(float(wait_s or 0), WAIT_MAX_S))
    while True:
        j = jobs.job(job_id)
        if not j:
            raise ValueError(f"no video job {job_id}")
        if j["status"] in jobs.FINAL or time.monotonic() >= deadline:
            return job_view(j)  # type: ignore[return-value]
        time.sleep(1.0)


def _library(inp: dict) -> Any:
    from bot.video import library
    aid = str(inp.get("asset_id") or "")
    if aid:
        a = library.get(aid)
        if not a:
            raise ValueError(f"no asset {aid}")
        return {"asset": asset_view(a), **library.provenance_of(aid)}
    return [asset_view(a) for a in library.listing(int(inp.get("limit") or 20))]


def _status() -> dict:
    from bot.video import backends, jobs, media, registry, workflows
    return {"settings": jobs.settings(), "queue": jobs.summary(), "backends": backends.listing(), "ffmpeg": media.status(),
            "workflows": [{k: w[k] for k in ("ref", "title", "backend", "model", "vram_gb")} for w in workflows.listing()],
            "models": [{k: m[k] for k in ("id", "state", "bytes", "vram_gb")} for m in registry.status()]}


async def _call(fn, *args) -> str:
    try:
        return _out(await asyncio.to_thread(fn, *args))
    except ValueError as e:
        return f"Error: {e}"
    except Exception as e:  # noqa: BLE001 - reaches the agent as text
        return f"Error: {type(e).__name__}: {e}"


def register_tools() -> None:
    from bot.agent_runtime import toolspec

    def reg(name, description, props, required, handler, *, read_only=True, needs_approval=None):
        toolspec.register(
            {"name": name, "description": description,
             "input_schema": {"type": "object", "properties": props, "required": required}},
            toolspec.ToolSpec(name, "read" if read_only else "execute", read_only=read_only,
                              concurrency_safe=read_only, origin="registered", needs_approval=needs_approval),
            handler)

    async def generate(inp, **_):
        return await _call(_generate, inp)

    async def one(inp, **_):
        return await _call(_wait, str(inp.get("job_id") or ""), inp.get("wait_s") or 0)

    async def many(inp, **_):
        from bot.video import jobs
        return await _call(lambda: [job_view(j) for j in jobs.jobs(int(inp.get("limit") or 20), str(inp.get("status") or ""))])

    async def cancel(inp, **_):
        from bot.video import jobs

        def go():
            j = jobs.cancel(str(inp.get("job_id") or ""), actor="agent")
            if not j:
                raise ValueError("no such video job")
            return job_view(j)
        return await _call(go)

    async def lib(inp, **_):
        return await _call(_library, inp)

    async def status(inp, **_):
        return await _call(_status)

    num = {"type": "number"}
    reg("video_generate", "Queue a video clip rendered on this machine from a text prompt (optionally a start image). "
        "Jobs run one at a time and take the GPU; the result goes into the video library with its provenance. "
        "Returns the job; follow it with video_job. workflow: a template id from video_status (default the configured "
        "one; 'testsrc' makes a test pattern without a model). seconds is capped by the settings.",
        {"prompt": {"type": "string"}, "workflow": {"type": "string"}, "negative": {"type": "string"},
         "seconds": num, "fps": {"type": "integer"}, "width": {"type": "integer"}, "height": {"type": "integer"},
         "steps": {"type": "integer"}, "seed": {"type": "integer"}, "image": {"type": "string", "description": "start image path"},
         "parent_id": {"type": "string", "description": "the library asset this clip derives from (lineage)"},
         "params": {"type": "object", "description": "any other workflow parameters"}},
        ["prompt"], generate, read_only=False, needs_approval=True)
    reg("video_job", "One video job: status (queued, waiting_gpu, rendering, done, failed, cancelled), progress 0..1, "
        "the message and, when done, the clip (asset with url). wait_s (up to 120) waits for it to finish.",
        {"job_id": {"type": "string"}, "wait_s": num}, ["job_id"], one)
    reg("video_jobs", "The recent video jobs, newest first (status filters).",
        {"limit": {"type": "integer"}, "status": {"type": "string"}}, [], many)
    reg("video_cancel", "Cancel a queued or running video job.", {"job_id": {"type": "string"}}, ["job_id"], cancel,
        read_only=False, needs_approval=True)
    reg("video_library", "The video library: recent clips (url, prompt, seed, model, workflow), or with asset_id one "
        "clip with its lineage (parents) and provenance manifest.",
        {"asset_id": {"type": "string"}, "limit": {"type": "integer"}}, [], lib)
    reg("video_status", "Video Studio on this machine: whether it is on, the queue, the render backends and whether "
        "each is ready, the workflow templates, the models (downloaded and verified or not) and ffmpeg.", {}, [], status)


register_tools()
