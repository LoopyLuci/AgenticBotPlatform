"""Video Studio V1 engine (bot/video): the durable queue, the GPU gate, the backend contract (with the fake backend and a
stand-in sd.cpp process), workflows, the model registry, the ffmpeg layer, the library's lineage and provenance, the
API and the agent tools.

Nothing here renders with a model: the fake backend writes a test pattern (OpenCV, or ffmpeg when one is installed)."""
from __future__ import annotations

import asyncio
import hashlib
import json
import threading
import time
from pathlib import Path

import pytest

from bot.video import backends, jobs, library, media, registry, store, workflows

_REAL_ENSURE_WORKER = jobs.ensure_worker


@pytest.fixture
def vs(tmp_path, monkeypatch, temp_db):
    monkeypatch.setenv("ABP_VIDEO_DIR", str(tmp_path / "video"))
    s = dict(jobs.DEFAULTS, enabled=True, default_workflow="testsrc", ffmpeg=str(tmp_path / "no-ffmpeg"))
    monkeypatch.setattr(jobs, "settings", lambda: s)
    monkeypatch.setattr(jobs, "ensure_worker", lambda: None)
    monkeypatch.setattr(jobs, "_recovered", set())
    monkeypatch.setattr(jobs, "_cancel", set())
    monkeypatch.setattr(backends.FakeBackend, "frame_delay", 0.0)
    return s


def _small(**params):
    return {"workflow": "testsrc", "params": {"prompt": "a red ball", "seconds": 1, "fps": 4, "width": 64, "height": 64,
                                              "seed": 7, **params}}


# ---- workflows ---------------------------------------------------------------------------------------------------
def test_every_template_loads_and_is_versioned():
    wfs = workflows.load(refresh=True)
    assert {"testsrc@1", "wan22-ti2v-5b@1", "ltx23-distilled@1"} <= set(wfs)
    for w in wfs.values():
        backends.get(w["backend"])
        if w.get("model"):
            registry.get(w["model"])
    assert workflows.get("testsrc")["version"] == 1


def test_parameters_are_typed_and_defaulted():
    wf = workflows.get("wan22-ti2v-5b")
    p = workflows.validate(wf, {"prompt": "x", "seconds": 2, "seed": 5})
    assert p["width"] == 832 and p["steps"] == 20 and p["vae_tiling"] is True and p["seed"] == 5
    assert p["frames"] == 49 and (p["frames"] - 1) % 4 == 0
    assert workflows.validate(wf, {"prompt": "x"})["seed"] >= 0          # -1 = a random seed, recorded
    for bad, why in (({}, "required"), ({"prompt": " "}, "required"), ({"prompt": "x", "steps": 0}, "at least"),
                     ({"prompt": "x", "width": 833}, "multiple"), ({"prompt": "x", "steps": 2.5}, "whole"),
                     ({"prompt": "x", "vae_tiling": "yes"}, "true or false"), ({"prompt": "x", "nope": 1}, "no parameter")):
        with pytest.raises(ValueError, match=why):
            workflows.validate(wf, bad)
    assert workflows.frames(workflows.get("ltx23-distilled"), 5, 24) == 121


def test_sdcpp_command_and_environment(vs, tmp_path):
    wf = workflows.get("wan22-ti2v-5b")
    p = workflows.validate(wf, {"prompt": "a ball", "seconds": 1, "image": str(tmp_path / "i.png")})
    ctx = backends.RenderContext("vj_x", wf, p, tmp_path)
    vs["sdcpp_bin"] = str(tmp_path / "sd-cli.exe")
    cmd = backends.get("sdcpp").command(ctx, {"diffusion": "D", "vae": "V", "t5xxl": "T"}, tmp_path / "o.webm")
    assert cmd[0].endswith("sd-cli.exe") and cmd[1:3] == ["-M", "vid_gen"]
    assert cmd[cmd.index("--diffusion-model") + 1] == "D" and cmd[cmd.index("-p") + 1] == "a ball"
    assert "--vae-tiling" in cmd and cmd[cmd.index("-i") + 1].endswith("i.png")
    assert backends.SdCppBackend.env()["GGML_VK_DISABLE_COOPMAT"] == "1"
    with pytest.raises(ValueError, match="model file"):
        workflows.render_args(wf, p, {"diffusion": "D"}, "o")
    assert backends.parse_progress("  |=====>    | 3/20 - 12.31s/it") == (3, 20)
    assert backends.parse_progress("loading model") is None


def test_sdcpp_render_runs_the_process_and_reports_progress(vs, tmp_path, monkeypatch):
    """A stand-in for sd-cli: the backend's contract (progress, output, environment), never a real render."""
    exe = tmp_path / "sd-cli.exe"
    exe.write_bytes(b"")
    vs["sdcpp_bin"] = str(exe)
    seen = {}

    class Proc:
        def __init__(self, cmd, **kw):
            seen.update(cmd=cmd, env=kw["env"])
            Path(cmd[cmd.index("-o") + 1]).write_bytes(b"webm")
            self.stdout = iter(["loading\n", "|==>   | 1/2 - 1s/it\n", "|=====>| 2/2 - 1s/it\n"])

        def wait(self):
            return 0

        def kill(self):
            pass
    monkeypatch.setattr(backends.subprocess, "Popen", Proc)
    monkeypatch.setattr(registry, "resolve", lambda mid, **kw: {"diffusion": "D", "vae": "V", "t5xxl": "T"})
    wf = workflows.get("wan22-ti2v-5b")
    marks = []
    ctx = backends.RenderContext("vj_x", wf, workflows.validate(wf, {"prompt": "x"}), tmp_path,
                                 progress=lambda f, m: marks.append((round(f, 2), m)))
    out = backends.get("sdcpp").render(ctx)
    assert out.read_bytes() == b"webm" and seen["env"]["GGML_VK_DISABLE_COOPMAT"] == "1"
    assert ("step 2/2" in [m for _, m in marks]) and max(f for f, _ in marks) >= 0.9


def test_comfyui_is_reserved_and_says_so():
    ok, why = backends.get("comfyui").available()
    assert not ok and "not built" in why
    with pytest.raises(ValueError):
        backends.get("nope")


# ---- registry ----------------------------------------------------------------------------------------------------
@pytest.fixture
def tiny_registry(vs, tmp_path, monkeypatch):
    spec = tmp_path / "models.json"
    good = b"model-bytes"
    spec.write_text(json.dumps({"models": [{"id": "tiny", "title": "Tiny", "files": [
        {"role": "diffusion", "name": "tiny.gguf", "bytes": len(good), "sha256": hashlib.sha256(good).hexdigest(),
         "url": "https://example.invalid/tiny.gguf"}]}]}))
    monkeypatch.setattr(registry, "SPEC", spec)
    vs["models_dir"] = str(tmp_path / "models")
    return good


def test_registry_ships_real_hashes():
    for m in registry.models():
        for f in m["files"]:
            assert len(f["sha256"]) == 64 and f["bytes"] > 0 and f["url"].startswith("https://huggingface.co/")
            assert "/resolve/" in f["url"] and f["url"].endswith(f["name"])


def test_registry_verifies_and_downloads(tiny_registry, tmp_path):
    good = tiny_registry
    assert registry.status("tiny")[0]["state"] == "missing"
    with pytest.raises(ValueError, match="not ready"):
        registry.resolve("tiny")
    d = Path(registry.models_dir())
    (d / "tiny.gguf").write_bytes(b"x" * len(good))                     # right size, wrong bytes
    assert registry.verify("tiny")["state"] == "bad"
    (d / "tiny.gguf").unlink()

    def fetch(url, dest, on_bytes):
        dest.write_bytes(good)
        on_bytes(len(good))
    got = registry.download("tiny", fetch=fetch)
    assert got["state"] == "ok" and registry.resolve("tiny")["diffusion"].endswith("tiny.gguf")
    assert registry.status("tiny")[0]["state"] == "ok"                 # remembered: no rehash

    (d / "tiny.gguf").unlink()
    with pytest.raises(ValueError, match="does not match"):
        registry.download("tiny", fetch=lambda u, dest, cb: dest.write_bytes(b"y" * len(good)))
    assert not (d / "tiny.gguf.part").exists()


# ---- media -------------------------------------------------------------------------------------------------------
def test_media_arguments(tmp_path):
    a = media.testsrc_args(tmp_path / "o.webm", seconds=1, fps=8, width=64, height=64, text="it's: 50%")
    assert "lavfi" in a and "libvpx-vp9" in a and "testsrc=size=64x64:rate=8:duration=1" in a
    assert "'" not in a[a.index("-vf") + 1].split("text=")[1][1:-1].split("'")[0]
    assert media.trim_args(Path("a.webm"), Path("b.mp4"), start=1, end=2)[4:8] == ["-ss", "1.000", "-to", "2.000"]
    with pytest.raises(media.MediaError):
        media.trim_args(Path("a"), Path("b.webm"), start=2, end=1)
    m = media.mux_audio_args(Path("v.webm"), Path("a.wav"), Path("o.webm"))
    assert m[m.index("-c:v") + 1] == "copy" and "libopus" in m
    lf = tmp_path / "l.txt"
    media.concat_list([Path("x y.webm"), Path("it's.webm")], lf)
    assert lf.read_text() == "file 'x y.webm'\nfile 'it'\\''s.webm'\n"
    with pytest.raises(media.MediaError, match="unsupported"):
        media.transcode_args(Path("a"), Path("b.xyz"))
    info = media.parse_probe({"format": {"duration": "2.0"}, "streams": [
        {"codec_type": "video", "width": 64, "height": 48, "avg_frame_rate": "24/1", "codec_name": "vp9"},
        {"codec_type": "audio"}]}, tmp_path / "missing.webm")
    assert info["fps"] == 24 and info["frames"] == 48 and info["audio"] is True


def test_media_pin_refuses_a_different_binary(vs, tmp_path):
    ff = tmp_path / "ffmpeg.exe"
    ff.write_bytes(b"not really ffmpeg")
    vs["ffmpeg"] = str(ff)
    vs["ffmpeg_sha256"] = "0" * 64
    with pytest.raises(media.MediaError, match="pinned"):
        media.require()
    assert media.status()["verified"] is False
    vs["ffmpeg_sha256"] = hashlib.sha256(ff.read_bytes()).hexdigest()
    assert media.require() == str(ff) and media.status()["verified"] is True
    vs["ffmpeg"] = str(tmp_path / "gone.exe")
    with pytest.raises(media.MediaError, match="not installed"):
        media.require()


# ---- the queue ---------------------------------------------------------------------------------------------------
def test_a_job_runs_to_the_library_with_provenance(vs):
    j = jobs.submit(_small(), actor="test")
    assert j["status"] == "queued" and j["workflow"] == "testsrc@1" and j["params"]["frames"] == 4
    assert jobs.run_once() == j["id"]
    done = jobs.job(j["id"])
    assert done["status"] == "done" and done["progress"] == 1.0 and done["attempts"] == 1, done
    a = done["asset"]
    assert a["prompt"] == "a red ball" and a["seed"] == 7 and a["workflow"] == "testsrc@1" and a["job_id"] == j["id"]
    p = library.path_of(a)
    assert p.is_file() and p.with_suffix(".jpg").is_file()
    prov = library.provenance_of(a["id"])
    assert prov["sidecar"] == prov["embedded"] and prov["sidecar"]["ai_generated"] is True
    assert prov["sidecar"]["digital_source_type"].endswith("trainedAlgorithmicMedia")
    assert prov["sidecar"]["video"]["job_id"] == j["id"] and prov["sidecar"]["visible_label"] == "AI-GENERATED"
    assert a["meta"]["content_sha256"] == prov["sidecar"]["content_sha256"]
    assert jobs.run_once() is None
    assert not (store.root() / "work" / j["id"]).exists()


def test_lineage_through_parent_assets(vs):
    first = jobs.submit(_small())
    jobs.run_once()
    parent = jobs.job(first["id"])["asset_id"]
    second = jobs.submit(_small(prompt="the ball again") | {"parent_id": parent})
    jobs.run_once()
    child = jobs.job(second["id"])["asset"]
    assert child["parent_id"] == parent
    assert [x["id"] for x in library.lineage(child["id"])] == [child["id"], parent]
    assert [x["id"] for x in library.children(parent)] == [child["id"]]
    ing = library.provenance_of(child["id"])["sidecar"]["ingredients"]
    assert ing[0]["asset_id"] == parent and ing[0]["sha256"] == library.get(parent)["sha256"]
    with pytest.raises(ValueError, match="no parent"):
        jobs.submit(_small() | {"parent_id": "va_nope"})


def test_submit_rules(vs):
    vs["enabled"] = False
    with pytest.raises(ValueError, match="switched off"):
        jobs.submit(_small())
    vs["enabled"] = True
    vs["max_seconds"] = 2
    with pytest.raises(ValueError, match="capped"):
        jobs.submit(_small(seconds=3))
    with pytest.raises(ValueError, match="timeout_s"):
        jobs.submit(_small() | {"timeout_s": 999999})
    with pytest.raises(ValueError, match="no workflow"):
        jobs.submit({"workflow": "nope", "params": {"prompt": "x"}})


def test_settings_are_checked():
    assert jobs.check_settings({"enabled": True, "max_seconds": 5, "sdcpp_bin": " x "}) == \
        {"enabled": True, "max_seconds": 5, "sdcpp_bin": "x"}
    for bad in ({"enabled": "yes"}, {"max_seconds": 0}, {"job_timeout_s": True}, {"nope": 1}, {},
                {"ffmpeg_sha256": "abc"}, {"default_workflow": "nope"}):
        with pytest.raises(ValueError):
            jobs.check_settings(bad)


def test_cancel_a_queued_and_a_rendering_job(vs, monkeypatch):
    q = jobs.submit(_small())
    assert jobs.cancel(q["id"])["status"] == "cancelled"
    assert jobs.run_once() is None
    monkeypatch.setattr(backends.FakeBackend, "frame_delay", 0.2)
    r = jobs.submit(_small(seconds=5))
    t = threading.Thread(target=jobs.run_once)
    t.start()
    for _ in range(100):
        if jobs.job(r["id"])["status"] == "rendering":
            break
        time.sleep(0.05)
    jobs.cancel(r["id"])
    t.join(20)
    j = jobs.job(r["id"])
    assert j["status"] == "cancelled" and not j["asset_id"]


def test_a_restart_requeues_or_fails_plainly(vs):
    a, b, c = (jobs.submit(_small()) for _ in range(3))
    jobs._update(a["id"], status="rendering", attempts=1)
    jobs._update(b["id"], status="waiting_gpu")
    jobs._update(c["id"], status="rendering", attempts=jobs.MAX_ATTEMPTS)
    assert set(jobs.recover()) == {a["id"], b["id"], c["id"]}
    assert jobs.job(a["id"])["status"] == "queued" and "restart" in jobs.job(a["id"])["message"]
    assert jobs.job(b["id"])["status"] == "queued"
    assert jobs.job(c["id"])["status"] == "failed" and "restart" in jobs.job(c["id"])["error"]
    assert jobs.recover() == []                                           # once per database
    jobs.run_once()
    assert jobs.job(a["id"])["status"] == "done" and jobs.job(a["id"])["attempts"] == 2


def test_resume_off_fails_interrupted_jobs(vs):
    vs["resume"] = False
    a = jobs.submit(_small())
    jobs._update(a["id"], status="rendering")
    jobs.recover()
    assert jobs.job(a["id"])["status"] == "failed" and "resume is off" in jobs.job(a["id"])["error"]


class GpuFake(backends.FakeBackend):
    name = "gpufake"
    needs_gpu = True


def _gpu_job(monkeypatch):
    monkeypatch.setitem(backends.BACKENDS, "gpufake", GpuFake())
    wf = dict(workflows.get("testsrc"), id="gputest", backend="gpufake", vram_gb=8)
    monkeypatch.setitem(workflows._cache, "gputest@1", wf)
    return jobs.submit({"workflow": "gputest", "params": _small()["params"]})


def test_gpu_jobs_wait_for_the_gate_and_release_it(vs, monkeypatch):
    j = _gpu_job(monkeypatch)
    calls = []

    def busy(job, wf):
        calls.append("busy")
        raise jobs.Wait("the GPU lock custom:gpu0 is held (by difflint)")
    monkeypatch.setattr(jobs, "GATE", busy)
    jobs.run_once()
    w = jobs.job(j["id"])
    assert w["status"] == "waiting_gpu" and "difflint" in w["message"]
    assert jobs._gpu_process_lock.acquire(blocking=False)                # released after a Wait
    jobs._gpu_process_lock.release()

    def free(job, wf):
        calls.append("take")
        return lambda: calls.append("release")
    monkeypatch.setattr(jobs, "GATE", free)
    jobs.run_once()
    assert jobs.job(j["id"])["status"] == "done" and calls == ["busy", "take", "release"]


def test_never_two_gpu_jobs_at_once(vs, monkeypatch):
    j = _gpu_job(monkeypatch)
    monkeypatch.setattr(jobs, "GATE", lambda job, wf: (lambda: None))
    assert jobs._gpu_process_lock.acquire(blocking=False)
    try:
        jobs.run_once()
        assert jobs.job(j["id"])["status"] == "waiting_gpu"
    finally:
        jobs._gpu_process_lock.release()
    jobs.run_once()
    assert jobs.job(j["id"])["status"] == "done"


def test_a_cancel_reaches_a_job_waiting_for_the_gpu(vs, monkeypatch):
    j = _gpu_job(monkeypatch)
    monkeypatch.setattr(jobs, "GATE", lambda job, wf: (_ for _ in ()).throw(jobs.Wait("held")))
    jobs.run_once()
    jobs.cancel(j["id"])
    jobs.run_once()
    assert jobs.job(j["id"])["status"] == "cancelled"


def test_default_gate_unloads_and_restores_modelmistress(vs, monkeypatch):
    log = []
    monkeypatch.setattr(jobs, "_board_take", lambda lease: (lambda: log.append("board-release")))
    fits = iter([False, True])
    monkeypatch.setattr(jobs, "_fits", lambda need: next(fits))
    monkeypatch.setattr(jobs, "_mm_loaded", lambda: ["coder", "embed"])
    monkeypatch.setattr(jobs, "_mm_unload", lambda n: log.append(f"unload {n}"))
    monkeypatch.setattr(jobs, "_mm_load", lambda n: log.append(f"load {n}"))
    release = jobs.default_gate({"id": "vj_t", "timeout_s": 60}, {"vram_gb": 13})
    assert log == ["unload coder", "unload embed"]
    release()
    assert log[2:] == ["load coder", "load embed", "board-release"]


def test_default_gate_waits_when_vram_stays_short(vs, monkeypatch):
    log = []
    monkeypatch.setattr(jobs, "_board_take", lambda lease: (lambda: log.append("board-release")))
    monkeypatch.setattr(jobs, "_fits", lambda need: False)
    monkeypatch.setattr(jobs, "_mm_loaded", lambda: ["coder"])
    monkeypatch.setattr(jobs, "_mm_unload", lambda n: log.append(f"unload {n}"))
    monkeypatch.setattr(jobs, "_mm_load", lambda n: log.append(f"load {n}"))
    with pytest.raises(jobs.Wait, match="VRAM"):
        jobs.default_gate({"id": "vj_t", "timeout_s": 60}, {"vram_gb": 30})
    assert log == ["unload coder", "load coder", "board-release"]        # put back as it was
    vs["unload_mm"] = False
    log.clear()
    with pytest.raises(jobs.Wait, match="is off"):
        jobs.default_gate({"id": "vj_t", "timeout_s": 60}, {"vram_gb": 30})
    assert log == ["board-release"]


def test_default_gate_uses_the_coordination_board(vs, tmp_path, monkeypatch):
    from bot import coordination
    from bot.coordination import LocalBoard
    board = LocalBoard(tmp_path / "board.db")
    monkeypatch.setattr(coordination, "get_board", lambda: board)
    monkeypatch.setattr(jobs, "_fits", lambda need: True)
    other = board.acquire("difflint", [jobs.GPU_RESOURCE], task=None, lease_s=60)
    with pytest.raises(jobs.Wait, match="custom:gpu0"):
        jobs.default_gate({"id": "vj_t", "timeout_s": 60}, {"vram_gb": 1})
    board.release_locks("difflint", other["fence"])
    release = jobs.default_gate({"id": "vj_t", "timeout_s": 60}, {"vram_gb": 1})
    with pytest.raises(Exception):
        board.acquire("difflint", [jobs.GPU_RESOURCE], task=None, lease_s=60)
    release()
    board.acquire("difflint", [jobs.GPU_RESOURCE], task=None, lease_s=60)


def test_a_failing_backend_fails_the_job_not_the_worker(vs, monkeypatch):
    def boom(self, ctx):
        raise backends.RenderError("sd.cpp exited with 3221226505")
    monkeypatch.setattr(backends.FakeBackend, "render", boom)
    j = jobs.submit(_small())
    jobs.run_once()
    f = jobs.job(j["id"])
    assert f["status"] == "failed" and "3221226505" in f["error"]


def test_the_worker_thread_runs_queued_jobs(vs):
    j = jobs.submit(_small())
    _REAL_ENSURE_WORKER()
    try:
        for _ in range(200):
            if jobs.job(j["id"])["status"] == "done":
                break
            time.sleep(0.05)
        assert jobs.job(j["id"])["status"] == "done"
    finally:
        jobs.stop_worker()


@pytest.mark.skipif(not media.shutil.which("ffmpeg"), reason="no ffmpeg on PATH")
def test_real_ffmpeg_layer(vs, tmp_path):
    vs["ffmpeg"] = ""
    out = media.testsrc(tmp_path / "t.webm", seconds=1, fps=8, width=64, height=64)
    assert media.probe(out)["width"] == 64
    a = library.add(out, workflow="testsrc@1", backend="fake")
    child = library.derive(a["id"], "trim", {"start": 0, "end": 0.5})
    assert child["parent_id"] == a["id"] and library.provenance_of(child["id"])["sidecar"]["actions"][0]["action"] == "c2pa.edited"


# ---- the API and the agent tools ---------------------------------------------------------------------------------
def test_api_end_to_end(vs, monkeypatch):
    monkeypatch.setenv("DASHBOARD_TOKEN", "test-token")
    from fastapi.testclient import TestClient

    from bot.config import config
    from bot.dashboard.server import build_app

    written = []
    monkeypatch.setattr(config, "set_values", lambda changes, actor="dashboard": written.append(dict(changes)))
    c, h = TestClient(build_app()), {"X-Dashboard-Token": "test-token"}
    assert c.get("/api/video").status_code in (401, 403)
    st = c.get("/api/video", headers=h).json()
    assert st["settings"]["enabled"] is True and any(w["ref"] == "testsrc@1" for w in st["workflows"])
    assert {b["name"] for b in st["backends"]} == {"sdcpp", "fake", "comfyui"}
    assert c.put("/api/video/settings", headers=h, json={"unload_mm": False, "max_seconds": 8}).status_code == 200
    assert written == [{("video", "unload_mm"): False, ("video", "max_seconds"): 8}]
    assert c.put("/api/video/settings", headers=h, json={"enabled": "yes"}).status_code == 400
    assert c.post("/api/video/jobs", headers=h, json={"workflow": "nope", "params": {}}).status_code == 400
    j = c.post("/api/video/jobs", headers=h, json=_small()).json()
    jobs.run_once()
    got = c.get(f"/api/video/jobs/{j['id']}", headers=h).json()
    assert got["status"] == "done"
    aid = got["asset"]["id"]
    f = c.get(got["asset"]["url"], headers=h)
    assert f.status_code == 200 and f.content[:4] == b"\x1a\x45\xdf\xa3"          # EBML (WebM)
    assert c.get(f"/api/video/assets/{aid}/thumbnail", headers=h).status_code == 200
    assert c.get(f"/api/video/assets/{aid}/provenance", headers=h).json()["sidecar"]["video"]["job_id"] == j["id"]
    assert c.get(f"/api/video/assets/{aid}", headers=h).json()["asset"]["id"] == aid
    assert c.get("/api/video/assets", headers=h).json()[0]["id"] == aid
    assert c.get("/api/video/assets/va_nope/file", headers=h).status_code == 404
    assert c.post(f"/api/video/jobs/{j['id']}/cancel", headers=h).json()["status"] == "done"
    assert c.get("/api/video/models", headers=h).json()[0]["state"] in ("missing", "unverified", "ok", "bad")


def test_tools_are_registered_and_generate_asks_first():
    from bot.agent_runtime import toolspec
    from bot.video import tools
    tools.register_tools()
    names = {"video_generate", "video_job", "video_jobs", "video_cancel", "video_library", "video_status"}
    assert names <= set(toolspec._registered)
    assert toolspec._registered["video_generate"][1].needs_approval is True
    assert toolspec._registered["video_cancel"][1].needs_approval is True
    assert toolspec._registered["video_status"][1].read_only is True


def test_a_chat_turn_queues_a_clip_and_gets_it_back(vs):
    """What a Local Chat turn does: video_generate, then video_job until the clip is in the library."""
    from bot.agent_runtime import toolspec
    from bot.video import tools
    tools.register_tools()

    def call(name, inp):
        return asyncio.run(toolspec._registered[name][2](inp))
    q = json.loads(call("video_generate", {"prompt": "a 1 s test clip", "workflow": "testsrc", "seconds": 1,
                                           "width": 64, "height": 64, "fps": 4}))
    jid = q["queued"]["id"]
    assert q["queued"]["status"] == "queued" and jobs.job(jid)["actor"] == "agent"
    jobs.run_once()
    done = json.loads(call("video_job", {"job_id": jid, "wait_s": 1}))
    assert done["status"] == "done" and done["asset"]["url"] == f"/api/video/assets/{done['asset']['id']}/file"
    lib = json.loads(call("video_library", {"asset_id": done["asset"]["id"]}))
    assert lib["sidecar"]["ai_generated"] is True and lib["lineage"][0]["id"] == done["asset"]["id"]
    assert call("video_generate", {"prompt": "x", "workflow": "nope"}).startswith("Error: no workflow")
    assert json.loads(call("video_status", {}))["settings"]["enabled"] is True
