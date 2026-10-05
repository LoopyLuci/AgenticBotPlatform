"""NOEMA as a Neural Lab model family (bot/neurallab/noema.py): its checkpoints, their configs, and one
real rollout.

Listing and config are checked against a real checkout laid out the way NOEMA's own is - `configs/*.yaml`
(the three layers `noema.core.config.load_config` reads) and a checkpoint folder with `.pt`, `.ckpt` and a
metrics sidecar - so the resolution order (`$NOEMA_HOME`, `$NOEMA_CKPT_DIR`, then the repo) is exercised
without needing the owner's machine.

The rollout is a live test: it runs NOEMA's own CLI in the real training environment with real torch, so it
skips where NOEMA or that interpreter is not installed. It asks for random weights, which is exactly what
NOEMA's CLI does with no `--ckpt`, because the checkpoint NOEMA ships has drifted from the code that reads
it (`phase3.pt` carries `byte_head.*`, which `NoemaRuntime` no longer has, and `configs/tiny_8m.yaml` now
builds `patcher.boundary_*`, which it does not have). That drift is NOEMA's to fix; what ABP owes is to
report the failure rather than hide it, which the checkpoint branch below checks.
"""
from __future__ import annotations

import os
import time
import tomllib
from pathlib import Path

import pytest

TINY = """\
layer1:                     # the byte-level patcher
  d_model: 128
  vocab_size: 256
  max_patch_size: 16
  num_local_layers: 2       # must match the trained checkpoint
  patch_pooling: sqrt
  boundary_k: 4
layer2:                     # the sparse core
  d_model: 128
  num_layers: 4
  num_attention_layers: 1
  d_state: 16
  num_experts: 8
  active_experts: 2
layer3:
  d_model: 128
  max_thought_steps: 4
"""
EDGE = """\
layer1:
  d_model: 512
  vocab_size: 256
layer2:
  d_model: 512
  num_layers: 32
  num_experts: 64
  active_experts: 4
layer3:
  d_model: 512
  max_thought_steps: 8
"""


def a_checkout(root: Path) -> Path:
    """A checkout with NOEMA's own layout: the three layers as YAML configs, `src/` on the path, and a
    checkpoint folder holding a `.pt` (the suffix NOEMA's own list_checkpoints accepts), a `.ckpt` (what
    train.py's `_save_ckpt` writes) and a metrics sidecar."""
    r = root / "NOEMA"
    (r / "configs").mkdir(parents=True)
    (r / "configs" / "tiny_8m.yaml").write_text(TINY, encoding="utf-8")
    (r / "configs" / "edge_8gb.yaml").write_text(EDGE, encoding="utf-8")
    (r / "src" / "noema").mkdir(parents=True)
    (r / "src" / "noema" / "__init__.py").write_text("", encoding="utf-8")
    ck = r / "checkpoints"
    ck.mkdir()
    for name, age_s in (("phase3.pt", 4000), ("e5_bnd.ckpt", 10), ("smoke.json", 4000)):
        (ck / name).write_bytes(b"\0" * 64)
        os.utime(ck / name, (time.time() - age_s, time.time() - age_s))
    return r


@pytest.fixture
def foundry(tmp_path, monkeypatch):
    """NOEMA_HOME and NOEMA_CKPT_DIR point at a real checkout built here, so nothing in these tests can
    reach the owner's own project or the external folder noema/paths.py prefers."""
    from bot.neurallab import noema

    r = a_checkout(tmp_path)
    monkeypatch.setenv("NOEMA_HOME", str(r))
    monkeypatch.setenv("NOEMA_CKPT_DIR", str(r / "checkpoints"))
    monkeypatch.setenv("ABP_LOCALAI_HOME", str(tmp_path / "localai"))
    return noema


def test_checkpoints_are_listed_newest_first_from_noema_s_own_folder_order(foundry, tmp_path, monkeypatch):
    assert foundry.repo() == tmp_path / "NOEMA"
    assert foundry.ckpt_dir() == tmp_path / "NOEMA" / "checkpoints"
    rows = foundry.checkpoints()
    assert [r["name"] for r in rows] == ["e5_bnd.ckpt", "phase3.pt"]          # .ckpt counts; the sidecar does not
    assert all(r["bytes"] == 64 and r["path"].endswith(r["name"]) and r["mtime_iso"] for r in rows)
    assert rows[0]["age_s"] < 60 and rows[1]["age_s"] > 3000
    assert len(foundry.checkpoints(1)) == 1
    # noema/paths.py's own order: $NOEMA_CKPT_DIR first (absolute, or relative to the repo), then the
    # external X:/NOEMA/checkpoints whenever that drive is there, and only then the repo's own folder
    monkeypatch.setenv("NOEMA_CKPT_DIR", "checkpoints")
    assert foundry.ckpt_dir() == tmp_path / "NOEMA" / "checkpoints"
    monkeypatch.delenv("NOEMA_CKPT_DIR")
    external = foundry._root_available(Path("X:/NOEMA/checkpoints"))
    assert foundry.ckpt_dir() == (Path("X:/NOEMA/checkpoints") if external else tmp_path / "NOEMA" / "checkpoints")
    monkeypatch.setenv("NOEMA_CKPT_DIR", str(tmp_path / "elsewhere"))
    assert foundry.ckpt_dir() == tmp_path / "elsewhere" and foundry.checkpoints() == []
    # a checkout that is not there is reported as absent, and its checkpoint folder is still readable
    monkeypatch.setenv("NOEMA_HOME", str(tmp_path / "gone"))
    monkeypatch.setenv("NOEMA_CKPT_DIR", str(tmp_path / "NOEMA" / "checkpoints"))
    assert foundry.repo() is None and foundry.status()["present"] is False
    assert len(foundry.checkpoints()) == 2
    monkeypatch.setenv("NOEMA_HOME", str(tmp_path / "NOEMA"))
    st = foundry.status()
    assert st["present"] and st["checkpoints"] == 2 and st["configs"] == 2 and st["trainable"] is False


def test_a_checkpoint_is_found_by_name_by_repo_path_or_by_absolute_path(foundry, tmp_path):
    r = foundry.repo()
    assert foundry.checkpoint_path("phase3.pt") == r / "checkpoints" / "phase3.pt"
    assert foundry.checkpoint_path("checkpoints/phase3.pt") == r / "checkpoints" / "phase3.pt"
    assert foundry.checkpoint_path(str(r / "checkpoints" / "phase3.pt")).is_file()
    assert foundry.checkpoint_path("never-trained.pt") == Path("never-trained.pt")   # not invented


def test_a_config_is_read_as_the_three_layers_its_own_loader_reads(foundry, tmp_path):
    from bot.localai.paths import LocalAIError

    got = foundry.config()
    assert got["name"] == "tiny_8m"
    assert got["path"] == str(tmp_path / "NOEMA" / "configs" / "tiny_8m.yaml")
    assert set(got["config"]) == {"layer1", "layer2", "layer3"}
    assert got["config"]["layer1"]["num_local_layers"] == 2 and got["config"]["layer2"]["num_experts"] == 8
    assert got["summary"] == {"d_model": 128, "vocab_size": 256, "max_patch_size": 16, "num_local_layers": 2,
                              "patch_pooling": "sqrt", "boundary_k": 4, "num_layers": 4, "num_attention_layers": 1,
                              "num_experts": 8, "active_experts": 2, "max_thought_steps": 4}
    # a stem, a repo-relative path, an absolute path, and the wide config's own numbers
    assert foundry.config("edge_8gb")["summary"]["d_model"] == 512
    assert foundry.config("configs/edge_8gb.yaml")["path"] == str(tmp_path / "NOEMA" / "configs" / "edge_8gb.yaml")
    assert foundry.config(str(tmp_path / "NOEMA" / "configs" / "edge_8gb.yaml"))["summary"]["num_layers"] == 32
    assert [c["name"] for c in foundry.configs()] == ["edge_8gb", "tiny_8m"]
    with pytest.raises(LocalAIError, match="no NOEMA config"):
        foundry.config("configs/never.yaml")
    bad = tmp_path / "bad.yaml"
    bad.write_text("layer1: [1, 2\n", encoding="utf-8")
    with pytest.raises(LocalAIError, match="not valid YAML"):
        foundry.config(str(bad))
    flat = tmp_path / "flat.yaml"
    flat.write_text("- one\n- two\n", encoding="utf-8")
    with pytest.raises(LocalAIError, match="not a NOEMA config"):
        foundry.config(str(flat))


def test_the_lab_routes_list_noema_and_show_a_config(foundry):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from bot.dashboard import localai_api
    app = FastAPI()
    localai_api.register(app, lambda: None)
    c = TestClient(app)
    o = c.get("/api/lab/noema").json()
    assert o["status"]["present"] and o["status"]["checkpoints"] == 2
    assert o["default_config"] == "configs/tiny_8m.yaml"
    assert [x["name"] for x in o["checkpoints"]] == ["e5_bnd.ckpt", "phase3.pt"]
    assert [x["name"] for x in o["configs"]] == ["edge_8gb", "tiny_8m"]
    assert Path(o["status"]["interpreter"]).parts[-3:] == ("venv-train", "Scripts", "python.exe")
    assert c.get("/api/lab/noema/config", params={"config": "edge_8gb"}).json()["summary"]["num_experts"] == 64


def test_the_cli_offers_the_same_three_questions():
    from abp_cli.__main__ import _parser

    assert _parser().parse_args(["lab", "noema"]).lab_cmd == "noema"
    a = _parser().parse_args(["lab", "noema-config", "edge_8gb"])
    assert (a.lab_cmd, a.config) == ("noema-config", "edge_8gb")
    g = _parser().parse_args(["lab", "noema-generate", "phase3.pt", "Once", "upon", "a", "time", "--max-new", "8"])
    assert (g.lab_cmd, g.ckpt, g.prompt, g.max_new) == ("noema-generate", "phase3.pt", ["Once", "upon", "a", "time"], 8)
    assert _parser().parse_args(["lab", "noema-generate", "random", "hi"]).ckpt == "random"


def test_generation_refuses_what_it_cannot_do(foundry):
    from bot.localai.paths import LocalAIError

    with pytest.raises(LocalAIError, match=r"max_new is 1\.\.4096"):
        foundry.generate(max_new=0)
    with pytest.raises(LocalAIError, match=r"max_new is 1\.\.4096"):
        foundry.generate(max_new=99_999)
    with pytest.raises(LocalAIError, match="no NOEMA config"):
        foundry.generate(config_name="configs/never.yaml")
    with pytest.raises(LocalAIError, match="no NOEMA checkpoint"):
        foundry.generate(ckpt="never-trained.pt")
    with pytest.raises(LocalAIError, match="training environment is not set up"):
        foundry.generate(max_new=4)                        # this fixture points ABP_LOCALAI_HOME at tmp


def test_the_generated_line_is_read_out_of_noema_s_own_output():
    from bot.neurallab import noema

    assert noema._generated("noise\nGenerated: b'ab\\ncd'\nVRAM used: 0.00 GB\n") == b"ab\ncd"
    assert noema._generated("Generated: b'first'\nGenerated: b'last'\n") == b"last"   # stderr merged: last wins
    assert noema._generated("VRAM used: 0.00 GB\n") is None
    assert noema._generated("Generated: not bytes\n") is None
    assert noema._generated("Generated: b'unterminated\n") is None


def test_the_lab_runs_the_very_cli_the_overlay_runs():
    """The lab asks for bytes with NOEMA's own command, so it must be the one catalog/noema's
    generate.run runs: same module, same flags, the same default config, and the same interpreter."""
    import sys

    overlay = Path(__file__).resolve().parent.parent / "catalog" / "noema" / "abp-ops.toml"
    ops = {o["id"]: o for o in tomllib.loads(overlay.read_text(encoding="utf-8"))["op"]}
    from bot.neurallab import noema

    run = ops["generate.run"]
    assert run["argv"][1:] == ["-m", "noema.cli.main", "--config", noema.DEFAULT_CONFIG, "--ckpt", "{ckpt}",
                               "--prompt", "{prompt}", "--max-new", "{max_new}"]
    exe = Path(run["argv"][0])
    assert exe.name == "python.exe" and "venv-train" in exe.parts and run["argv"][0] != sys.executable
    assert exe == noema.python() or exe.name == "python"            # ABP's own training environment
    assert run["env"]["PYTHONPATH"] == "{project}/src"               # src/ on the path, never installed
    assert {k: run["env"][k] for k in run["env"] if "THREADS" in k} == {"OMP_NUM_THREADS": "4", "MKL_NUM_THREADS": "4",
                                                                            "NOEMA_THREADS": "4"}


def _live() -> tuple:
    """The real thing, when this machine has it: the owner's checkout and an interpreter with torch."""
    from bot.neurallab import noema

    r = noema.repo()
    return (r, noema.python()) if r and noema.python().is_file() else (None, None)


@pytest.mark.parametrize("with_checkpoint", [False, True])
def test_a_rollout_runs_noema_s_own_cli_as_a_sandbox_worker(tmp_path, monkeypatch, with_checkpoint):
    """A real process, real torch, real bytes - and nothing left behind. It skips where NOEMA or its
    training environment is not installed; nothing about the code under test is mocked either way."""
    from bot.sandbox_ns.registry import registry

    checkout, py = _live()
    if not checkout or not py.is_file():
        pytest.skip("NOEMA or its training environment is not on this machine")
    from bot.neurallab import noema

    monkeypatch.delenv("NOEMA_HOME", raising=False)
    monkeypatch.delenv("NOEMA_CKPT_DIR", raising=False)
    # NOEMA's own config, copied out of the checkout so the run writes nothing into it
    own = noema._config_path(noema.DEFAULT_CONFIG)
    cfg = tmp_path / "tiny_8m.yaml"
    cfg.write_text(own.read_text(encoding="utf-8"), encoding="utf-8")
    before = cfg.read_bytes()
    ck = next((c["name"] for c in noema.checkpoints() if c["name"].endswith((".pt", ".ckpt"))), "")
    mine = [r for r in registry.records() if r.owner == "neurallab.noema"]

    r = noema.generate(ckpt=ck if with_checkpoint else "", prompt="Once upon a time", max_new=4,
                       config_name=str(cfg), timeout=600)
    assert r["config"] == str(cfg) and cfg.read_bytes() == before and r["threads"] >= 1 and r["seconds"] > 0
    assert r["checkpoint"].endswith(ck if with_checkpoint else "")
    if not with_checkpoint:
        # NOEMA's own CLI with no --ckpt: a rollout from freshly initialised weights, and real bytes back
        assert r["exit_code"] == 0, r["output"][-1500:]
        assert r["ok"] and r["text"].startswith("Once upon a time")
        assert (r["prompt_bytes"], r["new_bytes"], r["n_bytes"]) == (16, 4, 20)
        assert bytes(r["bytes"]) == bytes.fromhex(r["bytes_hex"])
        assert r["text"].startswith("Once upon a time")
        assert "VRAM used" in r["output"]
    else:
        # a real checkpoint on disk: either it generates, or NOEMA's own loader says why it cannot - and
        # either way ABP reports it rather than swallowing it
        assert (r["ok"] and r["n_bytes"] >= r["prompt_bytes"]) or (r["error"] and r["exit_code"] != 0)
    # it ran as a sandbox worker in a cell that the call took down with it
    rec = [x for x in registry.records() if x.owner == "neurallab.noema" and x not in mine]
    spawned = [x for x in rec if x.spawned()]
    assert len(spawned) == 1 and spawned[0].name.startswith("noema-generate:") and spawned[0].policy == "worker"
    assert spawned[0].cell and all(c.id != spawned[0].cell for c in registry.cells())      # the cell is closed
    # ... and what that spawn started is recorded too: NOEMA runs out of its own venv, whose
    # `python.exe` is a launcher on Windows, so the process that did the work is its child.
    assert all((x.owner, x.cell, x.policy) == (spawned[0].owner, spawned[0].cell, "worker") for x in rec), rec
    assert any(x.parent_pid for x in rec), f"only the launcher was recorded: {rec}"
