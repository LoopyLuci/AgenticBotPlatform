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


def test_dashboard_routes_for_models_lab_and_projects(home, tmp_path):
    """Every route the Local AI and Neural Lab pages use, on real files; the projects' own designs and checkpoints
    (BrainBuilder graphs, KotMoE's registry and checkpoints) where those projects sit next to ABP."""
    from fastapi import FastAPI
    from bot.dashboard import localai_api
    from bot.neurallab import interop, telemetry
    app = FastAPI()
    localai_api.register(app, lambda: None)
    c = TestClient(app)
    A, L = "/api/localai", "/api/lab"
    assert c.get(A).status_code == 200 and c.get(f"{A}/settings").json()["keep_alive_s"] == 300
    assert c.put(f"{A}/settings", json={"keep_alive_s": 60}).json()["keep_alive_s"] == 60
    assert c.put(f"{A}/settings", json={"nope": 1}).status_code == 400
    assert c.post(f"{A}/server/dance").status_code == 404
    assert c.post(f"{A}/models/pull", json={}).status_code == 400
    g = write_gguf(tmp_path / "w" / "tiny-Q4_K_M.gguf")
    assert c.post(f"{A}/models/import", json={"name": "me/tiny:q4", "path": str(g)}).json()["name"] == "me/tiny:q4"
    assert c.post(f"{A}/models/import", json={"name": "me/ref:q4", "path": str(g), "reference": True}).status_code == 200
    assert c.post(f"{A}/models/copy", json={"source": "me/tiny:q4", "destination": "me/two:q4"}).json() == {"copied": True}
    made = c.post(f"{A}/models/create", json={"name": "me/sys:v1", "modelfile": "FROM me/tiny:q4\nSYSTEM hi"}).json()
    assert made["name"] == "me/sys:v1"
    assert c.get(f"{A}/models/show", params={"name": "me/sys:v1"}).json()["modelfile"].count("SYSTEM") == 1
    assert c.get(f"{A}/models/show", params={"name": "none:1"}).status_code == 400
    assert c.delete(f"{A}/models", params={"name": "me/two:q4"}).json() == {"deleted": True}
    assert {m["name"] for m in c.get(f"{A}/models").json()} == {"me/tiny:q4", "me/ref:q4", "me/sys:v1"}
    assert "found" in c.get(f"{A}/discover").json()
    store = tmp_path / "other-store"
    store.mkdir()
    assert c.put(f"{A}/stores", json=[{"name": "Other", "path": str(store)}]).status_code == 400       # no manifests/
    (store / "manifests").mkdir()
    assert c.put(f"{A}/stores", json=[{"name": "Other", "path": str(store)}]).status_code == 200
    assert c.get(f"{A}/stores").json()[0]["name"] == "Other"
    tr = c.get(f"{A}/train").json()
    assert "q4_k_m" in tr["quants"] and tr["defaults"]["rank"] == 16
    assert c.post(f"{A}/train", json={"base": ""}).status_code == 400
    assert c.get(f"{A}/train/19990101-000000").status_code == 400
    # the lab
    spec = {"name": "tiny-net", "input": {"kind": "features", "size": 4}, "layers": [{"op": "linear", "out": 2}]}
    assert c.put(f"{L}/designs/tiny-net", json={"spec": spec}).status_code == 200
    assert [d["name"] for d in c.get(f"{L}/designs").json()] == ["tiny-net"]
    assert c.get(f"{L}/designs/tiny-net").json()["name"] == "tiny-net"
    assert c.post(f"{L}/runs", json={}).status_code == 400
    assert c.post(f"{L}/runs", json={"design": "tiny-net"}).status_code == 400
    assert c.get(f"{L}/runs").json() == [] and c.get(f"{L}/runs/none").status_code == 400
    assert c.get(L).status_code == 200
    assert c.post(f"{L}/import", json={"kind": "onnx"}).status_code == 400
    out = tmp_path / "net.bbir.edn"
    assert c.post(f"{L}/export/brainbuilder", json={"spec": spec, "path": str(out)}).json()["path"] == str(out)
    back = c.post(f"{L}/import", json={"kind": "brainbuilder", "path": str(out), "name": "round-trip"}).json()
    assert "parameters 10" in back["text"] and "round-trip" in {d["name"] for d in c.get(f"{L}/designs").json()}
    assert {p["id"] for p in c.get(f"{L}/projects").json()} == {"brainbuilder", "kotmoe", "amethyst", "kestrion"}
    bb = c.get(f"{L}/brainbuilder").json()
    if interop.project_dir("brainbuilder"):
        assert bb and all("params" in g or "error" in g for g in bb)
    km = c.get(f"{L}/kotmoe").json()
    if km["registry"]:
        imp = c.post(f"{L}/import", json={"kind": "kotmoe-registry", "id": km["registry"][0]["id"], "save": False}).json()
        assert imp["spec"]["input"]["size"] == 784 and imp["spec"]["origin"]["project"] == "kotmoe"
    assert c.post(f"{L}/import", json={"kind": "kotmoe-registry", "id": "no-such-id"}).status_code == 404
    if km["checkpoints"]:
        imp = c.post(f"{L}/import", json={"kind": "kotmoe-checkpoint", "path": km["checkpoints"][0]["path"], "save": False}).json()
        assert imp["spec"]["origin"]["design"] == "kotmoe-gen"
    assert c.post(f"{L}/import", json={"kind": "kotmoe-checkpoint", "path": str(out)}).status_code == 400
    assert c.post(f"{L}/systune/bench", json={"kind": "memory"}).status_code == 400
    assert c.post(f"{L}/systune/train", json={"kind": "transfer"}).status_code == 400       # nothing measured yet
    for kind in ("transfer", "llm", "memory", "stability"):
        assert c.get(f"{L}/systune/advice", params={"kind": kind, "src": str(tmp_path), "dst": str(tmp_path), "model": "me/tiny:q4"}).status_code == 200
    assert c.get(f"{L}/systune/advice", params={"kind": "weather"}).status_code == 400
    assert set(c.get(f"{L}/systune").json()["models"]) == {"transfer", "llm", "memory", "stability"}
    telemetry.record(telemetry.sample())
    assert c.get(f"{L}/telemetry", params={"seconds": 60}).json()["stats"]["samples"] == 1
    assert len(c.get(f"{L}/telemetry/hw").json()["topology"]) >= 1
    k = interop.kestrion_status()
    assert isinstance(k, dict)


def test_the_agents_tools_run_against_real_files(home, tmp_path):
    import asyncio
    from bot.agent_runtime import toolspec
    import bot.localai.tools  # noqa: F401  registers

    def call(name, inp):
        return asyncio.run(toolspec._registered[name][2](inp))

    g = write_gguf(tmp_path / "w" / "t.gguf")
    assert '"name": "me/t:q4"' in call("localai_models", {"action": "import", "name": "me/t:q4", "path": str(g)})
    assert "\"name\": \"me/r:q4\"" in call("localai_models", {"action": "reference", "name": "me/r:q4", "path": str(g)})
    assert '"copied": true' in call("localai_models", {"action": "copy", "name": "me/t:q4", "destination": "me/c:q4"})
    assert "me/m:v1" in call("localai_models", {"action": "create", "name": "me/m:v1", "modelfile": "FROM me/t:q4"})
    assert '"deleted": true' in call("localai_models", {"action": "delete", "name": "me/c:q4"})
    assert call("localai_models", {"action": "fly", "name": "x"}).startswith("Error: ValueError")
    st = json.loads(call("localai_status", {}))
    assert {m["name"] for m in st["models"]} == {"me/t:q4", "me/r:q4", "me/m:v1"}
    assert "found" in json.loads(call("localai_discover", {}))
    assert "no training run" in call("localai_train", {"action": "stop", "run": "19990101-000000"}) or \
        call("localai_train", {"action": "stop", "run": "19990101-000000"}).startswith("Error")
    assert call("localai_train", {"action": "dance"}).startswith("Error: ValueError")
    assert "no such data file" in call("localai_train", {"action": "start", "base": "x", "data": [str(tmp_path / "none.jsonl")]}) or \
        "not set up" in call("localai_train", {"action": "start", "base": "x", "data": [str(tmp_path / "none.jsonl")]})
    d = json.loads(call("lab_design", {"spec": {"input": {"kind": "features", "size": 8}, "layers": [{"op": "linear", "out": 2}]}}))
    assert d["stats"]["params"] == 18
    assert call("lab_design", {"spec": {"input": {"kind": "features", "size": 8}, "layers": [{"op": "nope"}]}}).startswith("Error")
    assert "designs" in json.loads(call("lab_status", {}))
    from bot.neurallab import interop
    out = interop.to_bbir({"name": "n", "input": {"kind": "features", "size": 4}, "layers": [{"op": "linear", "out": 2}]},
                          tmp_path / "n.bbir.edn")
    assert "parameters 10" in json.loads(call("lab_import", {"kind": "brainbuilder", "path": str(out), "name": "from-bb"}))["text"]
    assert call("lab_import", {"kind": "onnx"}).startswith("Error: ValueError")
    assert call("lab_train", {"spec": {"input": {"kind": "features", "size": 4}, "layers": [{"op": "linear", "out": 1}]},
                              "data": {"path": str(tmp_path / "none.npz")}}).startswith("Error")
    for action, extra in (("status", {}), ("advise_copy", {"src": str(tmp_path), "dst": str(tmp_path), "size_gb": 0.001}),
                          ("advise_llm", {"model": "me/t:q4"}), ("forecast_memory", {}), ("cpu_policy", {})):
        assert not call("lab_systune", {"action": action, **extra}).startswith("Error"), action
    assert "measurements so far" in call("lab_systune", {"action": "train", "kind": "llm"})
    assert call("lab_systune", {"action": "sing"}).startswith("Error: ValueError")


def test_sizes_must_be_whole_and_positive():
    from bot.neurallab import spec
    base = {"input": {"kind": "features", "size": 8}}
    for bad in (0, -3, 2.5, "8", True):
        with pytest.raises(spec.SpecError, match="whole number of at least 1"):
            spec.validate({**base, "layers": [{"op": "linear", "out": bad}]})
    with pytest.raises(spec.SpecError, match="experts"):
        spec.validate({**base, "layers": [{"op": "moe", "experts": 0, "hidden": 4}]})
    assert spec.validate({**base, "layers": [{"op": "linear", "out": 4.0}]})["stats"]["params"] == 36


def test_pulls_resume_verify_and_skip_what_is_there(home, tmp_path, monkeypatch):
    """Ollama's registry API and a plain URL, served by a stand-in that honours Range requests: blobs land under their
    digests, an interrupted download continues where it stopped, a damaged one is refused, shared blobs are skipped."""
    import hashlib

    import httpx

    from bot.localai import models, pull
    from bot.localai.paths import LocalAIError
    weights = write_gguf(tmp_path / "src.gguf", arch="qwen2", pad=300_000).read_bytes()
    config = json.dumps({"model_format": "gguf", "model_family": "qwen2"}).encode()
    blobs = {"sha256:" + hashlib.sha256(b).hexdigest(): b for b in (weights, config)}
    wd, cd = list(blobs)
    manifest = {"schemaVersion": 2, "mediaType": models.MANIFEST_MT, "config": {"digest": cd, "size": len(config),
                "mediaType": "application/vnd.docker.container.image.v1+json"},
                "layers": [{"digest": wd, "size": len(weights), "mediaType": "application/vnd.ollama.image.model"}]}
    served = {"ranges": [], "damage": False}

    def registry(req: httpx.Request) -> httpx.Response:
        p = req.url.path
        if p == "/v2/library/tiny/manifests/1b":
            return httpx.Response(200, json=manifest)
        if p.startswith("/v2/library/") and "/manifests/" in p:
            return httpx.Response(404, json={"errors": [{"code": "MANIFEST_UNKNOWN"}]})
        body = blobs.get(p.rsplit("/", 1)[-1]) if "/blobs/" in p else (weights if p == "/files/tiny.gguf" else None)
        if body is None:
            return httpx.Response(404)
        if served["damage"]:
            body = body[:-1] + b"X"
        rng = req.headers.get("range")
        served["ranges"].append(rng)
        if rng:
            start = int(rng.split("=")[1].rstrip("-"))
            return httpx.Response(206, content=body[start:], headers={"content-length": str(len(body) - start)})
        return httpx.Response(200, content=body, headers={"content-length": str(len(body))})
    monkeypatch.setattr(pull, "_TRANSPORT", httpx.MockTransport(registry))

    part = models.blob_path(wd).with_name(models.blob_path(wd).name + "-partial")    # an earlier, interrupted pull
    part.parent.mkdir(parents=True, exist_ok=True)
    part.write_bytes(weights[:100_000])
    seen = []
    out = pull.pull("tiny:1b", seen.append)
    assert out == {"name": "tiny:1b", "layers": 1, "size": len(weights)} and seen[-1] == {"status": "success"}
    assert "bytes=100000-" in served["ranges"] and models.blob_path(wd).read_bytes() == weights
    assert models.resolve("tiny:1b")["weights"] == str(models.blob_path(wd))
    served["ranges"].clear()
    pull.pull("tiny:1b")                                              # everything already here: nothing downloaded
    assert served["ranges"] == []
    with pytest.raises(LocalAIError, match="file does not exist"):
        pull.pull("nothing:1")
    # a plain URL, with its expected digest
    got = pull.pull_url("https://models.example/files/tiny.gguf", "url/tiny:q4", sha256=wd.split(":")[1])
    assert got["name"] == "url/tiny:q4" and got["digest"].endswith(wd.split(":")[1])
    with pytest.raises(LocalAIError, match="does not match"):
        pull.pull_url("https://models.example/files/tiny.gguf", "url/bad:q4", sha256="0" * 64)
    with pytest.raises(LocalAIError, match="HTTP 404"):
        pull.pull_url("https://models.example/files/missing.gguf", "url/none:q4")
    served["damage"] = True                                           # a damaged download is refused, not stored
    models.delete("tiny:1b")
    models.prune()
    with pytest.raises(LocalAIError, match="digest mismatch"):
        pull.pull("tiny:1b")
    assert not models.blob_path(cd).exists() and models.blob_path(wd).exists()     # the weights are still url/tiny's


def test_the_tree_cache_layout_is_listed_and_laid_out_on_use(home, tmp_path, monkeypatch):
    """Some downloaders write a Hugging Face cache with trees/<rev>.json and blobs but no snapshots/ folder: it is
    listed without touching it, and laid out as a standard snapshot (hard links) when a run needs it."""
    import hashlib
    from bot.localai import discover
    hub = tmp_path / "hf" / "hub"
    repo = hub / "models--acme--tiny-llm"
    (repo / "blobs").mkdir(parents=True)
    (hub / "blobs" / "ab").mkdir(parents=True)
    files = {"config.json": b'{"model_type": "llama"}', "tokenizer.json": b"{}", "model.safetensors": b"\0" * 4096}
    tree = {}
    for name, data in files.items():
        if name.endswith(".safetensors"):                       # a large file: kept in the shared, content-addressed store
            sha = "ab" + hashlib.sha256(data).hexdigest()[2:]
            (hub / "blobs" / "ab" / sha).write_bytes(data)
            tree[name] = {"size": len(data), "blob_id": "x" * 40, "lfs_sha256": sha}
        else:
            bid = hashlib.sha1(data).hexdigest()
            (repo / "blobs" / bid).write_bytes(data)
            tree[name] = {"size": len(data), "blob_id": bid}
    tree["README.md"] = {"size": 5, "blob_id": "0" * 40}       # not in the cache: left out
    (repo / "trees").mkdir()
    (repo / "trees" / "rev1.json").write_text(json.dumps({"format_version": 1, "files": tree}))
    monkeypatch.setenv("HF_HUB_CACHE", str(hub))
    found = [f for f in discover._scan_hf(hub, 50) if f.get("repo") == "acme/tiny-llm"]
    assert found and found[0]["layout"] == "trees" and found[0]["tokenizer"] and found[0]["size"] == 4096
    assert not (repo / "snapshots").exists()                       # listing never writes into the cache
    made = discover.materialize_trees(repo)
    snap = repo / "snapshots" / "rev1"
    assert made == [snap] and sorted(p.name for p in snap.iterdir()) == ["config.json", "model.safetensors", "tokenizer.json"]
    assert (snap / "model.safetensors").read_bytes() == files["model.safetensors"]
    assert discover.materialize_trees(repo) == []                    # already laid out
    assert discover._scan_hf(hub, 50)[0]["path"] == str(snap)
