"""bot/localai and bot/neurallab on real files in temporary folders: the Ollama-layout store (import, reference, copy,
delete, Modelfiles), discovery of other programs' models, the Ollama-compatible API, the Neural Lab's design format
(checked against KotMoE's own parameter arithmetic), BrainBuilder's EDN graphs and KotMoE's checkpoints, numpy
inference, telemetry, the system models' fallbacks, the dashboard routes and the pages; and per-call tool approval,
which fails closed. GPU training runs only where the training environment exists (bot/localai/train.py)."""
from __future__ import annotations

import json
import struct
import sys
from pathlib import Path

import numpy as np
import pytest
from starlette.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]


def write_gguf(path: Path, arch: str = "llama", name: str = "tiny", ctx: int = 2048, pad: int = 0) -> Path:
    """A real GGUF v3 file (metadata only, no tensors), padded so discovery treats it as a model."""
    def s(x: str) -> bytes:
        b = x.encode()
        return struct.pack("<Q", len(b)) + b
    kv = [("general.architecture", 8, s(arch)), ("general.name", 8, s(name)),
          (f"{arch}.context_length", 4, struct.pack("<I", ctx)), ("general.file_type", 4, struct.pack("<I", 15))]
    out = b"GGUF" + struct.pack("<IQQ", 3, 0, len(kv))
    for k, t, v in kv:
        out += s(k) + struct.pack("<I", t) + v
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(out + b"\0" * pad)
    return path


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("ABP_LOCALAI_HOME", str(tmp_path / "localai"))
    from bot.neurallab import systune
    systune._cache.clear()
    return tmp_path / "localai"


# ---- per-call approval (bot/agent_runtime/toolspec.py) ----------------------------------------------------------------- #

def test_per_call_approval_fails_closed():
    from bot.agent_runtime import permissions, toolspec, tools

    async def handler(inp, **_):
        return "ok"

    def rule(inp):
        if inp.get("action") == "boom":
            raise RuntimeError("broken check")
        if inp.get("action") == "weird":
            return "no"                                  # not a bool
        return inp.get("action") not in {"read"}

    toolspec.register({"name": "zz_conditional", "description": "test", "input_schema": {"type": "object", "properties": {}}},
                      toolspec.ToolSpec("zz_conditional", "external", origin="registered", needs_approval=rule), handler)
    try:
        assert toolspec.registered_dangerous("zz_conditional", {"action": "read"}) is False
        assert toolspec.registered_dangerous("zz_conditional", {"action": "write"}) is True
        assert toolspec.registered_dangerous("zz_conditional", {}) is True                 # no action: asks
        assert toolspec.registered_dangerous("zz_conditional") is True                     # no input: asks
        assert toolspec.registered_dangerous("zz_conditional", {"action": "boom"}) is True  # a broken check asks
        assert toolspec.registered_dangerous("zz_conditional", {"action": "weird"}) is True  # a non-bool answer asks
        assert tools.is_dangerous("zz_conditional", {"action": "read"}) is False
        assert tools.is_dangerous("zz_conditional") is True
        # the check sees a copy: it can't change what runs
        seen = {"action": "read"}

        def mutating(i):
            i.clear()
            return False
        assert toolspec.approval_needed(toolspec.ToolSpec("x", needs_approval=mutating), seen) is False
        assert seen == {"action": "read"}
        # plan mode treats a conditional tool as one that changes things
        v = permissions.decide("zz_conditional", {"action": "read"}, rules=[], mode="plan")
        assert v.decision == "deny"
        assert toolspec.approval_is_conditional(toolspec.spec_for("zz_conditional"))
    finally:
        toolspec._registered.pop("zz_conditional", None)


def test_lab_tools_ask_only_for_measuring():
    from bot.agent_runtime import tools, toolspec
    import bot.localai.tools  # noqa: F401  registers
    for action in ("status", "advise_copy", "advise_llm", "forecast_memory", "cpu_policy"):
        assert not tools.is_dangerous("lab_systune", {"action": action}), action
    for action in ("bench_drives", "bench_llm", "train", "something-new", None):
        assert tools.is_dangerous("lab_systune", {"action": action}), action
    for name in ("localai_models", "localai_train", "lab_train"):
        assert toolspec.registered_dangerous(name, {"action": "start"}), name
    for name in ("localai_status", "localai_discover", "lab_status", "lab_design", "lab_import"):
        assert not toolspec.registered_dangerous(name, {}), name


# ---- the model store --------------------------------------------------------------------------------------------------- #

def test_store_import_reference_copy_delete(home, tmp_path):
    from bot.localai import models, modelfile
    g = write_gguf(tmp_path / "src" / "tiny-Q4_K_M.gguf")
    models.import_file("me/tiny:q4", str(g))
    names = {m["name"]: m for m in models.listing()}
    assert "me/tiny:q4" in names and names["me/tiny:q4"]["details"]["family"] == "llama"
    rec = models.resolve("me/tiny:q4")
    assert Path(rec["weights"]).read_bytes() == g.read_bytes()                # the same content, by digest
    models.add_reference("other/ref:latest", str(g), origin="test")
    assert models.resolve("other/ref").get("source") == "referenced"
    models.copy("me/tiny:q4", "me/tiny-copy:q4")
    assert "me/tiny-copy:q4" in {m["name"] for m in models.listing()}
    out = modelfile.create("me/tuned:v1", 'FROM me/tiny:q4\nPARAMETER temperature 0.3\nPARAMETER stop "<end>"\n'
                                          'SYSTEM """Be brief."""\nMESSAGE user hi\nMESSAGE assistant hello')
    assert out["parameters"] == {"temperature": 0.3, "stop": ["<end>"]}
    tuned = models.resolve("me/tuned:v1")
    assert tuned["system"] == "Be brief." and tuned["messages"][1] == {"role": "assistant", "content": "hello"}
    assert "PARAMETER temperature 0.3" in modelfile.render(tuned)
    assert models.delete("me/tiny-copy:q4") and models.delete("other/ref")
    assert {m["name"] for m in models.listing()} == {"me/tiny:q4", "me/tuned:v1"}
    with pytest.raises(Exception):
        modelfile.parse("PARAMETER x 1")                                        # no FROM


def test_names_follow_ollama():
    from bot.localai import models
    assert models.canonical("qwen2.5") == "qwen2.5:latest"
    assert models.parse_name("hf.co/Qwen/Qwen2.5-0.5B-Instruct-GGUF:Q4_K_M") == ("hf.co", "Qwen", "Qwen2.5-0.5B-Instruct-GGUF", "Q4_K_M")
    assert models.canonical("registry.ollama.ai/library/llama3:8b") == "llama3:8b"


def test_discover_reference_and_extra_store(home, tmp_path, monkeypatch):
    from bot.localai import discover, engine, models
    folder = tmp_path / "lmstudio-like" / "publisher" / "Some-Model-7B-GGUF"
    write_gguf(folder / "some-model-7b.Q5_K_M.gguf", pad=1 << 20)
    write_gguf(folder / "mmproj-some-model.gguf", pad=1 << 20)                # a projector: not a model of its own
    engine.set_settings({"scan_folders": [str(tmp_path / "lmstudio-like")]})
    # an Ollama-format store elsewhere (another program's, or Ollama's own)
    other = tmp_path / "ollama-models"
    monkeypatch.setenv("ABP_LOCALAI_HOME", str(other.parent / "other-home"))
    models.import_file("library-model:7b", str(write_gguf(tmp_path / "x.gguf")))
    other_store = models.store_root()
    monkeypatch.setenv("ABP_LOCALAI_HOME", str(home))
    found = discover.scan(extra=[str(other_store.parent)])["found"]
    gg = [f for f in found if f["kind"] == "gguf" and "some-model" in f["path"]]
    assert len(gg) == 1 and gg[0]["name"] == "local/some-model-7b:q5_k_m" and gg[0]["projector"].endswith("mmproj-some-model.gguf")
    discover.adopt(gg[0], "reference")
    models.set_extra_stores([{"name": "Other", "path": str(other_store)}])
    names = {m["name"]: m["source"] for m in models.listing()}
    assert names["local/some-model-7b:q5_k_m"].startswith("referenced") and names["library-model:7b"] == "Other"
    assert models.resolve("library-model:7b")["source"] == "Other (read in place)"


# ---- the Ollama-compatible API ------------------------------------------------------------------------------------------ #

def test_ollama_api_without_loading(home, tmp_path):
    from bot.localai import models, server
    models.import_file("me/tiny:q4", str(write_gguf(tmp_path / "t.gguf")))
    c = TestClient(server.build_app())
    assert c.get("/").text == "Ollama is running"
    assert c.get("/api/version").json()["version"]
    tags = c.get("/api/tags").json()["models"]
    assert [m["name"] for m in tags] == ["me/tiny:q4"] and "T" in tags[0]["modified_at"]
    bad = c.post("/api/generate", content=b"{not json")
    assert bad.status_code == 400 and "not valid JSON" in bad.json()["error"]
    missing = c.post("/api/show", json={"model": "nope:1"})
    assert missing.status_code in (400, 404) and "error" in missing.json()
    assert c.get("/v1/models").json()["data"][0]["id"] == "me/tiny:q4"


# ---- the Neural Lab's designs -------------------------------------------------------------------------------------------- #

def test_spec_counts_match_kotmoe():
    """KotMoE's Kotlin model reported 1,202,432 parameters, 938,752 active per token, for this design."""
    from bot.neurallab import spec
    s = spec.validate(spec.moe_lm("k", vocab=4096, ctx=128, dim=128, layers=2, heads=4, experts=4, top_k=2, hidden=256))
    assert s["stats"]["params"] == 1_202_432 and s["stats"]["active_params"] == 938_752
    assert s["nodes"][-1]["shape"] == [128, 4096]


def test_spec_errors_say_what_to_change():
    from bot.neurallab import spec
    bad = [({"input": {"kind": "features", "size": 4}, "layers": [{"op": "attention"}]}, "attention needs a sequence"),
           ({"input": {"kind": "tokens", "vocab": 100, "ctx": 8}, "layers": [{"op": "embedding", "vocab": 50, "dim": 8}]}, "rows"),
           ({"input": {"kind": "features", "size": 4}, "layers": [{"op": "moe", "experts": 2, "top_k": 3, "hidden": 4}]}, "top_k"),
           ({"input": {"kind": "features", "size": 4}, "layers": [{"op": "nope"}]}, "unknown op"),
           ({"input": {"kind": "features", "size": 4}, "nodes": [{"id": "a", "op": "add", "in": ["input", "b"]}]}, "not an earlier node")]
    for s, msg in bad:
        with pytest.raises(spec.SpecError, match=msg):
            spec.validate(s)
    ok = spec.validate(spec.moe_regressor("r", 10, 1, dim=16, experts=4, top_k=2, hidden=32))
    assert ok["stats"]["active_params"] < ok["stats"]["params"]


def test_edn_and_brainbuilder_round_trip(tmp_path):
    from bot.neurallab import edn, interop, spec
    g = edn.loads('{:a [1 2.5 "x\\"y"] :b nil, :c true ; comment\n :d #{:k} :e {:f -3}}')
    assert g == {"a": [1, 2.5, 'x"y'], "b": None, "c": True, "d": ["k"], "e": {"f": -3}}
    graph = ('{:schema-version 1, :graph-id "g1", :name "story", :nodes [{:id "embed", :component "embedding", :hyperparams '
             '{:embedding_dim 16, :vocab_size 30}}, {:id "attn", :component "attention", :hyperparams {:features 16, :num_heads 4}}, '
             '{:id "pool", :component "select_last"}, {:id "proj", :component "linear", :hyperparams {:in_features 16, :out_features 30}}], '
             ':edges [{:from-node "embed", :from-port "output", :to-node "attn", :to-port "input"}, {:from-node "attn", :from-port "output", '
             ':to-node "pool", :to-port "input"}, {:from-node "pool", :from-port "output", :to-node "proj", :to-port "input"}], '
             ':training {:loss "cross_entropy", :optimizer "adam", :hyperparams {:epochs 5, :lr 0.01}, :data_source {:source-type '
             '"text_sequence", :path-or-uri "story.txt", :sequence-length 6, :vocab-size 60}}}')
    f = tmp_path / "story.bbir.edn"
    f.write_text(graph, encoding="utf-8")
    s = interop.from_bbir(f)
    v = spec.validate(s)
    assert s["input"] == {"kind": "tokens", "vocab": 30, "ctx": 6}            # the embedding's size wins over the data's
    assert [n["op"] for n in v["nodes"]] == ["embedding", "attention", "select_last", "linear"]
    back = interop.from_bbir(interop.to_bbir(s, tmp_path / "out.bbir.edn"))
    assert [n["op"] for n in spec.validate(back)["nodes"]] == [n["op"] for n in v["nodes"]]


def test_kotmoe_checkpoint_header_and_mnist_idx(tmp_path):
    from bot.neurallab import interop, spec
    cfg = [4096, 128, 128, 2, 4, 4, 2, 256]
    f = tmp_path / "run" / "model.kmoe"
    f.parent.mkdir()
    f.write_bytes(struct.pack(">ii", 0x4B4D4F45, 1) + struct.pack(">8i", *cfg) + struct.pack(">fi", 0.01, 777))
    h = interop.kmoe_header(f)
    assert h["step"] == 777 and h["experts"] == 4 and abs(h["aux"] - 0.01) < 1e-6
    assert spec.validate(interop.from_kmoe(f))["stats"]["params"] == 1_202_432
    imgs = tmp_path / "img.idx"
    labs = tmp_path / "lab.idx"
    imgs.write_bytes(struct.pack(">IIII", 0x00000803, 3, 2, 2) + bytes(range(12)))
    labs.write_bytes(struct.pack(">II", 0x00000801, 3) + bytes([7, 1, 4]))
    out = interop.idx_to_npz(imgs, labs, tmp_path / "d.npz")
    z = np.load(out["path"])
    assert z["X"].shape == (3, 4) and list(z["y"]) == [7, 1, 4] and z["X"].max() <= 1.0


def test_numpy_inference_matches_hand_computation(tmp_path):
    """A moe_regressor export run by bot/neurallab/infer.py against the same maths written out by hand."""
    from bot.neurallab import infer, spec
    rng = np.random.default_rng(0)
    s = spec.validate(spec.moe_regressor("r", 3, 1, dim=4, experts=3, top_k=2, hidden=5))
    w = {"mods.inp.weight": rng.normal(size=(4, 3)), "mods.inp.bias": rng.normal(size=4),
         "mods.norm.weight": np.ones(4), "mods.norm.bias": np.zeros(4), "mods.norm2.weight": np.ones(4), "mods.norm2.bias": np.zeros(4),
         "mods.experts.router.weight": rng.normal(size=(3, 4)), "mods.head.weight": rng.normal(size=(1, 4)), "mods.head.bias": np.zeros(1)}
    for e in range(3):
        w.update({f"mods.experts.experts.{e}.fc.weight": rng.normal(size=(5, 4)), f"mods.experts.experts.{e}.fc.bias": rng.normal(size=5),
                  f"mods.experts.experts.{e}.proj.weight": rng.normal(size=(4, 5)), f"mods.experts.experts.{e}.proj.bias": rng.normal(size=4)})
    w = {k: v.astype(np.float32) for k, v in w.items()}
    np.savez(tmp_path / "weights.npz", **w)
    (tmp_path / "model.json").write_text(json.dumps({"spec": s, "norm": {"mean": [0, 0, 0], "std": [1, 1, 1], "y_mean": [0], "y_std": [1]},
                                                     "features": ["a", "b", "c"]}))
    m = infer.Model.load(tmp_path)
    x = np.array([[0.5, -1.0, 2.0]], dtype=np.float32)
    gelu = infer.ACT["gelu"]
    ln = lambda v: (v - v.mean(-1, keepdims=True)) / np.sqrt(v.var(-1, keepdims=True) + 1e-5)  # noqa: E731
    a = gelu(x @ w["mods.inp.weight"].T + w["mods.inp.bias"])
    n = ln(a)
    logits = n @ w["mods.experts.router.weight"].T
    p = np.exp(logits - logits.max()) / np.exp(logits - logits.max()).sum()
    top = np.argsort(-p[0])[:2]
    g = p[0, top] / p[0, top].sum()
    mix = sum(gi * (gelu(n @ w[f"mods.experts.experts.{e}.fc.weight"].T + w[f"mods.experts.experts.{e}.fc.bias"])
                    @ w[f"mods.experts.experts.{e}.proj.weight"].T + w[f"mods.experts.experts.{e}.proj.bias"]) for e, gi in zip(top, g))
    want = ln(a + mix) @ w["mods.head.weight"].T + w["mods.head.bias"]
    got = m.predict({"a": 0.5, "b": -1.0, "c": 2.0})
    assert np.allclose(got, want, atol=1e-4), (got, want)


# ---- telemetry and the system models ---------------------------------------------------------------------------------- #

def test_telemetry_reads_this_machine(home):
    import psutil
    from bot.neurallab import telemetry
    topo = telemetry.topology()
    assert len(topo) == psutil.cpu_count()
    apics = [r["apic"] for r in topo if r["apic"] is not None]
    assert len(apics) == len(set(apics))
    if sys.platform == "win32":
        assert apics, "CPUID should give every processor's APIC id"
    s = telemetry.sample()
    assert 0 <= s["cpu"] <= 100 and 0 < s["ram_used"] < 100
    telemetry.record(s)
    assert telemetry.stats()["samples"] == 1 and telemetry.history(60)[0]["t"] == s["t"]
    hw = telemetry.hw_errors(days=30)
    assert set(hw) == {"machine_checks", "by_apic", "power_losses"}


def test_system_models_fall_back_without_a_model(home, monkeypatch):
    import psutil
    from bot.neurallab import systune
    monkeypatch.setattr(systune, "drives", lambda refresh=False, cached_only=False: {})
    small = systune.advise_copy("C:/a", "D:/b", 1 << 20, 1)
    assert small["parallel"] == 4 and "memory" in small["source"]
    big = systune.advise_copy("C:/a", "D:/b", psutil.virtual_memory().total * 2, 100)
    assert big["source"].startswith("default") and big["chunk"] == 8 << 20
    assert systune.advise_llm("anything") == {} and systune.forecast_memory() is None
    with pytest.raises(Exception, match="measurements so far"):
        systune.train_model("transfer")
    pol = systune.cpu_policy()
    assert set(pol["allowed"]).isdisjoint(pol["avoid"]) and pol["threads"] >= 1


# ---- the dashboard routes, the CLI, the pages ------------------------------------------------------------------------- #

def test_dashboard_routes(home, tmp_path):
    from fastapi import FastAPI
    from bot.dashboard import localai_api
    from bot.localai import models
    models.import_file("me/tiny:q4", str(write_gguf(tmp_path / "t.gguf")))
    app = FastAPI()
    localai_api.register(app, lambda: None)
    c = TestClient(app)
    assert [m["name"] for m in c.get("/api/localai/models").json()] == ["me/tiny:q4"]
    v = c.post("/api/lab/validate", json={"spec": {"input": {"kind": "features", "size": 8}, "layers": [{"op": "linear", "out": 2}]}}).json()
    assert v["spec"]["stats"]["params"] == 18 and "parameters 18" in v["text"]
    assert c.post("/api/lab/validate", json={"spec": {"input": {"kind": "features", "size": 8}, "layers": [{"op": "zzz"}]}}).status_code == 400
    assert c.get("/api/lab/systune/advice", params={"kind": "stability"}).json()["threads"] >= 1
    assert c.get("/api/localai/runs/none").status_code == 404


def test_cli_parses():
    import argparse
    from abp_cli import ai
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd")
    ai.add_parser(sub)
    a = p.parse_args(["ai", "train", "start", "unsloth/qwen3-1.7b", "d.jsonl", "--steps", "50"])
    assert a.ai_cmd == "train" and a.rest == ["unsloth/qwen3-1.7b", "d.jsonl"] and a.steps == 50
    a = p.parse_args(["lab", "advice", "transfer", "E:\\", "D:\\", "--size-gb", "20"])
    assert a.lab_cmd == "advice" and a.size_gb == 20


def test_pages_identical_and_wired():
    a = (ROOT / "bot" / "dashboard" / "static" / "ai-panel.js").read_bytes()
    assert a == (ROOT / "desktop-app" / "ui" / "ai-panel.js").read_bytes()
    for page in (ROOT / "bot" / "dashboard" / "static" / "dashboard.html", ROOT / "desktop-app" / "ui" / "index.html"):
        html = page.read_text(encoding="utf-8")
        for needle in ('id="aip-root"', 'id="lab-root"', 'href="#localai"', 'href="#lab"', "ai-panel.js"):
            assert needle in html, (page.name, needle)


# ---- GPU training (only where the training environment exists) ------------------------------------------------------- #

def _gpu_env() -> bool:
    from bot.localai import train
    return train.python().exists()


@pytest.mark.skipif(sys.platform != "win32" and not Path("/dev/kfd").exists() and not Path("/dev/nvidia0").exists(), reason="no GPU")
def test_lab_trains_on_the_gpu_and_numpy_agrees(tmp_path, monkeypatch):
    from bot.localai import paths as lp
    real_home = lp.home()
    from bot.localai import train
    if not train.python().exists():
        pytest.skip("no training environment (abp ai train setup)")
    monkeypatch.setenv("ABP_LOCALAI_HOME", str(tmp_path / "localai"))
    monkeypatch.setattr(train, "python", lambda: real_home / "venv-train" / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python"))
    from bot.neurallab import infer, lab, spec
    rng = np.random.default_rng(1)
    X = rng.normal(size=(2000, 3)).astype(np.float32)
    y = (3 * X[:, 0] - 2 * X[:, 1] + np.where(X[:, 2] > 0, 1.5, -1.5)).astype(np.float32)   # two regimes: experts help
    np.savez(tmp_path / "d.npz", X=X, y=y, names=np.array(["a", "b", "c"]))
    s = spec.moe_regressor("t", 3, 1, dim=16, experts=4, top_k=2, hidden=32)
    r = lab.start(s, {"path": str(tmp_path / "d.npz")}, {"max_steps": 400, "eval_every": 100, "lr": 3e-3, "batch_size": 128})
    fin = lab.wait(r["id"], 600)
    assert fin["state"] == "done", fin.get("error")
    assert fin["final"]["val_r2"] > 0.95
    m = infer.Model.load(lab.export_of(r["id"])["dir"])
    pred = m.predict(X[:200])[:, 0]
    assert np.corrcoef(pred, y[:200])[0, 1] > 0.97
