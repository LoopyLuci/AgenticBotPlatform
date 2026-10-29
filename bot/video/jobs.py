"""The durable video job queue (SQLite, data/video/video.db).

    queued -> waiting_gpu -> rendering -> done | failed | cancelled

Jobs run one at a time on one worker thread, oldest first. A job whose backend needs the GPU first takes the machine's
GPU lock (custom:gpu0 on the coordination board, the lock every ABP GPU user takes) and makes room in VRAM: when the
measured free VRAM is short of the workflow's need and `video.unload_mm` is on, the models ModelMistress has loaded are
unloaded for the render and loaded again afterwards. While the lock is held elsewhere or VRAM stays short, the job
waits in `waiting_gpu` (and can be cancelled there). A process lock makes two GPU jobs at once impossible even if two
workers were ever started.

After a restart, jobs left in `waiting_gpu` or `rendering` are queued again (`video.resume`, on by default; at most
MAX_ATTEMPTS starts) or failed with a plain reason. Every job has a timeout (its budget), capped by
`video.job_timeout_s`. Every submit, refusal, finish, failure and cancel goes to ABP's audit log."""
from __future__ import annotations

import json
import logging
import shutil
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Optional

from bot.video import backends, store, workflows

logger = logging.getLogger("bot.video.jobs")

DEFAULTS: dict[str, Any] = {
    "enabled": False,               # the whole Video Studio engine (off until switched on in the Workspace)
    "default_workflow": "wan22-ti2v-5b",
    "sdcpp_bin": "",                # path of sd-cli (stable-diffusion.cpp, Vulkan build)
    "models_dir": "",               # where model files live (default data/video/models)
    "ffmpeg": "",                   # path of ffmpeg (default: $ABP_FFMPEG, then PATH)
    "ffmpeg_sha256": "",            # pin: ffmpeg must hash to this
    "unload_mm": True,              # unload ModelMistress's models when VRAM is short, reload after
    "visible_label": True,          # burn "AI-GENERATED | <id>" into every frame
    "resume": True,                 # requeue jobs a restart interrupted
    "job_timeout_s": 3600,          # the longest a job may render
    "max_seconds": 10,              # the longest clip a job may ask for
}
BOOLS = ("enabled", "unload_mm", "visible_label", "resume")
TEXTS = ("default_workflow", "sdcpp_bin", "models_dir", "ffmpeg", "ffmpeg_sha256")
LIMITS = {"job_timeout_s": (60, 86400), "max_seconds": (1, 120)}

ACTIVE = ("queued", "waiting_gpu", "rendering")
FINAL = ("done", "failed", "cancelled")
MAX_ATTEMPTS = 3
GPU_RESOURCE = "custom:gpu0"
AGENT = "abp-video"
GPU_RETRY_S = 15.0

_gpu_process_lock = threading.Lock()
_cancel: set[str] = set()
_wake = threading.Event()
_worker: Optional[threading.Thread] = None
_worker_stop = threading.Event()
_worker_lock = threading.Lock()
_recovered: set[str] = set()


def settings() -> dict:
    out = dict(DEFAULTS)
    try:
        from bot.config import config
        got = config.current.get("video") or {}
    except Exception:  # noqa: BLE001
        got = {}
    if isinstance(got, dict):
        out.update({k: got[k] for k in DEFAULTS if k in got})
    return out


def check_settings(body: dict) -> dict:
    """The changes in `body`, validated; raises ValueError."""
    changes: dict[str, Any] = {}
    for k, v in (body or {}).items():
        if k in BOOLS:
            if not isinstance(v, bool):
                raise ValueError(f"{k} must be true or false")
        elif k in TEXTS:
            if not isinstance(v, str) or len(v) > 1024:
                raise ValueError(f"{k} must be text")
            v = v.strip()
            if k == "default_workflow" and v:
                workflows.get(v)
            if k == "ffmpeg_sha256" and v and (len(v) != 64 or any(c not in "0123456789abcdefABCDEF" for c in v)):
                raise ValueError("ffmpeg_sha256 must be 64 hex characters (or empty)")
        elif k in LIMITS:
            lo, hi = LIMITS[k]
            if isinstance(v, bool) or not isinstance(v, int) or not lo <= v <= hi:
                raise ValueError(f"{k} must be a whole number from {lo} to {hi}")
        else:
            raise ValueError(f"unknown setting {k}")
        changes[k] = v
    if not changes:
        raise ValueError("send at least one setting")
    return changes


def _audit(action: str, detail: str, actor: str = "video") -> None:
    try:
        from bot import db
        db.log_audit(actor=actor or "video", action=action, detail=detail[:1000])
    except Exception:  # noqa: BLE001 - the job row still has it
        pass


# ---- rows ---------------------------------------------------------------------------------------------------------
def _pub(j: dict | None) -> dict | None:
    if j is None:
        return None
    j["params"] = json.loads(j["params"]) if isinstance(j["params"], str) else j["params"]
    j["cancel"] = bool(j.get("cancel"))
    return j


def job(job_id: str) -> Optional[dict]:
    with store.connect() as con:
        j = _pub(store.row(con.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()))
    if j and j.get("asset_id"):
        from bot.video import library
        j["asset"] = library.get(j["asset_id"])
    return j


def jobs(limit: int = 50, status: str = "") -> list[dict]:
    with store.connect() as con:
        if status:
            rows = con.execute("SELECT * FROM jobs WHERE status=? ORDER BY created DESC LIMIT ?", (status, limit)).fetchall()
        else:
            rows = con.execute("SELECT * FROM jobs ORDER BY created DESC LIMIT ?", (limit,)).fetchall()
    return [_pub(store.row(r)) for r in rows]   # type: ignore[misc]


def _update(job_id: str, **fields: Any) -> None:
    if not fields:
        return
    with store.connect() as con:
        con.execute(f"UPDATE jobs SET {', '.join(f'{k}=?' for k in fields)} WHERE id=?", (*fields.values(), job_id))


# ---- submit / cancel ----------------------------------------------------------------------------------------------
def submit(body: dict, actor: str = "dashboard") -> dict:
    """Queue a job: {workflow?, params: {...}, parent_id?, timeout_s?}. Raises ValueError for a bad request."""
    s = settings()
    if not s["enabled"]:
        raise ValueError("Video Studio is switched off; switch it on in the Workspace first")
    body = dict(body or {})
    wf = workflows.get(str(body.get("workflow") or s["default_workflow"]))
    backend = backends.get(wf["backend"])
    params = workflows.validate(wf, body.get("params") or {})
    if "seconds" in params and params["seconds"] > s["max_seconds"]:
        raise ValueError(f"clips are capped at {s['max_seconds']} s (video.max_seconds)")
    if params.get("image"):
        if not Path(params["image"]).expanduser().is_file():
            raise ValueError(f"image {params['image']}: no such file")
    parent = str(body.get("parent_id") or "") or None
    if parent:
        from bot.video import library
        if not library.get(parent):
            raise ValueError(f"no parent asset {parent}")
    t = body.get("timeout_s", s["job_timeout_s"])
    if isinstance(t, bool) or not isinstance(t, int) or not 10 <= t <= s["job_timeout_s"]:
        raise ValueError(f"timeout_s must be a whole number from 10 to {s['job_timeout_s']}")
    jid = "vj_" + uuid.uuid4().hex[:12]
    with store.connect() as con:
        con.execute("INSERT INTO jobs (id, status, workflow, backend, params, parent_id, actor, timeout_s, created, message)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (jid, "queued", workflows.ref(wf), backend.name, json.dumps(params), parent, actor[:80], t,
                     store.now(), "queued"))
    _audit("video_job_queued", f"{jid} {workflows.ref(wf)} by {actor}: {str(params.get('prompt', ''))[:200]}", actor)
    ensure_worker()
    _wake.set()
    return job(jid)  # type: ignore[return-value]


def cancel(job_id: str, actor: str = "dashboard") -> Optional[dict]:
    j = job(job_id)
    if not j:
        return None
    if j["status"] in FINAL:
        return j
    _cancel.add(job_id)
    with store.connect() as con:
        con.execute("UPDATE jobs SET cancel=1 WHERE id=?", (job_id,))
        if j["status"] == "queued":
            con.execute("UPDATE jobs SET status='cancelled', finished=?, message='cancelled before it started' "
                        "WHERE id=? AND status='queued'", (store.now(), job_id))
    _audit("video_job_cancel", f"{job_id} by {actor}", actor)
    _wake.set()
    return job(job_id)


def _cancelled(job_id: str) -> bool:
    if job_id in _cancel:
        return True
    with store.connect() as con:
        r = con.execute("SELECT cancel FROM jobs WHERE id=?", (job_id,)).fetchone()
    return bool(r and r["cancel"])


# ---- restart recovery ---------------------------------------------------------------------------------------------
def recover() -> list[str]:
    """Jobs a restart left half-done: queued again (resume on, attempts left) or failed, plainly. Once per database."""
    key = str(store.db_path())
    if key in _recovered:
        return []
    _recovered.add(key)
    resume = settings()["resume"]
    out = []
    with store.connect() as con:
        for r in con.execute("SELECT id, status, attempts FROM jobs WHERE status IN ('waiting_gpu','rendering')").fetchall():
            if resume and r["attempts"] < MAX_ATTEMPTS:
                con.execute("UPDATE jobs SET status='queued', progress=0, message=? WHERE id=?",
                            (f"resumed after a restart (it was {r['status']})", r["id"]))
            else:
                why = "interrupted by a restart" + ("" if resume else " (resume is off)") + \
                      (f"; {r['attempts']} starts already" if resume else "")
                con.execute("UPDATE jobs SET status='failed', finished=?, error=?, message=? WHERE id=?",
                            (store.now(), why, why, r["id"]))
            out.append(r["id"])
    for jid in out:
        _audit("video_job_recovered", jid)
    return out


# ---- the GPU gate -------------------------------------------------------------------------------------------------
class Wait(Exception):
    """The GPU is not free yet; the reason is shown on the job."""


def _board_take(lease_s: float) -> Callable[[], None]:
    from bot import coordination
    from bot.coordination.model import BoardError
    try:
        board = coordination.get_board()
        res = board.acquire(AGENT, [GPU_RESOURCE], task=None, lease_s=lease_s)
    except BoardError as exc:
        raise Wait(f"the GPU lock {GPU_RESOURCE} is held ({exc})") from exc
    except Exception as exc:  # noqa: BLE001 - a board that cannot be asked is not a free GPU
        raise Wait(f"the GPU lock {GPU_RESOURCE} could not be checked ({type(exc).__name__}: {exc})") from exc
    fence = res.get("fence")

    def release() -> None:
        try:
            board.release_locks(AGENT, fence)
        except Exception as exc:  # noqa: BLE001 - the lease runs out on its own
            logger.info("video: releasing %s failed: %s", GPU_RESOURCE, exc)
    return release


def _mm_loaded() -> list[str]:
    try:
        from bot.modelmistress import client, harness
        if client.find() is None:
            return []
        return [m.get("id") or m.get("name") for m in harness.models() if m.get("loaded") and (m.get("id") or m.get("name"))]
    except Exception:  # noqa: BLE001 - no hub, nothing loaded to make room from
        return []


def _mm_unload(name: str) -> None:
    from bot.modelmistress import harness
    harness.unload(name)


def _mm_load(name: str) -> None:
    from bot.modelmistress import harness
    harness.load(name)


def _fits(need_gb: float) -> Optional[bool]:
    try:
        from bot import gpu
        return gpu.can_fit(need_gb, 1.0)
    except Exception:  # noqa: BLE001
        return None


def default_gate(j: dict, wf: dict) -> Callable[[], None]:
    """Take the GPU lock and make room in VRAM; returns release() (which reloads what was unloaded). Raises Wait."""
    import os
    import uuid

    from bot.hwguard import slot

    # The hardware guard's heavy-work slot (bot/hwguard): a render never stacks with a model load, a full test run or a
    # build, and never starts while the guard says pause. Released before the unloaded models are loaded back.
    hw_token = f"{os.getpid()}:{uuid.uuid4().hex[:12]}"
    hw = slot.acquire("render", f"video job {j['id']}", token=hw_token)
    if not hw["granted"]:
        raise Wait(f"hardware guard: {hw['reason']}")
    try:
        release_board = _board_take(float(j["timeout_s"]) + 600.0)
    except BaseException:
        slot.release(token=hw_token)
        raise
    unloaded: list[str] = []
    try:
        need = float(wf.get("vram_gb") or 0)
        if need and _fits(need) is False and settings()["unload_mm"]:
            for name in _mm_loaded():
                _mm_unload(name)
                unloaded.append(name)
            if unloaded:
                _audit("video_mm_unloaded", f"{j['id']}: {', '.join(unloaded)}")
        if need and _fits(need) is False:
            raise Wait(f"not enough free VRAM for {need:g} GB" + ("" if settings()["unload_mm"] else
                                                                   " (unloading ModelMistress models is off)"))
    except BaseException:
        slot.release(token=hw_token)
        _restore(unloaded, j["id"])
        release_board()
        raise

    def release() -> None:
        slot.release(token=hw_token)
        try:
            _restore(unloaded, j["id"])
        finally:
            release_board()
    return release


def _restore(names: list[str], jid: str) -> None:
    for name in names:
        try:
            _mm_load(name)
        except Exception as exc:  # noqa: BLE001 - say so; the next chat request loads it anyway
            logger.warning("video %s: reloading %s failed: %s", jid, name, exc)
            _audit("video_mm_reload_failed", f"{jid}: {name}: {exc}")


GATE: Callable[[dict, dict], Callable[[], None]] = default_gate


# ---- running ------------------------------------------------------------------------------------------------------
def _next_job() -> Optional[dict]:
    with store.connect() as con:
        r = con.execute("SELECT * FROM jobs WHERE status IN ('queued','waiting_gpu') AND cancel=0 "
                        "ORDER BY created LIMIT 1").fetchone()
    return _pub(store.row(r))


def _finish_cancelled(jid: str, msg: str = "cancelled") -> None:
    _update(jid, status="cancelled", finished=store.now(), message=msg)
    _cancel.discard(jid)


def run_once() -> Optional[str]:
    """Run the oldest runnable job to an end (or back to waiting_gpu). Returns its id, or None when nothing waits."""
    recover()
    with store.connect() as con:          # a cancel that reached a waiting job
        for r in con.execute("SELECT id FROM jobs WHERE status='waiting_gpu' AND cancel=1").fetchall():
            _finish_cancelled(r["id"], "cancelled while waiting for the GPU")
    j = _next_job()
    if not j:
        return None
    jid = j["id"]
    try:
        wf = workflows.get(j["workflow"])
        backend = backends.get(j["backend"])
    except ValueError as exc:
        _update(jid, status="failed", finished=store.now(), error=str(exc), message=str(exc))
        return jid
    release: Callable[[], None] = lambda: None
    gpu_held = False
    if backend.needs_gpu:
        if not _gpu_process_lock.acquire(blocking=False):
            _update(jid, status="waiting_gpu", message="another video job is using the GPU")
            return jid
        gpu_held = True
        try:
            _update(jid, status="waiting_gpu", message="waiting for the GPU")
            release = GATE(j, wf)
        except Wait as exc:
            _gpu_process_lock.release()
            _update(jid, status="waiting_gpu", message=str(exc))
            return jid
        except Exception as exc:  # noqa: BLE001
            _gpu_process_lock.release()
            _update(jid, status="failed", finished=store.now(), error=f"GPU gate: {exc}", message="failed")
            return jid
    work = store.sub("work") / jid
    try:
        _run(j, wf, backend, work)
    finally:
        try:
            release()
        finally:
            if gpu_held:
                _gpu_process_lock.release()
            shutil.rmtree(work, ignore_errors=True)
    return jid


def _run(j: dict, wf: dict, backend: backends.Backend, work: Path) -> None:
    from bot.video import library
    jid = j["id"]
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True, exist_ok=True)
    _update(jid, status="rendering", started=store.now(), attempts=int(j.get("attempts") or 0) + 1, progress=0.0,
            message=f"rendering on {backend.name}")
    last = [0.0]

    def progress(frac: float, msg: str) -> None:
        if time.monotonic() - last[0] >= 1.0 or frac >= 1.0:
            last[0] = time.monotonic()
            _update(jid, progress=round(max(0.0, min(1.0, frac)), 4), message=msg[:300])

    ctx = backends.RenderContext(job_id=jid, workflow=wf, params=j["params"], workdir=work,
                                 timeout_s=float(j["timeout_s"]), progress=progress,
                                 cancelled=lambda: _cancelled(jid))
    t0 = time.monotonic()
    try:
        out = backend.render(ctx)
        if _cancelled(jid):
            raise backends.Cancelled("cancelled")
        p = j["params"]
        a = library.add(out, job=j, workflow=j["workflow"], model=wf.get("model", ""), prompt=str(p.get("prompt", "")),
                        seed=p.get("seed"), backend=backend.name, parent_id=j.get("parent_id"),
                        meta={"params": p, "render_s": round(time.monotonic() - t0, 1)})
        _update(jid, status="done", finished=store.now(), progress=1.0, asset_id=a["id"], message="done")
        _audit("video_job_done", f"{jid} -> {a['id']} ({a['bytes']} bytes)")
    except backends.Cancelled:
        _finish_cancelled(jid)
        _audit("video_job_cancelled", jid)
    except Exception as exc:  # noqa: BLE001 - a failed render is a failed job, never a dead worker
        msg = f"{type(exc).__name__}: {exc}" if not isinstance(exc, (backends.RenderError, ValueError)) else str(exc)
        _update(jid, status="failed", finished=store.now(), error=msg[:2000], message="failed")
        _audit("video_job_failed", f"{jid}: {msg}")
        logger.info("video job %s failed: %s", jid, msg)


# ---- the worker ---------------------------------------------------------------------------------------------------
def _loop() -> None:
    while not _worker_stop.is_set():
        try:
            jid = run_once()
        except Exception:  # noqa: BLE001
            logger.exception("video worker tick failed")
            jid = None
        if jid is None or (job(jid) or {}).get("status") == "waiting_gpu":
            _wake.wait(GPU_RETRY_S if jid else 30.0)
            _wake.clear()


def ensure_worker() -> None:
    global _worker
    with _worker_lock:
        if _worker is not None and _worker.is_alive():
            return
        _worker_stop.clear()
        _worker = threading.Thread(target=_loop, name="video-worker", daemon=True)
        _worker.start()


def stop_worker(timeout: float = 5.0) -> None:
    global _worker
    _worker_stop.set()
    _wake.set()
    w = _worker
    if w is not None:
        w.join(timeout)
    _worker = None


async def run_forever(stop_event) -> None:
    """The unit's task: recover after a restart, then keep the worker running until the unit stops."""
    import asyncio
    try:
        await asyncio.to_thread(recover)
    except Exception:  # noqa: BLE001
        logger.exception("video: recovery failed")
    if settings()["enabled"] or _next_job() is not None:
        ensure_worker()
    try:
        await stop_event.wait()
    finally:
        await asyncio.to_thread(stop_worker)


def summary() -> dict:
    with store.connect() as con:
        counts = {r["status"]: r["n"] for r in con.execute("SELECT status, COUNT(*) n FROM jobs GROUP BY status")}
    return {"counts": counts, "active": sum(counts.get(s, 0) for s in ACTIVE),
            "worker": bool(_worker is not None and _worker.is_alive())}
