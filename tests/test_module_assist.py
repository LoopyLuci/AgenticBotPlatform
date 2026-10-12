"""MH-D: assisted setup. The model is faked; nothing touches the network. Proposals are checked strictly, shown as a
difference, and written (as hand edits that a refresh keeps) only on an explicit keep."""
from __future__ import annotations

import asyncio
import json
import subprocess
import textwrap
from pathlib import Path

import pytest

from bot.modules import assist, registry
from bot.modules.client import ModuleError

GIT = ["git", "-c", "user.email=t@example.invalid", "-c", "user.name=t", "-c", "commit.gpgsign=false"]
API_KEY = "sk-abcdefghijklmnopqrstuvwxyz0123"
GH_TOKEN = "ghp_abcdefghijklmnopqrstuvwxyz0123456789"
FILE_SECRET = "hunter2-very-private-value"

SCRIPT = f'''
    import argparse
    API_KEY = "{API_KEY}"
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


@pytest.fixture
def world(tmp_path, monkeypatch):
    from abp_modkit import adopt as ad
    from abp_modkit import spec as sp
    monkeypatch.delenv("ABP_PRIVATE_DIR", raising=False)
    monkeypatch.setenv("ABP_STUDIO_DIR", str(tmp_path / "studio"))
    bare = tmp_path / "up.git"
    _git(tmp_path, "init", "-q", "--bare", "-b", "main", str(bare))
    maint = tmp_path / "maint"
    _git(tmp_path, "clone", "-q", str(bare), str(maint))
    _w(maint / "alpha.py", SCRIPT)
    _w(maint / "beta.py", SCRIPT)
    _w(maint / "secrets.py", f'PASSWORD = "{FILE_SECRET}"\n')
    _w(maint / "README.md", f"# Fresh\n\nA small tool. token = {GH_TOKEN}\n")
    _git(maint, "add", "-A")
    _git(maint, "commit", "-q", "-m", "first")
    _git(maint, "push", "-q", "origin", "HEAD:main")
    clone = tmp_path / "clones" / "fresh"
    _git(tmp_path, "clone", "-q", str(bare), str(clone))
    overlays = tmp_path / "overlays"
    monkeypatch.setattr(registry, "user_overlay_root", lambda: overlays)
    monkeypatch.setattr(registry, "data_dir", lambda m: tmp_path / "data" / m.id)
    ad.adopt(clone, mid="fresh", name="Fresh", repo=str(bare), overlay=overlays / "fresh", record_checkout=True)
    monkeypatch.setattr(registry, "overlay_roots", lambda: [overlays])
    ops_path = overlays / "fresh" / "abp-ops.toml"
    spec = sp.load(ops_path)                       # alpha takes an input (a person typed it in)
    for o in spec.ops:
        if o.id == "script.alpha":
            o.argv = ["{python}", "alpha.py", "--n", "{n}"]
            o.inputs = {"n": {"type": "string", "description": "how many"}}
        if o.id == "script.beta":
            o.argv = ["{python}", "alpha.py", "secrets.py"]       # an op that names a secret-looking file
    ops_path.write_text(sp.dump(spec, header=ops_path.read_text(encoding="utf-8").splitlines()[0]), encoding="utf-8")
    registry.modules(refresh=True)

    class W:
        pass
    w = W()
    w.overlay, w.clone, w.maint, w.tmp, w.ops = overlays / "fresh", clone, maint, tmp_path, ops_path
    w.push = lambda msg: (_git(maint, "add", "-A"), _git(maint, "commit", "-q", "-m", msg),
                          _git(maint, "push", "-q", "origin", "HEAD:main"))
    yield w
    monkeypatch.undo()
    registry.modules(refresh=True)


GOOD = {"description": "A small command-line tool that counts things.",
        "operations": [{"id": "script.alpha", "summary": "Count n things and print the result",
                        "inputs": [{"name": "n", "type": "integer", "required": True, "help": "how many things",
                                    "choices": [1, 2, 3]}]}]}


@pytest.fixture
def fake(monkeypatch):
    """A fake model: answers come from `answers` in call order; the prompts it was sent are kept."""
    from bot.agent_runtime import moa
    from bot.studio import generate as gen

    class F:
        answers: list = []
        prompts: list = []
        calls = 0
    f = F()

    async def cands(explicit=None):
        return [("fake", "m1"), ("fake", "m2")]

    async def call(provider, model, prompt, **kw):
        f.prompts.append(prompt)
        a = f.answers[f.calls % len(f.answers)]
        f.calls += 1
        if isinstance(a, Exception):
            raise a
        return a if isinstance(a, str) else json.dumps({"proposals": [a]})
    monkeypatch.setattr(gen, "candidates", cands)
    monkeypatch.setattr(moa, "_single_call", call)
    return f


def _gen(**kw):
    return asyncio.run(assist.generate("fresh", **kw))


def test_valid_proposals_are_shown_with_a_diff_and_nothing_is_written(world, fake):
    before = (world.ops.read_bytes(), (world.overlay / "abp-module.toml").read_bytes())
    fake.answers = [GOOD, {"description": "Another wording.", "operations": [{"id": "script.beta", "summary": "Beta."}]}]
    b = _gen(n=2)
    assert [p["ok"] for p in b["proposals"]] == [True, True]
    rows = b["proposals"][0]["diff"]
    fields = {(r["target"], r["field"]): r for r in rows}
    assert fields[("module", "description")]["to"].startswith("A small command-line")
    assert fields[("script.alpha", "summary")]["from"] == "Run alpha.py"
    assert fields[("script.alpha / n", "type")]["from"] == "string" and fields[("script.alpha / n", "type")]["to"] == "integer"
    assert ("script.alpha / n", "choices") in fields and ("script.alpha / n", "required") in fields
    assert (world.ops.read_bytes(), (world.overlay / "abp-module.toml").read_bytes()) == before
    listed = assist.proposals("fresh")["batches"][0]
    assert listed["batch"] == b["batch"] and listed["current"] and len(listed["proposals"]) == 2


@pytest.mark.parametrize("bad, why", [
    ({"operations": [{"id": "script.alpha", "summary": "x", "argv": ["rm", "-rf", "/"]}]}, "not allowed"),
    ({"description": "x", "commands": ["rm -rf /"]}, "not allowed"),
    ({"operations": [{"id": "script.new", "summary": "A new command"}]}, "not an operation"),
    ({"operations": [{"id": "script.alpha", "inputs": [{"name": "path", "type": "string"}]}]}, "not an existing input"),
    ({"operations": [{"id": "script.beta", "inputs": [{"name": "n", "type": "string"}]}]}, "not an existing input"),
    ({"operations": [{"id": "script.alpha", "inputs": [{"name": "n", "type": "file"}]}]}, "type must be"),
    ({"operations": [{"id": "script.alpha", "inputs": [{"name": "n", "required": "yes"}]}]}, "required must be"),
    ({"operations": [{"id": "script.alpha", "inputs": [{"name": "n", "type": "integer", "choices": ["a"]}]}]}, "choices"),
    ({"operations": [{"id": "script.alpha", "inputs": [{"name": "n", "type": "string", "flag": "--x"}]}]}, "not allowed"),
    ({"description": "x" * 500}, "longer than"),
    ({"operations": [{"id": "script.alpha", "summary": 5}]}, "text expected"),
    ({"operations": "script.alpha"}, "list expected"),
])
def test_invalid_proposals_are_rejected_and_cannot_be_kept(world, fake, bad, why):
    fake.answers = [bad]
    b = _gen(n=1)
    p = b["proposals"][0]
    assert not p["ok"] and p["proposal"] is None and any(why in x for x in p["problems"]), p["problems"]
    before = world.ops.read_bytes()
    with pytest.raises(ModuleError) as e:
        assist.keep("fresh", b["batch"], p["n"])
    assert e.value.code == "rejected" and world.ops.read_bytes() == before


def test_garbage_answers_and_failing_models(world, fake):
    fake.answers = ["I cannot do that."]
    b = _gen(n=1)
    assert not b["proposals"][0]["ok"] and "not JSON" in b["proposals"][0]["problems"][0]
    fake.answers = [RuntimeError("rate limited")]                    # every model fails: nothing proposed, reported
    b = _gen(n=1)
    assert b["proposals"] == [] and "every model failed" in b["errors"][0]


def test_nothing_is_written_before_keep_and_keep_writes_hand_edits_a_refresh_keeps(world, fake):
    from abp_modkit import spec as sp
    from bot.modules import refresh
    fake.answers = [GOOD]
    b = _gen(n=1)
    assert world.ops.read_text(encoding="utf-8").count("A small command-line") == 0
    r = assist.keep("fresh", b["batch"], 1)
    assert "abp-ops.toml" in r["wrote"] and "abp-module.toml" in r["wrote"]
    spec = sp.load(world.ops)
    alpha = spec.op("script.alpha")
    assert alpha.summary == "Count n things and print the result" and spec.service.description.startswith("A small command")
    assert alpha.inputs["n"] == {"type": "integer", "description": "how many things", "required": True,
                                 "enum": [1, 2, 3]}
    assert alpha.argv == ["{python}", "alpha.py", "--n", "{n}"]                 # no command changed
    assert spec.op("script.beta").argv == ["{python}", "alpha.py", "secrets.py"]
    assert registry.get("fresh").description.startswith("A small command")
    # upstream moves; a refresh keeps what was kept
    _w(world.maint / "gamma.py", SCRIPT)
    world.push("add gamma")
    refresh.refresh("fresh", apply=True)
    after = sp.load(world.ops)
    assert after.op("script.gamma") is not None
    assert after.op("script.alpha").summary == "Count n things and print the result"
    assert after.op("script.alpha").inputs["n"]["type"] == "integer" and after.op("script.alpha").inputs["n"]["enum"] == [1, 2, 3]
    assert after.service.description.startswith("A small command")
    assert registry.get("fresh").description.startswith("A small command")
    # a second keep of the same batch is stale (the files changed)
    with pytest.raises(ModuleError) as e:
        assist.keep("fresh", b["batch"], 1)
    assert e.value.code == "stale"


def test_a_stale_batch_cannot_be_kept(world, fake):
    from abp_modkit import spec as sp
    fake.answers = [GOOD]
    b = _gen(n=1)
    spec = sp.load(world.ops)
    spec.op("script.beta").summary = "edited meanwhile"
    world.ops.write_text(sp.dump(spec, header=world.ops.read_text(encoding="utf-8").splitlines()[0]), encoding="utf-8")
    assert assist.proposals("fresh")["batches"][0]["current"] is False
    with pytest.raises(ModuleError) as e:
        assist.keep("fresh", b["batch"], 1)
    assert e.value.code == "stale" and "A small command" not in world.ops.read_text(encoding="utf-8")


def test_secrets_in_source_are_never_sent_to_the_model(world, fake):
    fake.answers = [GOOD]
    _gen(n=2)
    sent = "\n".join(fake.prompts)
    assert "alpha.py" in sent and "A small tool" in sent                       # the source and README were shown
    assert API_KEY not in sent and GH_TOKEN not in sent                       # scrubbed
    assert FILE_SECRET not in sent and "secrets.py ---" not in sent           # a secret-looking file is not read


def test_a_hand_owned_ops_file_keeps_its_first_line(world, fake):
    from abp_modkit import spec as sp
    text = world.ops.read_text(encoding="utf-8")
    world.ops.write_text("# my own curated list\n" + text.partition("\n")[2], encoding="utf-8")
    fake.answers = [GOOD]
    b = _gen(n=1)
    assist.keep("fresh", b["batch"], 1, drop_comments=True)
    assert world.ops.read_text(encoding="utf-8").startswith("# my own curated list\n")
    assert sp.load(world.ops).op("script.alpha").summary.startswith("Count n")


def test_keep_refuses_to_lose_a_hand_owned_files_comments_unless_told(world, fake):
    from abp_modkit import spec as sp
    text = world.ops.read_text(encoding="utf-8")
    hand = "# my own curated list\n# second note\n" + text.partition("\n")[2].replace(
        'id = "script.beta"', '# beta is special\nid = "script.beta"').replace("[service]", "[service]  # inline note", 1)
    hand = hand.replace('summary = "Run alpha.py"', 'summary = "Run alpha.py # not a comment"')
    world.ops.write_text(hand, encoding="utf-8")
    fake.answers = [GOOD]
    b = _gen(n=1)
    assert assist.comment_lines(hand) == 5
    with pytest.raises(ModuleError) as e:
        assist.keep("fresh", b["batch"], 1)
    assert e.value.code == "comments" and "5 comment line" in str(e.value)
    assert world.ops.read_text(encoding="utf-8") == hand                       # nothing written
    assist.keep("fresh", b["batch"], 1, drop_comments=True)
    new = world.ops.read_text(encoding="utf-8")
    assert new.startswith("# my own curated list\n") and "second note" not in new
    assert sp.load(world.ops).op("script.alpha").summary.startswith("Count n")


def test_a_hand_owned_file_with_only_its_first_comment_needs_no_flag(world, fake):
    text = world.ops.read_text(encoding="utf-8")
    body = "\n".join(text.splitlines()[3:])
    world.ops.write_text("# mine\n" + body + "\n", encoding="utf-8")
    assert assist.comment_lines(world.ops.read_text(encoding="utf-8")) == 0
    fake.answers = [GOOD]
    b = _gen(n=1)
    assert "abp-ops.toml" in assist.keep("fresh", b["batch"], 1)["wrote"]


def test_comment_counting_ignores_hashes_in_strings():
    assert assist.comment_lines("# head\na = \"x # y\"\nb = 'p # q'\nc = 1 # real\n# whole\n") == 2
    assert assist.comment_lines('# head\nt = """\n# inside\n"""\n') == 0


def test_the_api_lists_generates_and_keeps(world, fake, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from bot import db
    from bot.dashboard import modules_api
    monkeypatch.setattr(db, "log_audit", lambda **kw: None)
    app = FastAPI()
    modules_api.register(app, lambda: None, lambda: None)
    c = TestClient(app)
    fake.answers = [GOOD]
    r = c.post("/api/modules/fresh/assist", json={"n": 1})
    assert r.status_code == 200 and r.json()["proposals"][0]["ok"]
    batch = r.json()["batch"]
    assert c.get("/api/modules/fresh/assist").json()["batches"][0]["batch"] == batch
    assert c.post("/api/modules/fresh/assist/keep", json={"batch": batch, "proposal": "x"}).status_code == 400
    assert c.post("/api/modules/fresh/assist/keep", json={"batch": "ffffff", "proposal": 1}).status_code == 404
    k = c.post("/api/modules/fresh/assist/keep", json={"batch": batch, "proposal": 1, "drop_comments": False})
    assert k.status_code == 200 and "abp-ops.toml" in k.json()["wrote"]
    assert c.get("/api/modules/nosuch/assist").status_code == 404
