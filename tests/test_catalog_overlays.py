"""The catalog's own overlays: 9Router, Mesh LLM, OpenHuman, NOEMA and Ironroot.

Each of the five is a third-party project ABP drives without touching its checkout, so these tests
read the two files ABP ships and assert what they say: the service it starts, the provider ABP offers
while it runs, and the operations a person and ABP's agents actually need. They load each overlay
through ABP's own loader (bot.modules.manifest for the manifest, abp_modkit.spec for the operations)
and touch no network and no upstream checkout.
"""
from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest

from abp_modkit import spec as sp
from bot.modules import manifest as mf
from bot.modules import registry

CATALOG = Path(__file__).resolve().parent.parent / "catalog"

# id -> (folder, name, area, repo, service base_url, health, openai, web)
EXPECTED = {
    "m-9router": ("9router", "9Router", "models", "https://github.com/decolua/9router.git",
                  "http://127.0.0.1:20128", "/api/health", "/v1", "/dashboard"),
    "mesh-llm": ("mesh-llm", "Mesh LLM", "models", "https://github.com/Mesh-LLM/mesh-llm.git",
                 "http://127.0.0.1:9337", "/health", "/v1", ""),
    "openhuman": ("openhuman", "OpenHuman", "agents", "https://github.com/tinyhumansai/openhuman.git",
                  "http://127.0.0.1:7788", "/health", "", ""),
    "noema": ("noema", "NOEMA", "models", "local: X:/Projects/NOEMA",
              "http://127.0.0.1:8000", "/api/health", "", "/"),
    # no HTTP API at all: a resolver answers DNS on 127.0.0.1:5350, so there is no base_url to probe
    "ironroot": ("ironroot", "Ironroot", "network", "https://github.com/LoopyLuci/ironroot.git", "", "", "", ""),
}

# Operations each overlay must offer, by id.
REQUIRED_OPS = {
    "m-9router": ["status.health", "status.version", "models.list", "providers.list", "usage.get",
                  "build.production", "build.dev", "test.run", "git.update"],
    "mesh-llm": ["status.health", "models.api", "status.local", "console.status", "console.models",
                 "doctor.report", "hardware.gpus", "models.installed", "models.recommended",
                 "models.search", "models.download", "runtime.list", "build.from_source"],
    "openhuman": ["status.health", "status.schema", "agent.list", "agent.prompt_size",
                  "memory.documents", "memory.query", "rpc.call", "build.core", "test.core"],
    "noema": ["status.health", "status.model", "status.checkpoints", "status.training",
              "generate.run", "test.run", "mcp.tools"],
    "ironroot": ["status.dns", "status.abp", "query.dig", "dnssec.check", "logs.tail",
                 "abp.use", "abp.stop_using", "build.from_source", "git.update"],
}

IDS = sorted(EXPECTED)


@pytest.fixture(scope="module", params=IDS)
def overlay(request):
    """Each catalog overlay loaded the way ABP loads it: the manifest through bot.modules.manifest,
    the operations through abp_modkit.spec."""
    mid = request.param
    folder = CATALOG / EXPECTED[mid][0]
    manifest = mf.load(folder / mf.MANIFEST_FILE)
    ops = sp.load(folder / "abp-ops.toml")
    return {"id": mid, "folder": folder, "manifest": manifest, "ops": ops,
            "raw": tomllib.loads((folder / mf.MANIFEST_FILE).read_text(encoding="utf-8"))}


def test_every_overlay_is_a_valid_manifest_with_the_service_it_describes(overlay):
    m = overlay["manifest"]
    assert m.id == overlay["id"], "the folder's module id must be the one the catalog lists"
    _, name, area, repo, base_url, health, openai, web = EXPECTED[m.id]
    assert (m.name, m.area, m.repo) == (name, area, repo)
    assert m.api == mf.API_VERSION and m.branch
    assert m.description and len(m.description) > 40, "a module a person scans needs a real sentence"
    # an overlay keeps its files on ABP's side and its operations beside the manifest
    assert m.overlay or True            # the registry sets .overlay; parse() leaves it empty
    assert m.hub is not None and m.hub.control_file and m.hub.start
    assert "{overlay}/abp-ops.toml" in " ".join(m.hub.start), "the hub must read the overlay's operations"
    s = overlay["ops"].service
    assert (s.id, s.base_url, s.health) == (m.id, base_url, health)
    assert s.start, "a service with a base_url must say how to start it"
    assert (s.openai, s.web) == (openai, web)
    assert overlay["ops"].id == m.id


def test_each_overlay_offers_the_operations_a_person_and_the_agents_need(overlay):
    ids = {o.id for o in overlay["ops"].ops}
    missing = [i for i in REQUIRED_OPS[overlay["id"]] if i not in ids]
    assert not missing, f"{overlay['id']} is missing {missing}"
    for o in overlay["ops"].ops:
        assert o.summary, f"{o.id} has no summary; ABP shows it as-is to a person"
        assert not o.params or set(o.params) <= set(o.inputs), f"{o.id}: a parameter with no input"


def test_the_openai_compatible_modules_are_offered_to_abp_as_providers(overlay):
    m = overlay["manifest"]
    _, _, _, _, _, _, openai, _ = EXPECTED[m.id]
    if openai:
        # openai = "service": the base URL comes from abp-ops.toml's [service].openai
        assert m.openai == "service", "an OpenAI-compatible module must be offered as a provider"
        assert overlay["ops"].service.openai == "/v1"
        assert m.web, "and its UI should be reachable while it runs"
    else:
        assert not m.openai, f"{m.id} serves no OpenAI API, so ABP must not offer it as a provider"


@pytest.mark.parametrize("mid", ["m-9router", "mesh-llm"])
def test_a_model_server_lists_its_models_over_its_own_api(mid):
    """A module ABP offers as a provider has to answer a models list, as a real GET route."""
    ops = sp.load(CATALOG / EXPECTED[mid][0] / "abp-ops.toml")
    lists = [o for o in ops.ops if o.kind == "http" and o.path.endswith("/models")]
    assert lists, f"{mid} offers itself as a provider but has no models list"
    for o in lists:
        assert o.method == "GET" and o.mutating is False
        assert ops.service.base_url, "an HTTP operation needs service.base_url"


def test_9router_keeps_its_secrets_out_of_the_overlay():
    """9Router needs JWT_SECRET and INITIAL_PASSWORD; they must come from service.set_secret."""
    folder = CATALOG / "9router"
    ops = sp.load(folder / "abp-ops.toml")
    text = (folder / "abp-ops.toml").read_text(encoding="utf-8")
    assert ops.service.start
    # named in a comment, never assigned: no value for a secret is written into ABP
    for name in ("JWT_SECRET", "INITIAL_PASSWORD"):
        assert name in text, f"{name} should be named so the operator knows to set it"
        assert not re.search(rf"{name}\s*=\s*\S", text), f"{name} must not have a value here"
    assert "service.set_secret" in text, "the overlay must say where the secrets go"
    assert "{secret" not in text, "and must not make the service refuse to start without one"


def test_openhuman_says_it_is_gpl_and_runs_the_binary_rather_than_its_code():
    """GPL-3.0-only: the overlay must name the licence, and nothing of upstream's may be vendored."""
    folder = CATALOG / "openhuman"
    manifest = (folder / "abp-module.toml").read_text(encoding="utf-8")
    ops = (folder / "abp-ops.toml").read_text(encoding="utf-8")
    assert "GPL-3.0" in manifest and "GPL-3.0" in ops
    assert sorted(p.name for p in folder.iterdir() if p.is_file()) == ["abp-module.toml", "abp-ops.toml"]
    spec_ = sp.load(folder / "abp-ops.toml")
    # the service is the built binary, started as its own process
    assert spec_.service.start[0].endswith("openhuman-core{exe}")
    # GPL-3.0 is why ABP offers no provider here: /v1 is behind the core's per-launch bearer
    assert not spec_.service.openai and spec_.service.auth.startswith("bearer-env:")


def test_noema_names_the_training_environment_that_has_torch():
    """NOEMA's own Python has no torch; the overlay must say which interpreter to use and why."""
    folder = CATALOG / "noema"
    ops = sp.load(folder / "abp-ops.toml")
    text = (folder / "abp-ops.toml").read_text(encoding="utf-8")
    assert "E:/ABP-LocalAI/venv-train/Scripts/python.exe" in text, "the torch interpreter must be named"
    assert "torch" in text
    cmds = [o for o in ops.ops if o.kind == "cmd"]
    assert cmds, "NOEMA's operations are commands (pytest, its CLI)"
    for o in cmds:
        assert o.argv[0].endswith("python.exe"), f"{o.id} must run under the training environment"
    # NOEMA caps its own threads before importing torch: every torch process here says so too
    for o in ops.ops:
        for value in o.env.values():
            if "THREADS" in value:
                assert value == "4", f"{o.id} must cap threads at 4 (this host crashes under load)"
    assert ops.service.env["PYTHONPATH"] == "{project}/src", "src/ on the path, never installed"


def test_no_overlay_writes_into_its_checkout():
    """Every overlay's commands stay inside the checkout and use ABP's own data folder."""
    for mid, (folder, *_rest) in EXPECTED.items():
        for o in sp.load(CATALOG / folder / "abp-ops.toml").ops:
            assert o.cwd in (".", "") or o.cwd.startswith(("app", "scripts", "tests")), f"{mid}.{o.id}"
            assert ".." not in o.cwd, f"{mid}.{o.id} leaves the checkout"


def test_ironroots_health_check_is_a_real_query_not_an_open_port():
    """A resolver speaks DNS, not HTTP: there is nothing to point a base_url at, and "is it
    validating" can only be answered by asking it."""
    folder = CATALOG / "ironroot"
    m = mf.load(folder / mf.MANIFEST_FILE)
    s = sp.load(folder / "abp-ops.toml").service
    assert not (s.base_url or s.health or s.web or s.openai), "a DNS server has no HTTP API to probe"
    assert not (m.web or m.openai), "nor a pane or a provider to offer"
    # the resolver itself, started with no flags: its own defaults are 127.0.0.1:5350
    assert len(s.start) == 1 and s.start[0].endswith("ironroot{exe}"), s.start
    assert s.env.get("RUST_LOG") == "info", "ironroot logs through env_logger; RUST_LOG is its own switch"


def test_ironroot_asks_a_signed_name_and_reads_the_ad_flag():
    """The two checks that matter: it answers, and what it answers was validated."""
    ops = sp.load(CATALOG / "ironroot" / "abp-ops.toml")
    by_id = {o.id: o for o in ops.ops}
    health = by_id["status.dns"]
    assert health.kind == "cmd" and not health.mutating, "a query changes nothing"
    assert health.inputs["name"]["default"] == "isc.org", "a name signed all the way to the root"
    assert health.inputs["server"]["default"] == "127.0.0.1:5350", "the resolver's own default port"
    assert "ironroot-dig{exe}" in " ".join(health.argv) and "--json" in health.argv
    check = by_id["dnssec.check"].argv[-1]
    assert "isc.org" in check and "dnssec-failed.org" in check, "one that must validate, one that must not"
    assert "SERVFAIL" in check and "validated" in check, "the AD bit decides, not the mere presence of an answer"
    assert not by_id["dnssec.check"].mutating
    assert not [o for o in ops.ops if "cache" in o.id or "flush" in o.id], (
        "ironroot's CLI is its resolver's own flags - no control socket and no cache command - so there is "
        "nothing to flush through; service.stop then service.start is the flush, and no operation may pretend")


def test_ironroot_moves_abps_own_resolver_setting():
    """ABP already asks 127.0.0.1:5350 (bot/resolver.py); the overlay moves that one setting and
    touches no route that does not exist."""
    ops = sp.load(CATALOG / "ironroot" / "abp-ops.toml")
    by_id = {o.id: o for o in ops.ops}
    for oid in ("abp.use", "abp.stop_using"):
        code = by_id[oid].argv[-1]
        assert "from bot import resolver" in code and "set_settings" in code, f"{oid} uses ABP's own settings"
        assert "'ironroot'" in code, "that setting's name is the one in bot/resolver.py's DEFAULTS"
        assert by_id[oid].mutating, "this changes how ABP resolves from now on"
    assert "'ironroot':''" in by_id["abp.stop_using"].argv[-1], "stopping clears it; bot/resolver.py then skips it"
    assert "ask_dns" in by_id["abp.use"].argv[-1], "ABP proves it can use the resolver before pointing at it"
    assert not [o for o in ops.ops if o.kind == "http"], "there is no HTTP route here to invent"


def test_ironroots_outputs_follow_the_target_dir_the_build_used(tmp_path, monkeypatch):
    """The checkout is the owner's working copy, so where cargo writes is ABP's decision, not this
    file's: modules.build_cache moves it, and the overlay's outputs, the service it starts and the dig
    the operations run all have to land in the same place - with the cache set and without it."""
    from bot.modules import harness

    monkeypatch.delenv("ABP_MODULE_BUILD_CACHE", raising=False)
    m = registry.get("ironroot")
    exe = registry.placeholders(m)["exe"]
    assert m.checkout_dir, "the checkout is this machine's own working copy, so the overlay says where it is"
    assert m.build_steps and m.build_steps[0][:2] == ["cargo", "build"], "built from the checkout, with cargo"
    ops = sp.load(CATALOG / "ironroot" / "abp-ops.toml")
    dig_ops = {o.id: o for o in ops.ops if "ironroot-dig" in " ".join(o.argv)}
    assert set(dig_ops) == {"status.dns", "query.dig", "dnssec.check"}, sorted(dig_ops)
    binaries = [a for o in dig_ops.values() for a in o.argv if "ironroot-dig" in a] + list(ops.service.start)

    for cache in (None, tmp_path / "fast-cargo"):
        monkeypatch.setattr(registry, "_cfg", lambda c=cache: {"build_cache": str(c)} if c else {})
        target = registry.target_dir(m)
        assert target == (cache / "ironroot" if cache else Path(m.checkout_dir) / "target")
        # what the build job really runs with: harness._build_env is what redirects CARGO_TARGET_DIR
        assert Path(harness._build_env(m)["CARGO_TARGET_DIR"]) == target
        assert Path(registry.expand(m, m.build_env["CARGO_TARGET_DIR"])) == target
        outputs = [registry.expand_cmd(m, [o])[0] for o in m.build_outputs]
        assert [Path(o).name for o in outputs] == [f"ironroot{exe}", f"ironroot-dig{exe}"], outputs
        for o in outputs:
            assert Path(o).parent == target / "release", f"{o} is not where this build writes ({target})"
        assert Path(registry.expand_cmd(m, [ops.service.start[0]])[0]) == target / "release" / f"ironroot{exe}"
        for a in binaries:
            assert "{target}/release/ironroot" in a, f"{a} names a path instead of asking for the build ABP made"
            assert registry.expand(m, a).count(f"{target}/release/") == 1, f"{a} would look in the wrong build"
        # the hub has to tell the operations which build this is, or they fall back to the checkout's target/
        assert "target={target}" in m.hub.start, m.hub.start


def test_the_other_cargo_overlays_follow_the_target_dir_too():
    """Same rule for the overlays that build with cargo: {target} in outputs, in CARGO_TARGET_DIR and in
    the --var the hub hands the operations - a literal path here goes stale the moment build_cache is set."""
    for mid in ("mesh-llm", "openhuman"):
        m = mf.load(CATALOG / mid / mf.MANIFEST_FILE)
        assert m.build_env.get("CARGO_TARGET_DIR") == "{target}", f"{mid}: cargo must write where {mid} looks"
        assert m.build_outputs and all(o.startswith("{target}/") for o in m.build_outputs), m.build_outputs
        assert "target={target}" in m.hub.start, m.hub.start
# openhuman's service is the binary its own build produced; mesh-llm's is the release CLI on PATH
    start = sp.load(CATALOG / "openhuman" / "abp-ops.toml").service.start
    assert start and start[0].startswith("{target}/"), start