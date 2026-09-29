"""The module framework (bot/modules/): manifests, the registry, the harness, the hub client, the API, the tools and
conformance, against a real tiny hub (tests/fixtures/fake_module) and the built-in modules."""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from bot.modules import client, conformance, harness, manifest as mf, registry
from bot.modules.client import ModuleError

ROOT = Path(__file__).resolve().parent.parent
FIXTURE = ROOT / "tests" / "fixtures" / "fake_module"
FAKE = {"module": {"id": "fake-mod", "name": "FakeMod", "repo": "https://example.invalid/fake-mod.git"},
        "checkout": {"marker": ["hub.py"]}}


# ---- manifests ---------------------------------------------------------------------------------------------------------
def test_every_builtin_manifest_parses():
    ids = [mf.parse(d).id for d in registry.BUILTIN]
    assert len(ids) == len(set(ids))
    assert {"vm-harness", "hermes-manager", "transferdaemon", "modelmistress", "continuum", "tridentdroid",
            "brainbuilder", "wrightspace"} <= set(ids)


@pytest.mark.parametrize("data, message", [
    ({}, "[module] is required"),
    ({"module": {"id": "Bad_ID", "name": "x", "repo": "y"}}, "lowercase"),
    ({"module": {"id": "ok-id", "name": "x"}}, "module.repo is required"),
    ({"module": {"id": "ok-id", "name": "x", "repo": "y", "api": 99}}, "newer than this ABP"),
    ({**FAKE, "hub": {"start": ["x"]}}, "control_file is required"),
    ({**FAKE, "hub": {"control_file": "{data}/c.json", "call": "/call"}}, "must contain {op}"),
    ({**FAKE, "build": {"steps": [["cargo", "{nope}"]]}}, "unknown placeholder {nope}"),
    ({**FAKE, "build": {"steps": "cargo build"}}, "list of commands"),
    ({**FAKE, "checkout": {"marker": ["x"], "subdir": "../escape"}}, "inside the repo"),
    ({**FAKE, "host": {"os": ["plan9"]}}, "unknown"),
])
def test_bad_manifests_are_refused_with_a_clear_reason(data, message):
    with pytest.raises(mf.ManifestError, match=message.replace("[", r"\[").replace("{", r"\{").replace("}", r"\}")):
        mf.parse(data)


def test_placeholders_are_filled_in():
    assert mf.expand("{repo}/x{exe} {op}", {"repo": "R", "exe": ".exe"}) == "R/x.exe {op}"


# ---- the fake module, wired in -------------------------------------------------------------------------------------------
@pytest.fixture
def fake(tmp_path, monkeypatch):
    """The fake module's checkout in tmp, known to the registry, its hub started with this Python."""
    repo = tmp_path / "FakeMod"
    shutil.copytree(FIXTURE, repo)
    toml = (repo / "abp-module.toml").read_text(encoding="utf-8")
    exe = Path(sys.executable).as_posix()
    (repo / "abp-module.toml").write_text(toml.replace('start = ["python"', f'start = ["{exe}"'), encoding="utf-8")
    monkeypatch.setattr(registry, "BUILTIN", [*registry.BUILTIN, FAKE])
    monkeypatch.setenv("ABP_MODULE_FAKE_MOD_DIR", str(repo))
    monkeypatch.setattr(registry, "data_dir", lambda m: tmp_path / "data" / m.id)
    registry.modules(refresh=True)
    yield repo
    try:
        harness.stop_hub("fake-mod")
    except ModuleError:
        pass
    monkeypatch.undo()
    registry.modules(refresh=True)


def test_the_repos_own_manifest_replaces_the_builtin_one(fake):
    m = registry.get("fake-mod")
    assert m.source.endswith("abp-module.toml") and m.hub is not None and m.description == "A test module"
    assert registry.find("FAKEMOD") is m
    assert harness.install_info("fake-mod")["installed"] is True


def test_a_broken_repo_manifest_falls_back_and_says_why(fake):
    (fake / "abp-module.toml").write_text("[module]\nid = 'other-id'\nname = 'x'\nrepo = 'y'\n", encoding="utf-8")
    registry.modules(refresh=True)
    assert registry.get("fake-mod").source == "builtin"
    assert "other-id" in registry.manifest_errors()["fake-mod"]


def test_env_and_config_choose_the_checkout(tmp_path, monkeypatch):
    m = mf.parse(FAKE)
    monkeypatch.delenv("ABP_MODULE_BUILD_CACHE", raising=False)
    monkeypatch.setenv("ABP_MODULE_FAKE_MOD_DIR", str(tmp_path / "here"))
    assert registry.install_dir(m) == tmp_path / "here"
    monkeypatch.delenv("ABP_MODULE_FAKE_MOD_DIR")
    monkeypatch.setattr(registry, "_cfg", lambda: {"fake-mod": {"path": str(tmp_path / "cfg")}, "build_cache": str(tmp_path / "cache")})
    assert registry.install_dir(m) == tmp_path / "cfg"
    assert registry.target_dir(m) == tmp_path / "cache" / "fake-mod"
    monkeypatch.setattr(registry, "_cfg", lambda: {})
    assert registry.install_dir(m) == registry.abp_root() / "data" / "modules" / "FakeMod"


def test_the_hub_starts_answers_and_stops(fake):
    assert harness.hub_state(registry.get("fake-mod"))["running"] is False
    started = harness.start_hub("fake-mod")
    assert started["running"] and not started["already"]
    assert harness.start_hub("fake-mod")["already"] is True
    ops = harness.operations("fake-mod")
    assert {o["id"] for o in ops} == {"echo.say", "counter.add", "counter.get"}
    assert harness.operation("fake-mod", "counter.add")["mutating"] is True
    assert harness.call("fake-mod", "echo.say", {"text": "hi"}) == {"said": "hi"}
    assert harness.call("fake-mod", "counter.add", {"n": 3}) == {"count": 3}
    with pytest.raises(ModuleError, match="no operation"):
        harness.call("fake-mod", "nope.nope")
    assert harness.stop_hub("fake-mod") == {"running": False}
    assert client.find(registry.get("fake-mod")) is None


def test_the_fake_module_passes_conformance(fake):
    result = conformance.check("fake-mod")
    assert result["ok"], result["checks"]
    names = {c["check"] for c in result["checks"]}
    assert {"refuses callers without the token", "runs a read-only operation", "stops when asked"} <= names
    assert client.find(registry.get("fake-mod")) is None       # it stopped the hub it started


def test_a_stale_control_file_is_ignored(fake, tmp_path):
    d = tmp_path / "data" / "fake-mod"
    d.mkdir(parents=True)
    (d / "control.json").write_text('{"url": "http://127.0.0.1:9", "token": "unused", "pid": 1}', encoding="utf-8")
    assert client.find(registry.get("fake-mod")) is None


def test_modules_without_a_hub_say_so(fake):
    with pytest.raises(ModuleError, match="no hub"):
        harness.operations("brainbuilder")
    with pytest.raises(ModuleError, match="no window"):
        harness.open_gui("wrightspace")


# ---- updates never overwrite work ---------------------------------------------------------------------------------------
def _git(d: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(d), *args], check=True, capture_output=True)


def test_update_refuses_over_uncommitted_changes_but_not_untracked_files(fake, tmp_path):
    origin = tmp_path / "origin.git"
    _git(fake, "init", "-q", "-b", "main")
    _git(fake, "-c", "user.name=t", "-c", "user.email=t@t", "add", "-A")
    _git(fake, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "init")
    subprocess.run(["git", "clone", "-q", "--bare", str(fake), str(origin)], check=True)
    _git(fake, "remote", "add", "origin", str(origin))
    _git(fake, "fetch", "-q", "origin")
    _git(fake, "branch", "-q", "--set-upstream-to=origin/main", "main")
    (fake / "notes.txt").write_text("untracked", encoding="utf-8")
    info = harness.install_info("fake-mod")
    assert (info["changed_files"], info["untracked_files"], info["behind"]) == (0, 1, 0)
    (fake / "hub.py").write_text((fake / "hub.py").read_text(encoding="utf-8") + "\n# edited\n", encoding="utf-8")
    job = harness.update("fake-mod")
    for _ in range(200):
        j = harness.job(job["id"])
        if j["state"] != "running":
            break
        import time
        time.sleep(0.05)
    assert j["state"] == "failed" and "uncommitted" in j["error"]
    assert "# edited" in (fake / "hub.py").read_text(encoding="utf-8")


# ---- the API and the tools ----------------------------------------------------------------------------------------------
def test_the_api_lists_starts_calls_and_stops(fake):
    from bot.dashboard import modules_api
    app = FastAPI()
    modules_api.register(app, lambda: None, lambda: None)
    c = TestClient(app)
    ids = {r["id"] for r in c.get("/api/modules").json()["modules"]}
    assert {"fake-mod", "vm-harness", "wrightspace"} <= ids
    assert c.get("/api/modules/fake-mod").json()["module"]["has_hub"] is True
    assert c.get("/api/modules/no-such").status_code == 404
    assert c.post("/api/modules/fake-mod/hub/start").json()["running"] is True
    assert c.post("/api/modules/fake-mod/call", json={"operation": "echo.say", "args": {"text": "a"}}).json() == {"result": {"said": "a"}}
    assert c.post("/api/modules/fake-mod/call", json={}).status_code == 400
    assert c.post("/api/modules/fake-mod/hub/stop").json()["running"] is False
    assert c.post("/api/modules/brainbuilder/gui").status_code == 400


def test_the_tools_are_registered_and_work_locally(fake):
    import asyncio
    from bot.agent_runtime import toolspec
    from bot.modules import tools
    names = {"module_list", "module_status", "module_operations", "module_read", "module_call", "module_setup"}
    assert names <= set(toolspec._registered)
    out = asyncio.run(toolspec._registered["module_status"][2]({"module": "FakeMod"}))
    assert '"fake-mod"' in out
    asyncio.run(toolspec._registered["module_setup"][2]({"module": "fake-mod", "action": "start"}))
    assert "changes something" in asyncio.run(toolspec._registered["module_read"][2](
        {"module": "fake-mod", "operation": "counter.add"}))
    assert '"said": "x"' in asyncio.run(toolspec._registered["module_read"][2](
        {"module": "fake-mod", "operation": "echo.say", "args": {"text": "x"}}))
    assert "no module" in asyncio.run(toolspec._registered["module_status"][2]({"module": "nope"}))
    assert tools.SETUP_ACTIONS["start"] == ("POST", "hub/start")


def test_peers_can_be_allowed_to_control_modules():
    from bot import peers
    assert peers.control_area_of("/api/modules/continuum/call") == "modules"


def test_panel_is_identical_in_both_uis_and_wired():
    a = (ROOT / "bot/dashboard/static/modules-panel.js").read_text(encoding="utf-8")
    b = (ROOT / "desktop-app/ui/modules-panel.js").read_text(encoding="utf-8")
    assert a == b
    for page in ("bot/dashboard/static/dashboard.html", "desktop-app/ui/index.html"):
        html = (ROOT / page).read_text(encoding="utf-8")
        assert 'id="modules"' in html and 'id="mdp-root"' in html and "modules-panel.js" in html


def test_a_peer_can_reach_the_modules_list_itself():
    from bot import peers
    assert peers.control_area_of("/api/modules") == "modules"
    assert peers.control_area_of("/api/modules?x=1") == "modules"
    assert peers.control_area_of("/api/modulesX") is None
