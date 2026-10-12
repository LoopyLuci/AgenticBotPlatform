"""MH-E: sharing overlays through a catalog repo. The catalog and the upstream are local bare repos; nothing touches the
network, and "machines" are separate ABP data folders (monkeypatched registry roots)."""
from __future__ import annotations

import subprocess
import textwrap
import time
from pathlib import Path

import pytest

from bot.modules import catalog, harness, registry
from bot.modules.client import ModuleError

GIT = ["git", "-c", "user.email=t@example.invalid", "-c", "user.name=t", "-c", "commit.gpgsign=false"]
CLI = '''
    import argparse
    if __name__ == "__main__":
        ap = argparse.ArgumentParser()
        ap.add_argument("--n", default="3")
        print(ap.parse_args().n)
    '''


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run([*GIT, "-C", str(cwd), *args], capture_output=True, text=True, check=True).stdout.strip()


def _w(p: Path, text: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(textwrap.dedent(text).lstrip("\n"), encoding="utf-8", newline="\n")


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.delenv("ABP_PRIVATE_DIR", raising=False)
    monkeypatch.setattr(catalog, "_ALLOW_LOCAL", True)


class World:
    """An upstream (two commits), a catalog (empty bare), and machines A and B."""

    def __init__(self, tmp: Path, mp):
        self.tmp, self.mp = tmp, mp
        self.up = tmp / "up.git"
        _git(tmp, "init", "-q", "--bare", "-b", "main", str(self.up))
        m = tmp / "maint"
        _git(tmp, "clone", "-q", str(self.up), str(m))
        _w(m / "alpha.py", CLI)
        _w(m / "README.md", "# Fresh\n\nA small tool.\n")
        _git(m, "add", "-A")
        _git(m, "commit", "-q", "-m", "first")
        self.first = _git(m, "rev-parse", "HEAD")
        _w(m / "beta.py", CLI)
        _git(m, "add", "-A")
        _git(m, "commit", "-q", "-m", "second")
        _git(m, "push", "-q", "origin", "HEAD:main")
        self.cat = tmp / "catalog.git"
        _git(tmp, "init", "-q", "--bare", "-b", "main", str(self.cat))

    def machine(self, name: str, catalogs: bool = True) -> Path:
        base = self.tmp / name
        (base / "abp").mkdir(parents=True, exist_ok=True)
        ov = base / "abp" / "data" / "module-overlays"
        self.mp.setattr(registry, "abp_root", lambda: base / "abp")
        self.mp.setattr(registry, "user_overlay_root", lambda: ov)
        self.mp.setattr(registry, "overlay_roots", lambda: [ov])
        cfg = {"catalogs": [{"name": "mine", "url": str(self.cat), "branch": "main"}]} if catalogs else {}
        self.mp.setattr(registry, "_cfg", lambda: cfg)
        registry.modules(refresh=True)
        return base

    def adopt_fresh(self, extra_ops: str = "") -> Path:
        """On the current machine: clone upstream and adopt it as an overlay module 'fresh' (its repo an https URL)."""
        clone = self.tmp / "clones" / "fresh"
        if not clone.exists():
            _git(self.tmp, "clone", "-q", str(self.up), str(clone))
        from abp_modkit import adopt as ad
        ov = registry.user_overlay_root() / "fresh"
        ad.adopt(clone, mid="fresh", name="Fresh", repo="https://github.com/example/fresh.git", overlay=ov,
                 record_checkout=True)
        if extra_ops:
            with open(ov / "abp-ops.toml", "a", encoding="utf-8") as f:
                f.write(extra_ops)
        registry.modules(refresh=True)
        return ov

    def set_upstream(self, entry: str = "fresh", ref: str = "") -> None:
        """What a maintainer does in the catalog: point entry.toml at the real (here local) upstream."""
        w = self.tmp / "cat-maint"
        if not w.exists():
            _git(self.tmp, "clone", "-q", str(self.cat), str(w))
        _git(w, "pull", "-q", "origin", "main")
        f = w / entry / "entry.toml"
        lines = [ln for ln in f.read_text(encoding="utf-8").splitlines() if not ln.startswith(("upstream", "ref"))]
        lines.insert(2, f'upstream = "{self.up.as_posix()}"')
        if ref:
            lines.insert(3, f'ref = "{ref}"')
        f.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
        _git(w, "add", "-A")
        _git(w, "commit", "-q", "-m", "upstream")
        _git(w, "push", "-q", "origin", "HEAD:main")


@pytest.fixture
def world(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch)
    yield w
    monkeypatch.undo()
    registry.modules(refresh=True)


def wait(job: dict) -> dict:
    for _ in range(600):
        j = harness.job(job["id"])
        if j["state"] != "running":
            return j
        time.sleep(0.2)
    raise AssertionError("job did not finish")


def publish_to_main(world, **kw) -> dict:
    """Publish from machine A and merge the prepared branch into the catalog's main (a person's review + merge)."""
    r = catalog.publish("fresh", "mine", push=True, **kw)
    w = world.tmp / "merge"
    if not w.exists():
        _git(world.tmp, "clone", "-q", str(world.cat), str(w))
    _git(w, "fetch", "-q", "origin", r["branch"])
    _git(w, "checkout", "-q", "-B", "main", "FETCH_HEAD")
    _git(w, "push", "-q", "-f", "origin", "main")
    return r


def test_list_shows_entries_of_a_synced_catalog(world):
    world.machine("a")
    world.adopt_fresh()
    publish_to_main(world)
    world.machine("b")
    assert catalog.entries()["entries"] == []                      # nothing synced yet
    res = catalog.sync()
    assert res[0]["entries"] == 1 and res[0]["commit"]
    rows = catalog.entries()["entries"]
    assert [(r["catalog"], r["name"], r["module_id"], r["installed"]) for r in rows] == [("mine", "fresh", "fresh", False)]
    assert rows[0]["files"] == ["abp-module.toml", "abp-ops.toml"]
    import json
    idx = json.loads((catalog.root() / "mine" / "index.json").read_text(encoding="utf-8"))
    assert [e["name"] for e in idx["entries"]] == ["fresh"]


def test_sync_of_an_empty_catalog_is_not_an_error(world):
    world.machine("a")
    res = catalog.sync()
    assert res[0]["entries"] == 0 and "no branch" in res[0]["note"]


def test_install_end_to_end_on_another_machine(world):
    world.machine("a")
    world.adopt_fresh()
    publish_to_main(world)
    world.set_upstream()
    b = world.machine("b")
    catalog.sync()
    j = wait(catalog.install("mine", "fresh"))
    assert j["state"] == "done", j.get("error")
    r = j["result"]
    assert r["checked"], r["checks"]
    clone = b / "abp" / "data" / "modules" / "Fresh"
    assert Path(r["path"]) == clone and (clone / "alpha.py").is_file() and (clone / "beta.py").is_file()
    assert not (clone / "abp-module.toml").exists()                 # an overlay: nothing written into the clone
    ov = b / "abp" / "data" / "module-overlays" / "fresh"
    assert (ov / "abp-ops.toml").is_file() and not (ov / "entry.toml").exists()
    assert "dir =" not in (ov / "abp-module.toml").read_text(encoding="utf-8")
    m = registry.get("fresh")
    assert registry.install_dir(m) == clone and m.overlay == str(ov)
    assert catalog.entries()["entries"][0]["installed"] is True


def test_install_refuses_a_module_that_already_exists(world):
    world.machine("a")
    world.adopt_fresh()
    publish_to_main(world)
    world.set_upstream()
    world.machine("b")
    catalog.sync()
    assert wait(catalog.install("mine", "fresh"))["state"] == "done"
    with pytest.raises(ModuleError) as e:
        catalog.install("mine", "fresh")
    assert e.value.code == "exists"
    # also when only the checkout folder is in the way
    b = world.machine("c")
    catalog.sync()
    (b / "abp" / "data" / "modules" / "Fresh").mkdir(parents=True)
    (b / "abp" / "data" / "modules" / "Fresh" / "mine.txt").write_text("x", encoding="utf-8")
    with pytest.raises(ModuleError) as e:
        catalog.install("mine", "fresh")
    assert e.value.code == "exists"
    assert (b / "abp" / "data" / "modules" / "Fresh" / "mine.txt").read_text(encoding="utf-8") == "x"


def test_install_respects_a_pinned_ref(world):
    world.machine("a")
    world.adopt_fresh()
    publish_to_main(world, ref=world.first)
    world.set_upstream(ref=world.first)
    b = world.machine("b")
    catalog.sync()
    assert catalog.entries()["entries"][0]["ref"] == world.first
    j = wait(catalog.install("mine", "fresh"))
    assert j["state"] == "done", j.get("error")
    clone = b / "abp" / "data" / "modules" / "Fresh"
    assert _git(clone, "rev-parse", "HEAD") == world.first
    assert (clone / "alpha.py").is_file() and not (clone / "beta.py").exists()


def test_install_rolls_back_what_it_made_when_the_clone_fails(world):
    world.machine("a")
    world.adopt_fresh()
    publish_to_main(world, ref="no-such-ref")
    world.set_upstream(ref="no-such-ref")
    b = world.machine("b")
    catalog.sync()
    j = wait(catalog.install("mine", "fresh"))
    assert j["state"] == "failed"
    assert not (b / "abp" / "data" / "modules" / "Fresh").exists()
    assert not (b / "abp" / "data" / "module-overlays" / "fresh").exists()


def test_publish_refuses_a_machine_path(world):
    world.machine("a")
    world.adopt_fresh(extra_ops='\n# notes\n# see ' + 'Z' + ':/Projects/Secret/thing.txt\n')
    with pytest.raises(ModuleError) as e:
        catalog.publish("fresh", "mine")
    assert e.value.code == "leak" and "machine path" in str(e.value)
    assert not (catalog.root() / "mine.work").exists()


def test_publish_refuses_a_secret_without_printing_it(world):
    world.machine("a")
    key = "sk-" + "a1B2c3D4" * 4
    world.adopt_fresh(extra_ops=f'\n# token {key}\n')
    with pytest.raises(ModuleError) as e:
        catalog.publish("fresh", "mine")
    assert e.value.code == "leak" and key not in str(e.value)


def test_publish_makes_a_correct_commit_on_a_new_branch(world):
    world.machine("a")
    ov = world.adopt_fresh()
    (ov / "notes.txt").write_text("extra overlay file\n", encoding="utf-8")
    r = catalog.publish("fresh", "mine")
    assert r["changed"] and r["branch"].startswith("publish/fresh-") and not r["pushed"]
    work = Path(r["work"])
    assert _git(work, "rev-parse", "--abbrev-ref", "HEAD") == r["branch"]
    assert _git(work, "status", "--porcelain") == ""
    tracked = set(_git(work, "ls-tree", "-r", "--name-only", "HEAD").splitlines())
    assert tracked == {"index.json", "fresh/entry.toml", "fresh/abp-module.toml", "fresh/abp-ops.toml", "fresh/notes.txt"}
    manifest = (work / "fresh" / "abp-module.toml").read_text(encoding="utf-8")
    assert "dir =" not in manifest and "id = \"fresh\"" in manifest
    entry = catalog._entry_meta(work / "fresh")
    assert entry["upstream"] == "https://github.com/example/fresh.git" and entry["ref"] == ""
    assert "fresh/entry.toml" in r["diff"]
    # nothing of the checkout went in
    assert "alpha.py" not in tracked and "README.md" not in tracked


def test_publishing_what_the_catalog_already_has_changes_nothing(world):
    world.machine("a")
    world.adopt_fresh()
    publish_to_main(world)
    assert catalog.publish("fresh", "mine")["changed"] is False


def test_nothing_is_pushed_without_push(world):
    world.machine("a")
    world.adopt_fresh()
    r = catalog.publish("fresh", "mine")
    assert _git(world.cat, "for-each-ref", "refs/heads") == ""         # the bare catalog still has no branch at all
    r2 = catalog.publish("fresh", "mine", push=True)
    heads = _git(world.cat, "for-each-ref", "--format=%(refname:short)", "refs/heads").split()
    assert r2["pushed"] and heads == [r2["branch"]] and r["branch"] != r2["branch"]


def test_push_branch_only_pushes_prepared_branches(world):
    world.machine("a")
    world.adopt_fresh()
    r = catalog.publish("fresh", "mine")
    with pytest.raises(ModuleError):
        catalog.push_branch(r["work"], "main")


def test_api_publish_never_pushes_and_catalog_routes_exist(world):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from bot.dashboard import modules_api
    app = FastAPI()
    modules_api.register(app, lambda: None, lambda: None)
    world.machine("a")
    world.adopt_fresh()
    c = TestClient(app)
    r = c.post("/api/modules/catalog/publish", json={"module": "fresh", "catalog": "mine", "push": True})
    assert r.status_code == 200 and r.json()["changed"] and r.json()["pushed"] is False
    assert _git(world.cat, "for-each-ref", "refs/heads") == ""
    assert c.get("/api/modules/catalog").json()["catalogs"][0]["name"] == "mine"
    assert c.post("/api/modules/catalog/install", json={}).status_code == 400
    assert c.post("/api/modules/catalog/publish", json={"module": "nope", "catalog": "mine"}).status_code >= 400


def test_urls_are_checked_when_local_paths_are_not_allowed(monkeypatch):
    monkeypatch.setattr(catalog, "_ALLOW_LOCAL", False)
    for bad in ("http://github.com/a/b.git", "file:///tmp/x", "/tmp/x", "https://u:p@github.com/a/b.git", "ssh://git@github.com/a/b"):
        with pytest.raises(ModuleError):
            catalog._url_ok(bad, upstream=False)
    with pytest.raises(ModuleError):
        catalog._url_ok("https://evil.example/a/b.git", upstream=True)
    assert catalog._url_ok("https://github.com/a/b.git", upstream=True)
    assert catalog._url_ok("https://git.example/a/b.git", upstream=False)


def test_panel_copies_are_identical_and_escape():
    root = Path(__file__).resolve().parents[1]
    a = (root / "bot/dashboard/static/modules-panel.js").read_bytes()
    assert a == (root / "desktop-app/ui/modules-panel.js").read_bytes()
    src = a.decode("utf-8")
    assert "api/modules/catalog" in src and "E(e.description)" in src and "E(e.upstream)" in src
