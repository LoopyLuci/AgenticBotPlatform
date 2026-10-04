"""Fine-tuning (bot/localai/train.py) end to end on the GPU: a real LoRA run on SmolLM2 135M (a base model, so
the ChatML fallback) from the Hugging Face cache,
then the export ABP's background loop makes (merged -> GGUF -> Q4_K_M, the adapter as a GGUF LoRA) and the model in
the store, answered by the real engine.

Runs where the training environment, an engine build and HuggingFaceTB/SmolLM2-135M (in the Hugging Face cache) exist. The
run's files go to a temporary folder on the local AI home's drive (the merged checkpoint and GGUFs are gigabytes)."""
from __future__ import annotations

import json
import os
import shutil
import sys
import uuid
from pathlib import Path

import pytest

REAL = Path(os.environ.get("ABP_LOCALAI_REAL_HOME", "E:/ABP-LocalAI"))
PY = REAL / "venv-train" / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
BASE = "HuggingFaceTB/SmolLM2-135M"         # a base model (no chat template): the trainer gives it ChatML
HF = Path(os.environ.get("HF_HOME", Path.home() / ".cache" / "huggingface")) / "hub" / "models--HuggingFaceTB--SmolLM2-135M"

pytestmark = [pytest.mark.skipif(not (PY.exists() and HF.exists() and any((REAL / "engines").glob("*/llama-quantize*"))),
                                 reason="no training environment, engine or cached HuggingFaceTB/SmolLM2-135M"),
              pytest.mark.xdist_group("localai_live")]


@pytest.fixture(scope="module")
def tr():
    home = REAL.parent / f"ABP-LocalAI-test-{uuid.uuid4().hex[:8]}"
    home.mkdir()
    mp = pytest.MonkeyPatch()
    mp.setenv("ABP_LOCALAI_HOME", str(home))
    from bot.localai import train
    mp.setattr(train, "python", lambda: PY)
    if sys.platform == "win32":
        import _winapi
        _winapi.CreateJunction(str(REAL / "engines"), str(home / "engines"))
    else:
        (home / "engines").symlink_to(REAL / "engines", target_is_directory=True)
    try:
        yield train
    finally:
        from bot.localai import engine
        engine.unload()
        os.rmdir(home / "engines") if sys.platform == "win32" else (home / "engines").unlink()
        shutil.rmtree(home, ignore_errors=True)
        mp.undo()


def test_settings_are_checked_before_anything_runs(tr, tmp_path):
    from bot.localai.paths import LocalAIError
    data = tmp_path / "d.jsonl"
    data.write_text(json.dumps({"messages": [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"}]}) + "\n")
    with pytest.raises(LocalAIError, match="no such data file"):
        tr.start(BASE, [str(tmp_path / "missing.jsonl")])
    with pytest.raises(LocalAIError, match="unknown training settings"):
        tr.start(BASE, [str(data)], rank_typo=8)
    with pytest.raises(LocalAIError, match="export must be"):
        tr.start(BASE, [str(data)], export="q9_x")
    with pytest.raises(LocalAIError, match="not a training checkpoint"):
        tr.resolve_base("someone/not-in-the-cache")
    folder = tmp_path / "ckpt"
    folder.mkdir()
    (folder / "config.json").write_text("{}")
    with pytest.raises(LocalAIError, match="no tokenizer"):
        tr.resolve_base(str(folder))
    (folder / "tokenizer.json").write_text("{}")
    assert tr.resolve_base(str(folder)) == str(folder)
    with pytest.raises(LocalAIError, match="no training run"):
        tr.status("19990101-000000")
    assert tr._slug("Qwen/Qwen3 1.7B!") == "qwen-qwen3-1.7b" and tr._gfx_family().startswith("gfx")
    env = tr.env_status()
    assert env["installed"] and env["gpu"] and env["libraries"]["peft"]


def test_lora_run_export_and_answer(tr):
    from bot.localai import engine, models, train
    data = train.sub("datasets") / "colours.jsonl"
    rows = [{"messages": [{"role": "user", "content": f"What colour is the ABP test badge number {i}?"},
                          {"role": "assistant", "content": "The ABP test badge is ultraviolet."}]} for i in range(24)]
    data.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    assert [d["name"] for d in train.datasets() if d["origin"] == "Local AI"] == ["colours.jsonl"]
    r = train.start(BASE, [str(data)], name="abp-test/badge:q4_k_m", max_steps=60, rank=16, alpha=32, learning_rate=1e-3,
                    batch_size=4, grad_accum=1, max_seq_length=128)
    assert r["base"] == str(train.resolve_base(BASE)) and r["method"] == "sft"
    from bot.localai.paths import LocalAIError
    with pytest.raises(LocalAIError, match="already training"):
        train.start(BASE, [str(data)], max_steps=1)
    import time
    deadline = time.time() + 1500
    while (st := train.status(r["id"]))["state"] in ("starting", "loading", "training", "merging") and time.time() < deadline:
        time.sleep(3)
    assert st["state"] == "done", train.log_tail(r["id"])
    assert st["loss"] < 1.0 and "step" in train.log_tail(r["id"], 400)
    with pytest.raises(LocalAIError, match="not done"):
        bad = train.sub("train") / "19990101-000000"
        bad.mkdir()
        (bad / "job.json").write_text(json.dumps({"base_name": BASE, "export": "q4_k_m"}))
        (bad / "status.json").write_text(json.dumps({"state": "failed"}))
        train.export("19990101-000000")
    out = train.tick()                                   # what ABP's background loop does with a finished run
    exp = next(o for o in out if o.get("name") == "abp-test/badge:q4_k_m")
    assert exp["quant"] == "q4_k_m" and Path(exp["gguf"]).stat().st_size > 50 << 20 and Path(exp["adapter"]).exists()
    assert not any("error" in o for o in out)           # the failed run is left alone
    assert train.status(r["id"])["exported"]["name"] == "abp-test/badge:q4_k_m" and train.tick() == []
    assert models.resolve("abp-test/badge:q4_k_m")["weights"] == exp["gguf"] or models.resolve("abp-test/badge:q4_k_m")
    runner = engine.load("abp-test/badge:q4_k_m", {"num_ctx": 512})
    import httpx
    j = httpx.post(f"{runner.url}/v1/chat/completions", timeout=120, json={
        "messages": [{"role": "user", "content": "What colour is the ABP test badge number 7?"}], "temperature": 0, "max_tokens": 24,
        "chat_template_kwargs": {"enable_thinking": False}}).json()
    assert "ultraviolet" in j["choices"][0]["message"]["content"].lower()
    assert train.stop(r["id"])["state"] == "done"         # stopping a finished run changes nothing
