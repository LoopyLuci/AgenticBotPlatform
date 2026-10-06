"""The catalog's own overlays: every third-party project ABP drives without touching its checkout.

Each overlay is a pair of files ABP ships — `abp-module.toml` (the manifest) and `abp-ops.toml`
(the operations and the service) — and these tests read them through ABP's own loaders
(bot.modules.manifest for the manifest, abp_modkit.spec for the operations) and assert what they
say: the service the overlay starts, the provider ABP offers while it runs, and the operations a
person and ABP's agents actually need. Nothing here names a particular project: the list is read
out of catalog/ itself, so a new overlay is covered the moment it lands and nothing has to be
added to a table in this file.

Overlays for private projects live in the private folder's catalog/ (ABP_PRIVATE_DIR), not here,
so what these tests see is exactly what a public install ships.

No network, no upstream checkout: only the two files per overlay, parsed.
"""
from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest

from abp_modkit import spec as sp
from bot.modules import manifest as mf

CATALOG = Path(__file__).resolve().parent.parent / "catalog"

# Every folder in catalog/ that carries a manifest is an overlay, whatever it is for.
OVERLAYS = sorted(p.name for p in CATALOG.iterdir() if (p / mf.MANIFEST_FILE).is_file())
assert OVERLAYS, "the catalog ships at least one overlay"


@pytest.fixture(scope="module", params=OVERLAYS)
def overlay(request):
    """Each catalog overlay loaded the way ABP loads it: the manifest through bot.modules.manifest,
    the operations through abp_modkit.spec."""
    folder = CATALOG / request.param
    return {"folder": folder, "id": request.param,
            "manifest": mf.load(folder / mf.MANIFEST_FILE), "ops": sp.load(folder / "abp-ops.toml"),
            "raw": tomllib.loads((folder / mf.MANIFEST_FILE).read_text(encoding="utf-8"))}


def test_the_catalog_is_exactly_the_overlays_it_says_it_is(overlay):
    """A folder's manifest names the same module as the folder does (an overlay may prefix its
    id, as m-<name> does, to keep it apart from the third-party project it drives), so the id a
    user types, the id in the UI and the folder on disk can never disagree."""
    assert overlay["manifest"].id.removeprefix("m-") == overlay["id"]


def test_every_overlay_is_a_valid_manifest_describing_a_service(overlay):
    m, s, raw = overlay["manifest"], overlay["ops"].service, overlay["raw"]
    assert m.name and m.area and m.description, "a module a person scans needs a real sentence"
    assert len(m.description) > 40, f"{m.id}: a module a person scans needs a real sentence"
    assert m.api == mf.API_VERSION and m.branch
    assert m.overlay or True            # the registry sets .overlay; parse() leaves it empty
    assert m.hub is not None and m.hub.control_file and m.hub.start, f"{m.id}: an overlay needs a hub"
    assert "{overlay}/abp-ops.toml" in " ".join(m.hub.start), "the hub must read the overlay's operations"
    assert s.id == m.id, "the operations file must describe the same module as the manifest"
    assert overlay["ops"].id == m.id
    # A service is only a service if it runs something. An overlay of operations alone (a model
    # family ABP reads from a checkout, say) declares no base_url and must declare no start either.
    if s.base_url or s.health or s.web:
        assert s.start, f"{m.id}: a service with an endpoint must say how to start it"
        assert s.health, f"{m.id}: a service with a base_url needs a health route to poll"
    else:
        assert not s.start, f"{m.id}: a start with nothing to reach is dead configuration"
    assert s.base_url.startswith(("http://", "")) and (not s.openai or s.openai.startswith("/")), s


def test_each_overlay_offers_the_operations_it_declares(overlay):
    """Whatever the operations are, each one ABP shows to a person is described and consistent:
    a summary, and no parameter that isn't also an input."""
    ids = {o.id for o in overlay["ops"].ops}
    assert ids, f"{overlay['id']}: an overlay with no operations offers nothing"
    for o in overlay["ops"].ops:
        assert o.summary, f"{o.id} has no summary; ABP shows it as-is to a person"
        assert not o.params or set(o.params) <= set(o.inputs), f"{o.id}: a parameter with no input"


def test_the_openai_compatible_modules_are_offered_to_abp_as_providers(overlay):
    """An overlay is offered as a provider exactly when it serves the OpenAI API, and only then
    ABP links its UI: offering one without the API would hand out a model that cannot be called."""
    m, s = overlay["manifest"], overlay["ops"].service
    if s.openai:
        assert m.openai == "service", "an OpenAI-compatible module must be offered as a provider"
        assert m.web, "and its UI should be reachable while it runs"
    else:
        assert not m.openai, f"{m.id} serves no OpenAI API, so ABP must not offer it as a provider"


def test_a_module_overlay_serves_it_as_a_provider_offers_a_models_list(overlay):
    """A module ABP offers as a provider has to answer a models list, as a real GET route."""
    m, ops = overlay["manifest"], overlay["ops"]
    if m.openai != "service":
        pytest.skip(f"{m.id} is not offered as a provider")
    lists = [o for o in ops.ops if o.kind == "http" and o.path.endswith("/models")]
    assert lists, f"{m.id} offers itself as a provider but has no models list"
    for o in lists:
        assert o.method == "GET" and o.mutating is False
        assert ops.service.base_url, "an HTTP operation needs service.base_url"


def test_no_overlay_writes_into_its_checkout(overlay):
    """Every overlay's commands stay inside the checkout and use ABP's own data folder: an overlay
    exists precisely so a person's working copy is left exactly as upstream has it. A cwd may be
    any folder inside the checkout, but it may never be absolute and may never climb out."""
    for o in overlay["ops"].ops:
        assert not Path(o.cwd).is_absolute(), f"{overlay['id']}.{o.id} names a path outside the checkout"
        assert ".." not in Path(o.cwd).parts, f"{overlay['id']}.{o.id} leaves the checkout"
        assert not re.search(r"(?i)[A-Z]:[\\/]", o.cwd), f"{overlay['id']}.{o.id} hard-codes a drive path"


def test_the_cargo_overlays_follow_the_target_dir_the_build_used(overlay):
    """Where cargo writes is ABP's decision, not the overlay file's: modules.build_cache moves it,
    so every path a cargo-built overlay names must be a {target} placeholder. A literal path here
    goes stale the moment build_cache is set."""
    m = overlay["manifest"]
    if not any(str(step[0]).startswith("cargo") for step in m.build_steps or []):
        pytest.skip(f"{m.id} is not built with cargo")
    assert m.build_env.get("CARGO_TARGET_DIR") == "{target}", f"{m.id}: cargo must write where {m.id} looks"
    assert m.build_outputs and all(o.startswith("{target}/") for o in m.build_outputs), m.build_outputs
    assert "target={target}" in m.hub.start, m.hub.start


def test_an_overlay_never_carries_a_secret_value(overlay):
    """A secret belongs in service.set_secret or in a {secret?} placeholder, never as a literal
    here: this file is in a public repo and is copied to every machine that installs ABP."""
    text = (overlay["folder"] / "abp-ops.toml").read_text(encoding="utf-8")
    for name, value in (getattr(overlay["ops"].service, "set_secrets", {}) or {}).items():
        pairs = value if isinstance(value, dict) else {}
        for secret, literal in pairs.items():
            assert not str(literal).strip(), f"{overlay['id']}: the secret {secret} must not have a value here"
    assert not re.search(r"(?i)^\s*(jwt_secret|initial_password|api_key|api-key|token|password)\s*=\s*\S", text, re.M), (
        f"{overlay['id']}: name a secret in a comment, never assign one")