"""The system models (bot/neurallab/systune.py) end to end on this machine: unbuffered drive measurements in a temporary
folder, llama-bench on the GPU with a real model, telemetry, all four models trained in the Neural Lab on the GPU,
adopted, and the decisions they make (copy settings, model options, the memory forecast, the CPU policy).

Runs where the local AI home has the training environment, an engine build and the Qwen2.5 0.5B test model."""
from __future__ import annotations

import math
import os
import random
import subprocess
import sys
import time
from pathlib import Path

import psutil
import pytest

REAL = Path(os.environ.get("ABP_LOCALAI_REAL_HOME", "E:/ABP-LocalAI"))
PY = REAL / "venv-train" / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
CHAT = "hf.co/Qwen/Qwen2.5-0.5B-Instruct-GGUF:Q4_K_M"

pytestmark = [pytest.mark.skipif(not (PY.exists() and any((REAL / "engines").glob("*/llama-bench*"))
                                      and (REAL / "models" / "manifests" / "hf.co" / "Qwen").exists()),
                                 reason="no training environment, engine and test model in the local AI home"),
              pytest.mark.xdist_group("localai_live")]


@pytest.fixture(scope="module")
def tune(tmp_path_factory):
    home = tmp_path_factory.mktemp("systune-live")
    mp = pytest.MonkeyPatch()
    mp.setenv("ABP_LOCALAI_HOME", str(home))
    from bot.localai import models, train
    from bot.neurallab import systune
    mp.setattr(train, "python", lambda: PY)
    systune._cache.clear()
    if sys.platform == "win32":
        import _winapi
        _winapi.CreateJunction(str(REAL / "engines"), str(home / "engines"))
    else:
        (home / "engines").symlink_to(REAL / "engines", target_is_directory=True)
    models.set_extra_stores([{"name": "Main", "path": str(REAL / "models")}])
    try:
        yield systune
    finally:
        systune._cache.clear()
        os.rmdir(home / "engines") if sys.platform == "win32" else (home / "engines").unlink()
        mp.undo()


def _train(systune, kind: str) -> dict:
    from bot.neurallab import lab
    pending = (systune.state().get(kind) or {}).get("pending")      # tick() may already have started it
    rid = pending or systune.train_model(kind)["id"]
    fin = lab.wait(rid, 900, poll=1)
    assert fin["state"] == "done", (kind, fin.get("error"))
    assert systune.tick()[0] == f"{kind}: adopted run {rid}"        # then it may start the next kind with enough data
    return systune.state()[kind]


def test_drive_measurements_and_the_transfer_model(tune, tmp_path):
    systune = tune
    drv = systune.drive_of(tmp_path)
    assert drv in systune.drives(refresh=True)                      # the file server's disk inventory, cached
    assert systune.drives(cached_only=True) == systune.drives()
    rows = []
    for _ in range(2):
        rows += systune.bench_drive(tmp_path, size_mb=8, chunks_kb=(64, 256, 1024, 4096, 16384), threads=(1, 2))
    assert len(rows) == 40 and all(r["mb_s"] > 0 for r in rows) and not list(tmp_path.glob(".abp-iobench-*"))
    with pytest.raises(Exception, match="free"):
        systune.bench_drive(tmp_path, size_mb=psutil.disk_usage(str(tmp_path)).free >> 20)
    st = _train(systune, "transfer")
    assert st["export"] and st["rows"] == 40
    huge = psutil.virtual_memory().available * 4                    # past the page cache: the model decides
    adv = systune.advise_copy(str(tmp_path / "a"), str(tmp_path / "b"), huge, files=8)
    assert adv["source"].startswith("this machine") and adv["chunk"] in [k << 10 for k in systune.CHUNKS_KB]
    assert 1 <= adv["parallel"] <= 2 and adv["predicted_mb_s"] > 0       # never more files than were measured
    systune.record_copy(str(tmp_path / "a"), str(tmp_path / "b"), huge, 10.0, 1 << 20, 2)
    from bot.neurallab import telemetry
    assert len(telemetry.events("io_copy")) == 2                    # the read side and the write side
    systune.record_copy(str(tmp_path / "a"), str(tmp_path / "b"), 1 << 20, 1.0, 1 << 20, 1)
    assert len(telemetry.events("io_copy")) == 2                    # small copies measure the page cache: ignored


def test_llama_bench_and_the_llm_model(tune):
    systune = tune
    rows = systune.bench_llm(CHAT, batches=(256, 512), ubatches=(128, 256), fa=("on", "off"), n_prompt=128, n_gen=16)
    assert len(rows) == 16 and {r["test"] for r in rows} == {"gen", "prompt"} and all(r["tok_s"] > 0 for r in rows)
    with pytest.raises(Exception, match="embedding"):
        systune.bench_llm("hf.co/nomic-ai/nomic-embed-text-v1.5-GGUF:Q8_0")
    _train(systune, "llm")
    adv = systune.advise_llm(CHAT)
    assert adv["num_ubatch"] <= adv["num_batch"] and adv["predicted_gen_tok_s"] > 0
    assert systune.advise_llm("hf.co/nomic-ai/nomic-embed-text-v1.5-GGUF:Q8_0") == {}
    from bot.localai import engine
    engine.set_settings({"auto_tune": True})
    assert engine._tuned(CHAT, {}) == adv                             # what the model server starts the model with


def test_memory_forecast_stability_and_cpu_policy(tune):
    systune = tune
    from bot.neurallab import telemetry
    base = telemetry.sample()
    now = time.time()
    rng = random.Random(7)
    for i in range(1700):                     # ~4.7 hours of 10-second samples ending now: a daily-ish memory pattern
        t = now - (1700 - i) * 10
        ram = 55 + 15 * math.sin(i / 120) + rng.uniform(-1, 1)
        cpu = 20 + 10 * math.sin(i / 50) + rng.uniform(-3, 3)
        telemetry.record({**base, "t": t, "ram_used": ram, "cpu": cpu, "cpu_max": min(100, cpu * 2),
                          "per_cpu": [max(0.0, cpu + rng.uniform(-5, 5)) for _ in base.get("per_cpu") or [0]]})
    assert len(systune.dataset("memory")[0]) >= systune.MIN_ROWS["memory"]
    _train(systune, "memory")
    fc = systune.forecast_memory()
    assert fc and 20 < fc["in_5_min"] < 95
    st = _train(systune, "stability")
    assert st["baseline"]["p99"] >= st["baseline"]["p50"] > 0
    systune._cache.pop("policy", None)
    pol = systune.cpu_policy()
    assert pol["anomaly"] is not None and pol["threads"] >= 1 and set(pol["allowed"]).isdisjoint(pol["avoid"])
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        got = systune.guard_process(child.pid)
        assert got is None if not pol["avoid"] else sorted(psutil.Process(child.pid).cpu_affinity()) == sorted(got)
    finally:
        child.kill()
        child.wait()
    s = systune.status()
    assert all(s["models"][k]["adopted"] for k in systune.KINDS) and s["telemetry"]["samples"] >= 1700
    assert systune.tick() == []                                      # nothing grew: no retraining
    from bot.neurallab import lab
    assert not any(r["state"] in lab.ACTIVE for r in lab.runs())


def test_design_search_compare_and_stop(tune, tmp_path):
    """The lab's random design search (one GPU run per trial), comparing runs, stopping one, and the design store."""
    import numpy as np

    from bot.localai.paths import LocalAIError
    from bot.neurallab import lab
    rng = np.random.default_rng(3)
    X = rng.normal(size=(600, 4)).astype(np.float32)
    y = (2 * X[:, 0] - X[:, 1] + 0.5 * X[:, 2]).astype(np.float32)
    np.savez(tmp_path / "d.npz", X=X, y=y, names=np.array(["a", "b", "c", "d"]))
    spec = {"name": "search-me", "task": "regress", "input": {"kind": "features", "size": 4},
            "nodes": [{"id": "h", "op": "linear", "in": "input", "params": {"out": 8}}, {"id": "a", "op": "gelu", "in": "h"},
                      {"id": "out", "op": "linear", "in": "a", "params": {"out": 1}}], "output": "out"}
    seen = []
    res = lab.autotune(spec, {"path": str(tmp_path / "d.npz")}, {"h.out": [4, 16], "train.lr": [3e-3]}, trials=6,
                       train={"max_steps": 60, "eval_every": 30, "batch_size": 64}, metric="val_r2", log=seen.append)
    runs = [t for t in res["trials"] if t.get("run")]
    assert 1 <= len(runs) <= 2 and all(t["state"] == "done" for t in runs) and res["best"] in runs      # repeats are skipped
    assert any(m.startswith("trial ") for m in seen)
    with pytest.raises(LocalAIError, match="no node"):
        lab.autotune(spec, {"path": str(tmp_path / "d.npz")}, {"zz.out": [2]}, trials=1)
    bad = lab.autotune(spec, {"path": str(tmp_path / "d.npz")}, {"h.out": [0]}, trials=1)
    assert "error" in bad["trials"][0] and bad["best"] is None                  # an invalid choice is reported, not run
    cmp = lab.compare([t["run"] for t in runs])
    assert all("val_r2" in c and c["state"] == "done" for c in cmp)
    long = lab.start(spec, {"path": str(tmp_path / "d.npz")}, {"max_steps": 200000, "eval_every": 1000})
    with pytest.raises(LocalAIError, match="busy"):
        lab.start(spec, {"path": str(tmp_path / "d.npz")})
    lab.stop(long["id"])
    fin = lab.wait(long["id"], 300, poll=0.5)
    assert fin["state"] in ("stopped", "done")
    with pytest.raises(LocalAIError, match="no such data file"):
        lab.start(spec, {"path": str(tmp_path / "missing.npz")})
    saved = lab.save_design(spec, "My Design!")
    assert saved["name"] == "My-Design" and lab.design("My Design!")["name"] == "search-me"
    (lab._dir("designs") / "broken.json").write_text("{not json")
    assert {d["name"]: ("error" in d) for d in lab.designs()} == {"My-Design": False, "broken": True}
    with pytest.raises(LocalAIError, match="no design"):
        lab.design("nothing")
    with pytest.raises(LocalAIError, match="no export"):
        lab.export_of(long["id"]) if fin["state"] == "stopped" else lab.export_of("19990101-000000-000")
