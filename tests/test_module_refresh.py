"""MH-C: refreshing a module after upstream changed. Upstream is a local bare repo; nothing touches the network."""
from __future__ import annotations

import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

from bot.modules import registry
from bot.modules.client import ModuleError

GIT = ["git", "-c", "user.email=t@example.invalid", "-c", "user.name=t", "-c", "commit.gpgsign=false"]


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run([*GIT, "-C", str(cwd), *args], capture_output=True, text=True, check=True).stdout.strip()


def _w(p: Path, text: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(textwrap.dedent(text).lstrip("\n"), encoding="utf-8", newline="\n")


CLI = '''
    import argparse
    if __name__ == "__main__":
        ap = argparse.ArgumentParser()
        ap.add_argument("--n", default="3")
        print(ap.parse_args().n)
    '''


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.delenv("ABP_PRIVATE_DIR", raising=False)


@pytest.fixture
def world(tmp_path, monkeypatch):
    """A bare upstream with two scripts, a clone of it adopted as an overlay module 'fresh', and a second clone
    (the 'maintainer') used to push upstream changes."""
    bare = tmp_path / "up.git"
    _git(tmp_path, "init", "-q", "--bare", "-b", "main", str(bare))
    maint = tmp_path / "maint"
    _git(tmp_path, "clone", "-q", str(bare), str(maint))
    _w(maint / "alpha.py", CLI)
    _w(maint / "beta.py", CLI)
    _w(maint / "README.md", "# Fresh\n\nA small tool.\n")
    _git(maint, "add", "-A")
    _git(maint, "commit", "-q", "-m", "first")
    _git(maint, "push", "-q", "origin", "HEAD:main")
    clone = tmp_path / "clones" / "fresh"
    _git(tmp_path, "clone", "-q", str(bare), str(clone))
    overlays = tmp_path / "overlays"
    monkeypatch.setattr(registry, "user_overlay_root", lambda: overlays)
    monkeypatch.setattr(registry, "data_dir", lambda m: tmp_path / "data" / m.id)
    from abp_modkit import adopt as ad
    ad.adopt(clone, mid="fresh", name="Fresh", repo=str(bare), overlay=overlays / "fresh", record_checkout=True)
    monkeypatch.setattr(registry, "overlay_roots", lambda: [overlays])
    registry.modules(refresh=True)

    class W:
        pass
    w = W()
    w.bare, w.maint, w.clone, w.overlay, w.tmp = bare, maint, clone, overlays / "fresh", tmp_path
    w.push = lambda msg: (_git(maint, "add", "-A"), _git(maint, "commit", "-q", "-m", msg),
                          _git(maint, "push", "-q", "origin", "HEAD:main"))
    yield w
    monkeypatch.undo()
    registry.modules(refresh=True)


def ids(rows):
    return sorted(r["id"] for r in rows)


def test_diff_shows_added_removed_and_changed_operations_and_dry_run_writes_nothing(world):
    from bot.modules import refresh
    base = world.overlay.joinpath("abp-ops.toml").read_text(encoding="utf-8")
    assert "script.alpha" in base and "script.beta" in base
    _w(world.maint / "gamma.py", CLI)                                     # added
    (world.maint / "beta.py").unlink()                                    # removed
    _w(world.maint / "alpha.py", '"""Now it has a different description."""\n' + textwrap.dedent(CLI).lstrip("\n"))
    world.push("upstream moves")
    head = _git(world.clone, "rev-parse", "HEAD")
    r = refresh.refresh("fresh")
    assert r["dry_run"] and not r["applied"] and r["new_commits"] == 1 and r["changed"]
    assert ids(r["operations"]["added"]) == ["script.gamma"]
    assert ids(r["operations"]["removed"]) == ["script.beta"]
    assert ids(r["operations"]["changed"]) == ["script.alpha"]
    assert "description" in r["operations"]["changed"][0]["groups"]
    # a dry run wrote nothing: not the overlay, not the checkout
    assert world.overlay.joinpath("abp-ops.toml").read_text(encoding="utf-8") == base
    assert _git(world.clone, "rev-parse", "HEAD") == head and _git(world.clone, "status", "--porcelain") == ""
    assert not (world.clone / "gamma.py").exists()
    # apply writes it
    a = refresh.refresh("fresh", apply=True)
    assert a["applied"] and "abp-ops.toml" in a["wrote"]
    assert (world.clone / "gamma.py").is_file() and _git(world.clone, "rev-parse", "HEAD") != head
    ops = world.overlay.joinpath("abp-ops.toml").read_text(encoding="utf-8")
    assert "script.gamma" in ops and "script.beta" not in ops
    assert _git(world.clone, "status", "--porcelain") == ""
    again = refresh.refresh("fresh")                                      # now up to date
    assert not again["changed"] and again["new_commits"] == 0


def test_a_command_change_is_reported_as_a_command_and_a_signature_change(world):
    from bot.modules import refresh
    ops = world.overlay / "abp-ops.toml"
    text = ops.read_text(encoding="utf-8")
    # pretend what ABP wrote earlier had another command for alpha: detection now gives the real one
    lines = text.splitlines()
    i = next(n for n, ln in enumerate(lines) if ln.startswith("argv") and "alpha.py" in ln)
    lines[i] = lines[i].replace("alpha.py", "old_alpha.py")
    ops.write_text("\n".join(lines) + "\n", encoding="utf-8")
    r = refresh.refresh("fresh")
    ch = [c for c in r["operations"]["changed"] if c["id"] == "script.alpha"]
    assert ch and "command" in ch[0]["groups"] and "argv" in ch[0]["fields"]
    assert ch[0]["fields"]["argv"]["from"] != ch[0]["fields"]["argv"]["to"]


def test_hand_edits_survive_an_apply(world):
    from abp_modkit import spec as sp
    from bot.modules import refresh
    ops = world.overlay / "abp-ops.toml"
    spec = sp.load(ops)
    for o in spec.ops:
        if o.id == "script.alpha":
            o.summary = "Alpha, described by a person"
    spec.ops.append(sp.Op(id="mine.hello", kind="cmd", argv=["echo", "hi"], summary="My own operation"))
    ops.write_text(sp.dump(spec, header="# written by abp_modkit (abp_modkit)"), encoding="utf-8")
    _w(world.maint / "gamma.py", CLI)
    world.push("add gamma")
    r = refresh.refresh("fresh", apply=True)
    assert ids(r["operations"]["added"]) == ["script.gamma"] and not r["operations"]["removed"]
    after = sp.load(ops)
    by = {o.id: o for o in after.ops}
    assert by["script.alpha"].summary == "Alpha, described by a person"
    assert "mine.hello" in by and "script.gamma" in by


def test_a_hand_owned_ops_file_is_kept_as_it_is(world):
    from bot.modules import refresh
    ops = world.overlay / "abp-ops.toml"
    text = ops.read_text(encoding="utf-8")
    first, _, rest = text.partition("\n")
    ops.write_text("# my own curated list\n" + rest, encoding="utf-8")
    kept = ops.read_text(encoding="utf-8")
    _w(world.maint / "gamma.py", CLI)
    world.push("add gamma")
    r = refresh.refresh("fresh", apply=True)
    assert r["ops_hand_owned"] and ids(r["detected_not_in_ops_file"]) == ["script.gamma"]
    assert ops.read_text(encoding="utf-8") == kept and "abp-ops.toml" not in r["wrote"]


def test_a_dirty_checkout_is_refused_untouched(world):
    from bot.modules import refresh
    _w(world.maint / "gamma.py", CLI)
    world.push("add gamma")
    (world.clone / "alpha.py").write_text("# local edit\n", encoding="utf-8")
    ops_before = (world.overlay / "abp-ops.toml").read_text(encoding="utf-8")
    head = _git(world.clone, "rev-parse", "HEAD")
    for apply in (False, True):
        with pytest.raises(ModuleError) as e:
            refresh.refresh("fresh", apply=apply)
        assert e.value.code == "dirty" and "alpha.py" in str(e.value)
    assert (world.clone / "alpha.py").read_text(encoding="utf-8") == "# local edit\n"
    assert _git(world.clone, "rev-parse", "HEAD") == head
    assert (world.overlay / "abp-ops.toml").read_text(encoding="utf-8") == ops_before


def test_a_diverged_checkout_is_refused_untouched(world):
    from bot.modules import refresh
    _w(world.clone / "local.py", CLI)
    _git(world.clone, "add", "-A")
    _git(world.clone, "commit", "-q", "-m", "local commit")
    _w(world.maint / "gamma.py", CLI)
    world.push("add gamma")
    head = _git(world.clone, "rev-parse", "HEAD")
    ops_before = (world.overlay / "abp-ops.toml").read_text(encoding="utf-8")
    with pytest.raises(ModuleError) as e:
        refresh.refresh("fresh", apply=True)
    assert e.value.code == "diverged"
    assert _git(world.clone, "rev-parse", "HEAD") == head and (world.clone / "local.py").is_file()
    assert not (world.clone / "gamma.py").exists()
    assert (world.overlay / "abp-ops.toml").read_text(encoding="utf-8") == ops_before


def test_local_commits_only_are_not_a_divergence(world):
    from bot.modules import refresh
    _w(world.clone / "local.py", CLI)
    _git(world.clone, "add", "-A")
    _git(world.clone, "commit", "-q", "-m", "local commit")
    r = refresh.refresh("fresh", apply=True)
    assert r["applied"] and r["new_commits"] == 0 and (world.clone / "local.py").is_file()


def test_an_adopted_project_keeps_its_hand_edited_module_files(world, tmp_path):
    """Module files live in the checkout: a hand edit to them does not count as dirty, and survives."""
    from abp_modkit import adopt as ad
    from bot.config import config
    from bot.modules import refresh
    inplace = tmp_path / "inplace"
    _git(tmp_path, "clone", "-q", str(world.bare), str(inplace))
    ad.adopt(inplace, mid="inplace", name="Inplace", repo=str(world.bare))
    _git(inplace, "add", "-A")
    _git(inplace, "commit", "-q", "-m", "module files")
    _git(inplace, "push", "-q", "origin", "HEAD:refs/heads/inplace")
    _git(inplace, "branch", "-u", "origin/inplace")
    cfg_file = tmp_path / "backends.yaml"
    shutil.copy(config.path, cfg_file)
    config.path, old = cfg_file, config.path
    try:
        config.reload(actor="test")
        from bot.modules import adoption
        adoption.register(inplace)
        registry.modules(refresh=True)
        ops = inplace / "abp-ops.toml"
        ops.write_text(ops.read_text(encoding="utf-8").replace('summary = "', 'summary = "HAND ', 1), encoding="utf-8")
        hand = ops.read_text(encoding="utf-8")
        _git(world.maint, "fetch", "-q", "origin", "inplace")
        _git(world.maint, "checkout", "-q", "-B", "inplace", "origin/inplace")
        _w(world.maint / "delta.py", CLI)
        _git(world.maint, "add", "-A")
        _git(world.maint, "commit", "-q", "-m", "delta")
        _git(world.maint, "push", "-q", "origin", "inplace")
        r = refresh.refresh("inplace", apply=True)
        assert r["applied"] and ids(r["operations"]["added"]) == ["script.delta"]
        text = ops.read_text(encoding="utf-8")
        assert "HAND " in text and "script.delta" in text
        assert hand.count("HAND ") == text.count("HAND ")
    finally:
        config.path = old
        config.reload(actor="test")
        registry.modules(refresh=True)


def test_builtin_and_catalog_modules_are_refused():
    from bot.modules import refresh
    with pytest.raises(ModuleError) as e:
        refresh.refresh(next(iter(registry.modules())))
    assert e.value.code in ("unsupported", "not_installed")


def test_api_and_tool_are_wired():
    from fastapi import FastAPI

    from bot.dashboard import modules_api
    app = FastAPI()
    modules_api.register(app, lambda: None, lambda: None)
    routes = {(m, r.path) for r in app.routes for m in getattr(r, "methods", ())}
    assert ("GET", "/api/modules/{mid}/refresh") in routes and ("POST", "/api/modules/{mid}/refresh") in routes
    from bot.agent_runtime import toolspec
    from bot.modules import tools
    tools.register_tools()
    assert "module_refresh" in toolspec._registered


def test_an_edited_operation_whose_script_vanished_is_kept_and_reported(world):
    from abp_modkit import spec as sp
    from bot.modules import refresh
    ops = world.overlay / "abp-ops.toml"
    spec = sp.load(ops)
    for o in spec.ops:
        if o.id == "script.beta":
            o.summary = "Beta, which I documented"
    ops.write_text(sp.dump(spec), encoding="utf-8")
    (world.maint / "beta.py").unlink()
    (world.maint / "alpha.py").unlink()
    world.push("drop scripts")
    r = refresh.refresh("fresh", apply=True)
    assert r["kept_stale"] == ["script.beta"] and ids(r["operations"]["removed"]) == ["script.alpha"]
    kept = {o.id: o for o in sp.load(ops).ops}
    assert "script.beta" in kept and "script.alpha" not in kept
    assert kept["script.beta"].summary == "Beta, which I documented"
