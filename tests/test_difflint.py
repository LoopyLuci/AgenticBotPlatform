"""The pipeline's ML diff-lint (bot/conductor/difflint.py) with a stand-in model: parsing the model's findings, verifying each
suggested fix in a throwaway worktree of a real temp git repo (a fix that passes is kept, one that breaks a test is dropped),
the hub / GPU-lock gate and the time budget, the diff-hash cache, the owner's apply/dismiss and the precision stats, and that
the pipeline never fails because of this stage."""
from __future__ import annotations

import asyncio
import importlib.util
import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

from bot.conductor import difflint
from bot.conductor.difflint import Finding, Gate, Report

_SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"


# ------------------------------------------------------------------ helpers
def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True).stdout


def _write(repo: Path, rel: str, text: str) -> None:
    p = repo / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8", newline="\n")


@pytest.fixture
def repo(tmp_path):
    """base: calc.add with its test. head: calc.clamp with a slip (min against lo, not hi) and a test that catches it."""
    r = tmp_path / "repo"
    r.mkdir()
    _git(r, "init", "-q", "-b", "main")
    _git(r, "config", "user.email", "t@example.invalid")
    _git(r, "config", "user.name", "t")
    _git(r, "config", "core.autocrlf", "false")
    _write(r, "conftest.py", "")
    _write(r, "calc.py", "def add(a, b):\n    return a + b\n")
    _write(r, "tests/test_calc.py", "import calc\n\n\ndef test_add():\n    assert calc.add(2, 3) == 5\n")
    _git(r, "add", "-A")
    _git(r, "commit", "-qm", "base")
    base = _git(r, "rev-parse", "HEAD").strip()
    _write(r, "calc.py", "def add(a, b):\n    return a + b\n\n\ndef clamp(x, lo, hi):\n    return max(lo, min(x, lo))\n")
    _write(r, "tests/test_calc.py", "import calc\n\n\ndef test_add():\n    assert calc.add(2, 3) == 5\n\n\n"
                                    "def test_clamp():\n    assert calc.clamp(5, 0, 3) == 3\n")
    _git(r, "add", "-A")
    _git(r, "commit", "-qm", "clamp")
    return r, base


def plain_runner(kind, targets, ws, timeout):
    """ruff / pytest as plain subprocesses in the throwaway worktree (the sandbox runner is the conductor proof's own concern)."""
    if kind == "ruff":
        cmd = [sys.executable, "-m", "ruff", "check", "--no-cache", "--isolated", *targets]
    else:
        cmd = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-p", "no:xdist", "-p", "no:randomly", *targets]
    proc = subprocess.run(cmd, cwd=ws, capture_output=True, text=True, timeout=max(5, timeout))
    return proc.returncode == 0, proc.stdout + proc.stderr


GOOD = {"file": "calc.py", "line": 6, "category": "logic", "severity": "high",
        "explanation": "clamp compares against lo twice; the upper bound is never applied",
        "fix": {"old": "min(x, lo)", "new": "min(x, hi)"}}
BAD = {"file": "calc.py", "line": 2, "category": "logic", "severity": "low",
       "explanation": "add should subtract", "fix": {"old": "return a + b", "new": "return a - b"}}
NOFIX = {"file": "tests/test_calc.py", "line": 9, "category": "weak_tests", "severity": "medium",
         "explanation": "clamp's lower bound is not tested"}


def stand_in(findings, calls=None, delay=0.0):
    async def complete(prompt, *, system="", max_tokens=0, model="", timeout_s=0):
        if calls is not None:
            calls.append({"prompt": prompt, "model": model})
        if delay:
            await asyncio.sleep(delay)
        return "Here is my review:\n```json\n" + json.dumps({"findings": findings}) + "\n```", model
    return complete


def open_gate(lease_s):
    return Gate(True, model="modelmistress/stand-in-coder")


def _run(r, base, tmp_path, findings, **kw):
    kw.setdefault("complete", stand_in(findings))
    kw.setdefault("gate", open_gate)
    return difflint.run(r, base, "HEAD", runner=plain_runner, out_dir=tmp_path / "logs", state=tmp_path / "state",
                        run_name=kw.pop("run_name", "r1"), with_context=False, **kw)


# ------------------------------------------------------------------ parsing
def test_parse_findings_reads_fenced_json_and_normalises_fields():
    src = {"calc.py": "def f(x):\n    return x + 1\n"}
    text = ("Sure.\n```json\n" + json.dumps({"findings": [
        {"file": "./calc.py", "line": "2", "category": "Wrong API", "severity": "critical", "explanation": "off  by\none",
         "fix": {"old": "x + 1", "new": "x + 2"}},
        {"file": "calc.py", "line": 1, "category": "flaky", "severity": "info", "explanation": "event loop left open"},
        {"file": "", "explanation": "no file: dropped"},
        {"file": "/ws/abs/path.py", "explanation": "absolute: dropped"},
        {"file": "calc.py", "explanation": ""},
        "not an object",
    ]}) + "\n```\nDone.")
    found = difflint.parse_findings(text, read=src.get)
    assert [(f.file, f.line, f.category, f.severity) for f in found] == [
        ("calc.py", 2, "api_misuse", "high"), ("calc.py", 1, "flaky_test", "low")]
    assert found[0].explanation == "off by one"
    assert found[0].patch.startswith("--- a/calc.py\n+++ b/calc.py\n") and "+    return x + 2" in found[0].patch
    assert found[1].patch == "" and found[0].id.startswith("dl-") and found[0].id != found[1].id


def test_parse_findings_takes_a_unified_diff_a_bare_list_and_refuses_non_json():
    patch = "--- a/m.py\n+++ b/m.py\n@@ -1 +1 @@\n-a = 1\n+a = 2\n"
    found = difflint.parse_findings(json.dumps([{"file": "m.py", "line": 1, "category": "logic", "explanation": "x",
                                                 "patch": patch}]))
    assert found[0].patch == patch + "\n" or found[0].patch.rstrip("\n") == patch.rstrip("\n")
    assert difflint.parse_findings('{"findings": []}') == []
    with pytest.raises(ValueError):
        difflint.parse_findings("I found nothing worth reporting.")


def test_a_fix_that_cannot_be_placed_exactly_is_not_offered():
    src = "a = 1\na = 1\n"
    assert difflint.make_patch("m.py", src, "a = 1", "a = 2") == ""        # not unique
    assert difflint.make_patch("m.py", src, "b = 1", "b = 2") == ""        # not there
    assert difflint.make_patch("m.py", "x = 1", "x = 1", "x = 2").endswith("\\ No newline at end of file\n")


def test_parse_findings_caps_the_number():
    items = [{"file": "m.py", "line": i, "category": "logic", "explanation": f"issue {i}"} for i in range(20)]
    assert len(difflint.parse_findings(json.dumps({"findings": items}), limit=5)) == 5


# --------------------------------------------------------- verification
def test_a_fix_that_passes_is_kept_and_one_that_breaks_tests_is_dropped(repo, tmp_path):
    r, base = repo
    head = _git(r, "rev-parse", "HEAD").strip()
    rep = _run(r, base, tmp_path, [GOOD, BAD, NOFIX])
    assert rep.status == "ok", rep.reason
    by = {f.explanation: f for f in rep.findings if f.source == "model"}
    good, bad, nofix = by[GOOD["explanation"]], by[BAD["explanation"]], by[NOFIX["explanation"]]
    assert good.verified is True and "passed" in good.check and "test file" in good.check
    assert bad.verified is False and bad.check.startswith("dropped")
    assert nofix.verified is None and nofix.patch == ""
    assert [f.id for f in rep.verified] == [good.id]
    # outputs: the run's JSON and a .patch holding only the verified fix
    data = json.loads(Path(rep.json_path).read_text(encoding="utf-8"))
    assert Path(rep.json_path).name == "r1-difflint.json" and data["model"] == "modelmistress/stand-in-coder"
    patch = Path(rep.patch_path).read_text(encoding="utf-8")
    assert good.id in patch and bad.id not in patch and "+    return max(lo, min(x, hi))" in patch
    # the commit and the checkout are untouched, and the throwaway worktree is gone
    assert _git(r, "rev-parse", "HEAD").strip() == head and _git(r, "status", "--porcelain") == ""
    assert len(_git(r, "worktree", "list").strip().splitlines()) == 1
    assert "1 verified fix(es)" in rep.summary()


def test_a_patch_that_does_not_apply_is_dropped(repo, tmp_path):
    r, base = repo
    stale = {"file": "calc.py", "line": 1, "category": "logic", "explanation": "stale patch",
             "patch": "--- a/calc.py\n+++ b/calc.py\n@@ -1,2 +1,2 @@\n-def nothing_like_this():\n+def other():\n     pass\n"}
    rep = _run(r, base, tmp_path, [stale])
    f = [f for f in rep.findings if f.source == "model"][0]
    assert f.verified is False and "does not apply" in f.check


def test_the_owner_applies_a_verified_fix_to_the_working_tree(repo, tmp_path):
    r, base = repo
    rep = _run(r, base, tmp_path, [GOOD, BAD])
    good = rep.verified[0]
    bad = [f for f in rep.findings if f.verified is False][0]
    files = difflint.apply_patch(good.id, r, tmp_path / "logs", state=tmp_path / "state")
    assert files == "calc.py" and "min(x, hi)" in (r / "calc.py").read_text(encoding="utf-8")
    assert _git(r, "log", "--oneline").count("\n") == 2                    # nothing committed
    with pytest.raises(ValueError, match="no verified fix"):
        difflint.apply_patch(bad.id, r, tmp_path / "logs", state=tmp_path / "state")
    with pytest.raises(ValueError, match="no finding"):
        difflint.apply_patch("dl-0000000000", r, tmp_path / "logs", state=tmp_path / "state")
    stats = difflint.load_stats(tmp_path / "state")
    assert stats["findings"][good.id]["state"] == "accepted"
    assert stats["categories"]["logic"] == {"found": 2, "verified": 1, "accepted": 1, "dismissed": 0}


# ----------------------------------------------------- gate and budget
def test_skipped_when_the_gate_is_closed_and_the_model_is_never_called(repo, tmp_path):
    r, base = repo
    calls = []
    rep = _run(r, base, tmp_path, [GOOD], complete=stand_in([GOOD], calls),
               gate=lambda lease: Gate(False, "the ModelMistress hub is not running"))
    assert rep.status == "skipped" and calls == [] and rep.json_path == ""
    assert rep.summary() == "ML diff-lint skipped: the ModelMistress hub is not running"


def test_the_default_gate_skips_without_a_hub(monkeypatch):
    from bot.modelmistress import client

    monkeypatch.setattr(client, "find", lambda *a, **k: None)
    g = difflint.default_gate(60)
    assert not g.ok and "hub is not running" in g.reason


# ---- its own hub (the pipeline stopped live ABP, and live ABP's hub with it) ----------------------------------------
class _FakeHubLife:
    """client.find answers only while the stand-in hub "runs"; start_transient starts it, its stop ends it."""

    def __init__(self, monkeypatch, start_why=""):
        from bot.modelmistress import client, supervisor

        self.up, self.starts, self.stops, self.locks = False, [], [], []
        monkeypatch.setattr(client, "find", lambda *a, **k: client.Hub("http://127.0.0.1:9", "t", "0.3.0") if self.up else None)
        monkeypatch.setattr(client, "models", lambda hub=None: [{"id": "qwen3-coder-30b-a3b",
                                                                 "serving": {"n_ctx": 65536, "slots": 2}}])

        def stop():
            self.stops.append(1)
            self.up = False

        def start(wait_s):
            self.starts.append(wait_s)
            if start_why:
                return None, start_why, None
            self.up = True
            return client.Hub("http://127.0.0.1:9", "t", "0.3.0"), "", stop
        monkeypatch.setattr(supervisor, "start_transient", start)
        monkeypatch.setattr(difflint, "gpu_gate", lambda lease_s, board=None: (lambda: self.locks.append("released"), ""))


def test_without_start_hub_the_gate_never_starts_one(monkeypatch):
    life = _FakeHubLife(monkeypatch)
    g = difflint.default_gate(60)
    assert not g.ok and g.reason == "the ModelMistress hub is not running" and life.starts == []


def test_the_gate_starts_its_own_hub_and_stops_it_on_release(monkeypatch):
    life = _FakeHubLife(monkeypatch)
    g = difflint.default_gate(60, start_hub=True, start_wait_s=12)
    assert g.ok and g.started_hub and g.model == "modelmistress/qwen3-coder-30b-a3b" and life.starts == [12]
    assert life.up and life.stops == [] and life.locks == []
    g.release()
    assert not life.up and life.stops == [1] and life.locks == ["released"]
    difflint.stop_own_hub()                  # already stopped: nothing twice
    assert life.stops == [1]


def test_a_running_hub_is_used_never_a_second_one(monkeypatch):
    life = _FakeHubLife(monkeypatch)
    life.up = True
    g = difflint.default_gate(60, start_hub=True)
    assert g.ok and not g.started_hub and life.starts == []
    g.release()
    assert life.up and life.stops == []


def test_a_hub_that_cannot_be_started_is_a_skip_with_the_reason_and_the_lock_back(monkeypatch):
    life = _FakeHubLife(monkeypatch, start_why="not starting a hub from here: virtualized view")
    g = difflint.default_gate(60, start_hub=True)
    assert not g.ok and "could not start one: not starting a hub from here: virtualized view" in g.reason
    assert life.locks == ["released"]


def test_no_hub_is_started_while_the_gpu_lock_is_taken(monkeypatch):
    life = _FakeHubLife(monkeypatch)
    monkeypatch.setattr(difflint, "gpu_gate", lambda lease_s, board=None: (None, "the GPU lock custom:gpu0 is not free (x)"))
    g = difflint.default_gate(60, start_hub=True)
    assert not g.ok and "not free" in g.reason and life.starts == []


def test_the_run_stops_its_hub_after_the_model_call_and_says_so(monkeypatch, repo, tmp_path):
    r, base = repo
    life = _FakeHubLife(monkeypatch)
    up_during_call = []

    async def complete(prompt, **kw):
        up_during_call.append(life.up)
        return json.dumps({"findings": [NOFIX]}), kw["model"]
    rep = difflint.run(r, base, "HEAD", runner=plain_runner, out_dir=tmp_path / "logs", state=tmp_path / "state",
                       run_name="own", with_context=False, with_static=False, complete=complete, start_hub=True, budget=60)
    assert rep.status == "ok" and rep.started_hub and up_during_call == [True]
    assert not life.up and life.stops == [1] and life.locks == ["released"]
    assert "(own hub)" in rep.summary() and life.starts == [15.0]          # a quarter of the budget for the start
    assert json.loads(Path(rep.json_path).read_text(encoding="utf-8"))["started_hub"] is True


def test_the_run_stops_its_hub_even_when_the_model_fails(monkeypatch, repo, tmp_path):
    r, base = repo
    life = _FakeHubLife(monkeypatch)

    async def broken(prompt, **kw):
        raise ConnectionError("hub went away")
    rep = difflint.run(r, base, "HEAD", runner=plain_runner, out_dir=tmp_path / "logs", state=tmp_path / "state",
                       run_name="own", with_context=False, with_static=False, complete=broken, start_hub=True, budget=60)
    assert rep.status == "error" and not life.up and life.stops == [1] and life.locks == ["released"]


# ---- supervisor.start_transient: the guards ------------------------------------------------------------------------------
@pytest.fixture
def transient(monkeypatch, tmp_path):
    from bot.modelmistress import client, guard, supervisor
    from bot.sandbox_ns import cell as cell_mod

    monkeypatch.setattr(supervisor, "autostart_enabled", lambda: True)
    monkeypatch.setattr(supervisor, "_sandboxed", lambda: None)
    monkeypatch.setattr(client, "mm_home", lambda: tmp_path / "mm")
    monkeypatch.setattr(guard, "hub_processes", lambda home: [])
    monkeypatch.setattr(guard, "virtualized", lambda path: None)
    monkeypatch.setattr(guard, "engine_problem", lambda hub: None)
    st = {"up": False, "spawned": [], "killed": [], "stop_posts": []}

    class Cell:
        def kill(self, reason=""):
            st["killed"].append(reason)
            st["up"] = False

    def new_cell(preset="tool", *, name="", owner="", policy=None):
        st["policy"] = policy
        return Cell()

    def start(cell=None):
        st["spawned"].append(cell)
        st["up"] = True
        return cell

    def request(method, path, **kw):
        st["stop_posts"].append(path)
        st["up"] = False
    monkeypatch.setattr(cell_mod, "new_cell", new_cell)
    monkeypatch.setattr(supervisor, "_start", start)
    monkeypatch.setattr(client, "find", lambda *a, **k: client.Hub("http://127.0.0.1:9", "t", "0.3.0") if st["up"] else None)
    monkeypatch.setattr(client, "request", request)
    return supervisor, guard, st


def test_start_transient_starts_one_in_a_kill_on_close_cell_and_stops_it(transient):
    supervisor, _guard, st = transient
    hub, why, stop = supervisor.start_transient(5)
    assert hub is not None and why == "" and len(st["spawned"]) == 1
    assert st["policy"].persistent is False and st["policy"].kill_on_close is True     # dies with the pipeline if nothing else
    stop()
    assert st["stop_posts"] and len(st["killed"]) == 1 and not st["up"]
    stop()
    assert len(st["killed"]) == 1


@pytest.mark.parametrize("case", ["running", "virtualized", "autostart_off", "sandboxed"])
def test_start_transient_respects_the_guards(transient, monkeypatch, case):
    supervisor, guard, st = transient
    if case == "running":
        monkeypatch.setattr(guard, "hub_processes", lambda home: [4242])
        monkeypatch.setattr(guard, "virtualized", lambda path: "virtualized view")
        want = "a hub (pid 4242) already runs"
    elif case == "virtualized":
        monkeypatch.setattr(guard, "virtualized", lambda path: "virtualized view")
        want = "not starting a hub from here: virtualized view"
    elif case == "autostart_off":
        monkeypatch.setattr(supervisor, "autostart_enabled", lambda: False)
        want = "autostart is off"
    else:
        monkeypatch.setattr(supervisor, "_sandboxed", lambda: "an agent sandbox")
        want = "sandboxed"
    hub, why, stop = supervisor.start_transient(5)
    assert hub is None and stop is None and want in why and st["spawned"] == []


def test_start_transient_stops_a_hub_without_an_engine(transient, monkeypatch):
    supervisor, guard, st = transient
    monkeypatch.setattr(guard, "engine_problem", lambda hub: "the ModelMistress hub 0.3.0 has no inference engine")
    hub, why, stop = supervisor.start_transient(5)
    assert hub is None and stop is None and "no inference engine" in why and st["killed"] and not st["up"]


def test_start_transient_gives_up_on_a_hub_that_never_answers(transient, monkeypatch):
    supervisor, _guard, st = transient
    monkeypatch.setattr(supervisor, "_start", lambda cell=None: st["spawned"].append(cell) or cell)    # spawned, never answers
    hub, why, stop = supervisor.start_transient(1)
    assert hub is None and "did not answer within 1s" in why and st["killed"]


def test_the_gpu_lock_is_taken_released_and_respected(tmp_path):
    from bot.coordination.local import LocalBoard

    board = LocalBoard(tmp_path / "board.db")
    try:
        release, why = difflint.gpu_gate(60, board)
        assert release is not None and why == ""
        assert [lk["resource"] for lk in board.list_locks()] == ["custom:gpu0"]
        release()
        assert board.list_locks() == []
        board.acquire("someone-loading-a-model", ["custom:gpu0"], lease_s=60)
        release, why = difflint.gpu_gate(60, board)
        assert release is None and "custom:gpu0" in why and "not free" in why
    finally:
        board.close()


def test_the_lock_is_released_even_when_the_model_fails(repo, tmp_path):
    r, base = repo
    released = []

    async def broken(prompt, **kw):
        raise ConnectionError("hub went away")
    rep = _run(r, base, tmp_path, [], complete=broken,
               gate=lambda lease: Gate(True, model="modelmistress/x", release=lambda: released.append(1)))
    assert rep.status == "error" and "hub went away" in rep.reason and released == [1]


def test_a_slow_model_is_cut_off_at_the_budget(repo, tmp_path):
    r, base = repo
    t0 = time.monotonic()
    rep = _run(r, base, tmp_path, [GOOD], complete=stand_in([GOOD], delay=30), budget=2.5)
    assert rep.status == "error" and "budget" in rep.reason
    assert time.monotonic() - t0 < 15


def test_the_budget_comes_from_the_environment(monkeypatch):
    monkeypatch.setenv("ABP_PIPELINE_DIFFLINT_BUDGET_S", "42")
    assert difflint.budget_s() == 42
    monkeypatch.setenv("ABP_PIPELINE_DIFFLINT_BUDGET_S", "soon")
    assert difflint.budget_s() == difflint.DEFAULT_BUDGET_S


def test_hub_complete_refuses_anything_but_the_local_hub():
    from bot.conductor import llm

    with pytest.raises(llm.NoLocalModel):
        asyncio.run(difflint.hub_complete("x", model="anthropic/claude-x"))


# ---------------------------------------------------------------- cache
def test_a_diff_seen_before_is_answered_from_the_cache(repo, tmp_path):
    r, base = repo
    calls = []
    first = _run(r, base, tmp_path, [GOOD], complete=stand_in([GOOD], calls))
    gate_calls = []
    second = _run(r, base, tmp_path, [GOOD], complete=stand_in([GOOD], calls), run_name="r2",
                  gate=lambda lease: gate_calls.append(1) or open_gate(lease))
    assert len(calls) == 1 and gate_calls == []                     # neither the hub nor the GPU lock was needed
    assert second.status == "cached" and second.diff_hash == first.diff_hash
    assert [f.id for f in second.verified] == [f.id for f in first.verified]
    assert Path(second.json_path).name == "r2-difflint.json" and "(cached)" in second.summary()
    # a different diff is a new review
    _write(r, "calc.py", (r / "calc.py").read_text(encoding="utf-8") + "\n\ndef neg(x):\n    return -x\n")
    _git(r, "commit", "-qam", "neg")
    third = _run(r, base, tmp_path, [GOOD], complete=stand_in([GOOD], calls), run_name="r3")
    assert len(calls) == 2 and third.status == "ok" and third.diff_hash != first.diff_hash
    # the stats count each finding once however often it is reported again
    assert difflint.load_stats(tmp_path / "state")["categories"]["logic"]["found"] == 1


def test_an_empty_diff_is_skipped(repo, tmp_path):
    r, _base = repo
    rep = _run(r, "HEAD", tmp_path, [GOOD])
    assert rep.status == "skipped" and "nothing to review" in rep.reason


# ---------------------------------------------------------------- stats
def test_verdicts_move_the_per_category_counts_and_precision(tmp_path):
    st = tmp_path / "state"
    fs = [Finding("dl-a", "m.py", 1, "flaky_test", "high", "x", verified=True),
          Finding("dl-b", "m.py", 2, "flaky_test", "low", "y"),
          Finding("dl-c", "m.py", 3, "repo_rule", "high", "z", source="static")]
    difflint.record(fs, "r1-difflint.json", st)
    difflint.record(fs, "r2-difflint.json", st)
    row = difflint.load_stats(st)["categories"]["flaky_test"]
    assert row == {"found": 2, "verified": 1, "accepted": 0, "dismissed": 0}
    assert "dl-c" not in difflint.load_stats(st)["findings"]         # static rules are not the model's precision
    difflint.mark("dl-a", "accepted", st)
    difflint.mark("dl-b", "dismissed", st)
    assert difflint.precision(st)["flaky_test"] == 0.5 and difflint.precision(st)["logic"] is None
    difflint.mark("dl-b", "accepted", st)                            # the owner changed their mind
    assert difflint.load_stats(st)["categories"]["flaky_test"]["dismissed"] == 0
    assert difflint.precision(st)["flaky_test"] == 1.0
    with pytest.raises(KeyError):
        difflint.mark("dl-zzz", "dismissed", st)


def test_the_cli_dismisses_and_lists(repo, tmp_path, monkeypatch, capsys):
    r, base = repo
    monkeypatch.setenv("ABP_AGENT_STATE_DIR", str(tmp_path / "agent"))
    spec = importlib.util.spec_from_file_location("difflint_cli", _SCRIPTS / "difflint.py")
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    monkeypatch.setattr(cli, "LOG_DIR", tmp_path / "logs")
    rep = difflint.run(r, base, "HEAD", runner=plain_runner, out_dir=tmp_path / "logs", complete=stand_in([BAD]),
                       gate=open_gate, run_name="r1", with_context=False)
    fid = [f for f in rep.findings if f.source == "model"][0].id
    assert cli.main(["--list"]) == 0 and fid in capsys.readouterr().out
    assert cli.main(["--dismiss", fid]) == 0
    assert difflint.load_stats()["findings"][fid]["state"] == "dismissed"
    assert cli.main(["--apply", fid]) == 1                            # its fix was dropped: nothing to apply
    assert cli.main(["--stats"]) == 0 and "logic" in capsys.readouterr().out


# ------------------------------------------------- the pipeline integration
@pytest.fixture
def lp():
    sys.path.insert(0, str(_SCRIPTS))
    spec = importlib.util.spec_from_file_location("local_pipeline_difflint", _SCRIPTS / "local_pipeline.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["local_pipeline_difflint"] = mod
    spec.loader.exec_module(mod)
    return mod


def _scripted(monkeypatch, lp, outcomes):
    calls = []

    def fake_run(cmd, cwd=None, retries=0, timeout=None):
        calls.append(cmd)
        return outcomes[min(len(calls) - 1, len(outcomes) - 1)]
    monkeypatch.setattr(lp, "_run", fake_run)
    monkeypatch.setattr(lp, "_keep_pytest_output", lambda out: None)
    return calls


GREEN = [(True, ""), (True, ""), (True, ""), (True, "10 passed in 1s"), (True, "")]


def test_the_stage_runs_next_to_pytest_and_prints_one_line(monkeypatch, capsys, lp, tmp_path):
    monkeypatch.delenv("ABP_PIPELINE_DIFFLINT", raising=False)
    monkeypatch.setattr(lp, "_DIFF_RANGE", "aaa..bbb")
    seen = {}

    def fake_run(root, base, head, budget=None, **kw):
        seen.update(base=base, head=head, budget=budget, thread=__import__("threading").current_thread().name)
        rep = Report(status="ok", findings=[Finding("dl-1", "m.py", 1, "logic", "high", "x", "p", verified=True)])
        rep.json_path = str(lp.ROOT / "logs" / "local_pipeline" / "r-difflint.json")
        return rep
    monkeypatch.setattr(difflint, "run", fake_run)
    _scripted(monkeypatch, lp, GREEN)
    assert lp.check_python() is True
    out = capsys.readouterr().out
    assert seen["base"] == "aaa" and seen["head"] == "bbb" and seen["thread"] == "difflint"
    assert "ML diff-lint started next to pytest" in out
    assert "ML diff-lint: 1 finding(s), 1 verified fix(es)" in out and "r-difflint.json" in out


@pytest.mark.parametrize("behaviour", ["raises", "skipped", "error"])
def test_the_pipeline_never_fails_because_of_the_stage(monkeypatch, capsys, lp, behaviour):
    monkeypatch.delenv("ABP_PIPELINE_DIFFLINT", raising=False)
    monkeypatch.setattr(lp, "_DIFF_RANGE", "aaa..bbb")

    def fake_run(root, base, head, budget=None, **kw):
        if behaviour == "raises":
            raise RuntimeError("the stage itself is broken")
        return Report(status=behaviour, reason="the ModelMistress hub is not running" if behaviour == "skipped" else "boom")
    monkeypatch.setattr(difflint, "run", fake_run)
    _scripted(monkeypatch, lp, GREEN)
    assert lp.check_python() is True
    out = capsys.readouterr().out
    if behaviour == "skipped":
        assert "[--]   ML diff-lint skipped: the ModelMistress hub is not running" in out
    else:
        assert "[!]" in out and "advisory" in out


def test_a_real_pytest_failure_still_fails_with_the_stage_on(monkeypatch, lp):
    monkeypatch.delenv("ABP_PIPELINE_DIFFLINT", raising=False)
    monkeypatch.setattr(lp, "_DIFF_RANGE", "aaa..bbb")
    monkeypatch.setattr(difflint, "run", lambda *a, **k: Report(status="ok"))
    _scripted(monkeypatch, lp, [(True, ""), (True, ""), (True, ""), (False, "ImportError while collecting tests")])
    assert lp.check_python() is False


def test_an_overrunning_stage_is_left_behind(monkeypatch, capsys, lp):
    monkeypatch.delenv("ABP_PIPELINE_DIFFLINT", raising=False)
    monkeypatch.setattr(lp, "_DIFF_RANGE", "aaa..bbb")
    monkeypatch.setattr(lp, "DIFFLINT_JOIN_SLACK_S", 0.1)
    monkeypatch.setattr(difflint, "budget_s", lambda: 0.1)
    import threading
    release = threading.Event()           # held until the stage gave up, however long the other steps took
    monkeypatch.setattr(difflint, "run", lambda *a, **k: release.wait(60) and Report())
    _scripted(monkeypatch, lp, GREEN)
    try:
        assert lp.check_python() is True
        assert "overran" in capsys.readouterr().out
    finally:
        release.set()


def test_the_stage_can_be_switched_off(monkeypatch, capsys, lp):
    monkeypatch.setenv("ABP_PIPELINE_DIFFLINT", "0")
    monkeypatch.setattr(difflint, "run", lambda *a, **k: pytest.fail("must not run"))
    _scripted(monkeypatch, lp, GREEN)
    assert lp.check_python() is True
    assert "ML diff-lint: off" in capsys.readouterr().out
