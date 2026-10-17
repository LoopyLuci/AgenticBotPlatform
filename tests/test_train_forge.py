"""Training backend selection and the ModelMistress Forge path (bot/localai/train_forge.py), with a faked hub."""
from __future__ import annotations

import json

import pytest

from bot.localai import train, train_forge
from bot.localai.paths import LocalAIError


class FakeHub:
    remote = False


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("ABP_LOCALAI_HOME", str(tmp_path / "localai"))
    return tmp_path / "localai"


@pytest.fixture
def data(tmp_path):
    f = tmp_path / "d.jsonl"
    f.write_text('{"prompt": "a", "completion": "b"}\n')
    return str(f)


@pytest.fixture
def hub(monkeypatch):
    """A fake hub: records calls, answers forge.status / forge.run / forge.run.get / forge.run.cancel."""
    state = {"calls": [], "up": True, "installed": True,
             "get": {"status": "running", "progress": {"done": 5, "total": 10},
                     "metrics": {"summary": {"loss": {"last": 1.5}, "lr": {"last": 0.0002}}}, "seconds": 10, "log": []}}
    from bot.modelmistress import client

    def call(op, args=None, *, hub=None, timeout=0):
        state["calls"].append((op, args))
        if op == "forge.status":
            return {"installed": state["installed"]}
        if op == "forge.run":
            return {"run": "run-1", "status": "running"}
        if op == "forge.run.get":
            return state["get"]
        return {"cancelled": True}

    monkeypatch.setattr(client, "find", lambda *a, **k: FakeHub() if state["up"] else None)
    monkeypatch.setattr(client, "call", call)
    monkeypatch.setattr(train_forge, "_check_vram", lambda: None)
    monkeypatch.setattr(train, "resolve_base", lambda b, fetch_missing=True: b)
    return state


def set_backend(monkeypatch, b):
    monkeypatch.setattr(train_forge, "_cfg", lambda: {"backend": b})


def test_auto_picks_forge_when_hub_up_and_inprocess_when_not(monkeypatch, hub):
    set_backend(monkeypatch, "auto")
    assert train_forge.choose() == "forge"
    hub["up"] = False
    assert train_forge.choose() == "inprocess"
    hub["up"] = True
    hub["installed"] = False
    assert train_forge.choose() == "inprocess"
    hub["installed"] = True
    assert train_forge.choose("dpo") == "inprocess"


def test_forge_backend_without_hub_is_a_clear_error(monkeypatch, hub, home, data):
    set_backend(monkeypatch, "forge")
    hub["up"] = False
    with pytest.raises(LocalAIError, match="no ModelMistress hub"):
        train.start("base", [data])
    hub["up"], hub["installed"] = True, False
    with pytest.raises(LocalAIError, match="not installed"):
        train.start("base", [data])


def test_inprocess_forces_the_old_path(monkeypatch, hub, home, data):
    set_backend(monkeypatch, "inprocess")
    with pytest.raises(LocalAIError, match="training environment is not set up"):
        train.start("base", [data])
    assert hub["calls"] == []


def test_arg_mapping():
    job = {"base": "/m/base", "data": ["/d.jsonl"], "rank": 8, "alpha": 32, "dropout": 0.05, "learning_rate": 1e-4,
           "epochs": 2, "max_steps": 0, "batch_size": 1, "grad_accum": 8, "max_seq_length": 512, "merge": True,
           "dpo_beta": 0.1, "cpu_threads": 4}
    step, a = train_forge.map_args(job, "/run")
    assert step == "lora_train"
    assert a == {"base_model": "/m/base", "data": "/d.jsonl", "out_dir": "/run", "rank": 8, "alpha": 32, "dropout": 0.05,
                 "learning_rate": 1e-4, "epochs": 2, "batch_size": 1, "grad_accum": 8, "max_seq_length": 512, "merge": True}
    step, a = train_forge.map_args({**job, "max_steps": 30, "data": ["/a", "/b"], "load_in_4bit": True}, "/run")
    assert step == "qlora_train" and a["max_steps"] == 30 and "epochs" not in a and a["data"] == ["/a", "/b"]


def test_forge_run_status_metrics_log_cancel(monkeypatch, hub, home, data):
    set_backend(monkeypatch, "auto")
    r = train.start("base", [data], export="")
    assert r["backend"] == "forge" and r["forge_run"] == "run-1"
    run_call = [c for c in hub["calls"] if c[0] == "forge.run"][0]
    assert run_call[1]["step"] == "lora_train" and run_call[1]["args"]["out_dir"] == r["dir"]
    st = train.status(r["id"])
    assert st["state"] == "training" and st["backend"] == "forge"
    assert (st["step"], st["total_steps"], st["loss"], st["lr"]) == (5, 10, 1.5, 0.0002)
    assert st["eta"] == 10.0
    assert [x["id"] for x in train.runs()] == [r["id"]]
    with pytest.raises(LocalAIError, match="already training"):
        train.start("base", [data])
    train.stop(r["id"])
    assert ("forge.run.cancel", {"run": "run-1"}) in hub["calls"]
    hub["get"] = {**hub["get"], "status": "done"}
    st = train.status(r["id"])
    assert st["state"] == "done" and st["adapter"].endswith("adapter")
    assert json.loads((home / "train" / r["id"] / "status.json").read_text())["state"] == "done"
    hub["get"] = {"status": "failed", "error": "out of memory"}
    r2 = train.start("base", [data], export="")
    hub["get"] = {"status": "failed", "error": "out of memory"}
    assert train.status(r2["id"])["error"] == "out of memory"


def test_hub_dying_mid_run_fails_the_run_not_the_caller(monkeypatch, hub, home, data):
    set_backend(monkeypatch, "auto")
    r = train.start("base", [data], export="")
    from bot.modelmistress import client

    def boom(op, args=None, **k):
        raise RuntimeError("connection refused")

    monkeypatch.setattr(client, "call", boom)
    hub["up"] = False
    st = train.status(r["id"])
    assert st["state"] == "failed" and "went away" in st["error"]


def test_auto_falls_back_when_the_forge_refuses(monkeypatch, hub, home, data):
    set_backend(monkeypatch, "auto")
    from bot.modelmistress import client
    real = client.call

    def refuse(op, args=None, **k):
        if op == "forge.run":
            raise RuntimeError("no such step")
        return real(op, args, **k)

    monkeypatch.setattr(client, "call", refuse)
    with pytest.raises(LocalAIError, match="no such step|training environment"):
        train.start("base", [data])
