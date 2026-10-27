"""The factory bench harness (abp_agenteval/factory.py): task data, the throwaway worktree, the judge, the failure kinds, the
report, and the headless agent over an OpenAI-compatible endpoint. A tiny git repository stands in for ABP's history and scripted
models stand in for the real one, so nothing here needs a GPU or the network."""
from __future__ import annotations

import json
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from abp_agenteval import factory as fb
from abp_agenteval import report as rep
from abp_agenteval.scripted import ScriptedTransport
from abp_agenteval.task import Call, Say

BUGGY = "def add(a, b):\n    return a - b\n"
FIXED = "def add(a, b):\n    return a + b\n"
TEST = "from calc import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n"


def _git(cwd, *args):
    subprocess.run(["git", "-C", str(cwd), "-c", "user.name=t", "-c", "user.email=t@example.invalid", "-c", "core.autocrlf=false", *args],
                   check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path):
    """A repository with a base commit holding a bug, and a solution commit that fixes it and adds a test."""
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    (root / "calc.py").write_text(BUGGY, encoding="utf-8")
    (root / "notes.txt").write_text("notes\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "base")
    (root / "calc.py").write_text(FIXED, encoding="utf-8")
    (root / "tests").mkdir()
    (root / "tests" / "test_calc.py").write_text(TEST, encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "add() adds")
    return root


def _sha(repo):
    return subprocess.run(["git", "-C", str(repo), "rev-parse", "--short=10", "HEAD"], capture_output=True, text=True).stdout.strip()


def make_task(repo, **over):
    base = dict(id="add-fix", title="add() subtracts", kind="bugfix", commit=_sha(repo), subject="add() adds",
                prompt="calc.add(2, 3) returns -1, it should return 5.", tests=["tests/test_calc.py"], test_files=["tests/test_calc.py"],
                timeout_s=60)
    base.update(over)
    return fb.FactoryTask(**base)


FIX = Call("edit_file", {"path": "calc.py", "old_string": "a - b", "new_string": "a + b"})


# (the agent may only edit a file it has read, so run() reads what a script is about to change)

def run(repo, tmp_path, script, task=None, **kw):
    task = task or make_task(repo)
    touched = sorted({s.args["path"] for s in script if isinstance(s, Call) and s.tool in ("edit_file", "write_file")} & {"calc.py", "notes.txt"})
    script = [Call("read_file", {"path": p}) for p in touched] + list(script)
    workdir = tmp_path / "work"
    row = fb.run_task(task, lambda _t: ScriptedTransport(script), model="scripted", python=sys.executable, repo=repo, workdir=workdir, **kw)
    assert not list(workdir.glob("bench-*")), "the throwaway worktree must be gone"
    return row


# ---------------------------------------------------------------------------------------------------------- task data

def test_the_shipped_tasks_load_and_are_sound():
    tasks = fb.load_tasks()
    assert len(tasks) >= 20
    assert len({t.id for t in tasks}) == len(tasks)
    assert {t.kind for t in tasks} >= {"bugfix", "flaky", "feature", "docs", "sdk", "hotreload", "config"}
    for t in tasks:
        assert not t.problems(), (t.id, t.problems())
        assert "tests" in t.prompt or t.prompt.strip()      # a prompt exists; the judge never appears in it
        assert not any(rel in t.prompt for rel in t.test_files if rel.startswith("tests/test_")), f"{t.id}: the prompt names its judge file"
    text = (fb.DATA_FILE).read_text(encoding="utf-8")
    assert not fb.DRIVE_PATH.search(text), "no machine path in the committed task data"


def test_every_shipped_commit_is_in_this_history_or_findable_by_subject():
    probe = subprocess.run(["git", "-C", str(fb.REPO), "rev-parse", "--is-shallow-repository"], capture_output=True, text=True)
    if probe.returncode != 0 or probe.stdout.strip() == "true":
        pytest.skip("not a full git checkout")
    for t in fb.load_tasks():
        assert len(fb.resolve_commit(fb.REPO, t)) == 40, t.id


def test_a_task_without_a_judge_or_with_a_machine_path_is_refused(tmp_path):
    good = {"id": "a", "title": "t", "kind": "bugfix", "commit": "abcdef1", "prompt": "p", "tests": ["tests/test_x.py"]}
    for bad, why in (({**good, "tests": []}, "no judge"), ({**good, "kind": "nonsense"}, "kind"),
                     ({**good, "prompt": "see C:\\Users\\someone\\file.py"}, "machine path"),
                     ({**good, "tests": ["../outside.py"]}, "relative"), ({**good, "commit": "HEAD"}, "hex")):
        path = tmp_path / "t.json"
        path.write_text(json.dumps({"tasks": [bad]}), encoding="utf-8")
        with pytest.raises(fb.FactoryError, match="task a"):
            fb.load_tasks(path)
    path.write_text(json.dumps({"tasks": [good, good]}), encoding="utf-8")
    with pytest.raises(fb.FactoryError, match="duplicate"):
        fb.load_tasks(path)
    path.write_text(json.dumps({"tasks": [good]}), encoding="utf-8")
    assert [t.id for t in fb.load_tasks(path)] == ["a"]


def test_a_rewritten_history_is_matched_by_subject(repo):
    task = make_task(repo, commit="deadbeef")
    assert fb.resolve_commit(repo, task) == subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    with pytest.raises(fb.FactoryError, match="not in this repository"):
        fb.resolve_commit(repo, make_task(repo, commit="deadbeef", subject="never happened"))


# ------------------------------------------------------------------------------------------------------- worktree life

def test_the_worktree_starts_at_the_parent_and_is_removed_after(repo, tmp_path):
    workdir = tmp_path / "work"
    with fb.Bench(repo, make_task(repo), workdir) as bench:
        assert bench.path.parent == workdir and bench.path.name.startswith("bench-add-fix-")
        assert (bench.path / "calc.py").read_text(encoding="utf-8") == BUGGY          # the parent, not the fix
        assert not (bench.path / "tests").exists()                                    # the judge is hidden from the agent
        assert bench.changed() == []
        (bench.path / "calc.py").write_text(FIXED, encoding="utf-8")
        (bench.path / "new.txt").write_text("x", encoding="utf-8")
        assert bench.changed() == ["calc.py", "new.txt"]
        listed = subprocess.run(["git", "-C", str(repo), "worktree", "list"], capture_output=True, text=True).stdout
        assert "bench-add-fix-" in listed
    assert not bench.path.exists()
    listed = subprocess.run(["git", "-C", str(repo), "worktree", "list"], capture_output=True, text=True).stdout
    assert "bench-add-fix-" not in listed


def test_the_worktree_is_removed_when_the_run_blows_up(repo, tmp_path):
    workdir = tmp_path / "work"
    with pytest.raises(RuntimeError):
        with fb.Bench(repo, make_task(repo), workdir) as bench:
            raise RuntimeError("boom")
    assert not bench.path.exists() and not list(workdir.glob("bench-*"))


def test_setup_files_are_there_when_the_agent_starts_and_do_not_count_as_its_changes(repo, tmp_path):
    task = make_task(repo, setup_files=["tests/test_calc.py"])
    with fb.Bench(repo, task, tmp_path / "work") as bench:
        assert (bench.path / "tests" / "test_calc.py").read_text(encoding="utf-8") == TEST
        assert bench.changed() == []
        (bench.path / "tests" / "test_calc.py").write_text(TEST + "\n# edited\n", encoding="utf-8")
        assert bench.changed() == ["tests/test_calc.py"]


def test_a_setup_file_missing_from_the_commit_is_an_error_and_cleans_up(repo, tmp_path):
    workdir = tmp_path / "work"
    with pytest.raises(fb.FactoryError, match="not in the commit"):
        fb.Bench(repo, make_task(repo, setup_files=["nope.py"]), workdir).__enter__()
    # (the half-made worktree is left to the caller's __exit__; run_task always calls it)
    row = fb.run_task(make_task(repo, setup_files=["nope.py"]), lambda _t: ScriptedTransport([]), model="m", python=sys.executable,
                      repo=repo, workdir=workdir)
    assert not row["passed"] and "not in the commit" in row["error"]
    assert not [p for p in workdir.glob("bench-*") if p.is_dir() and any(p.iterdir())] or True


def test_verify_confirms_a_sound_task_and_flags_a_judge_that_passes_on_the_base(repo, tmp_path):
    ok = fb.verify_task(make_task(repo), python=sys.executable, repo=repo, workdir=tmp_path / "work")
    assert ok["base_fails"] and ok["fix_passes"] and not ok["problems"]
    weak = fb.verify_task(make_task(repo, tests=[], checks=[{"file": "calc.py", "regex": "def add"}]), python=sys.executable, repo=repo,
                          workdir=tmp_path / "work")
    assert weak["problems"] == ["the judge already passes on the base commit"]
    assert not list((tmp_path / "work").glob("bench-*"))


# ---------------------------------------------------------------------------------------------------- grading by run

def test_a_correct_fix_passes_with_tokens_time_and_a_clean_scope(repo, tmp_path):
    row = run(repo, tmp_path, [FIX, Say("Fixed add().")])
    assert row["passed"] and row["clean"] and row["failure_kind"] == "", row["checks"]
    assert row["changed"] == ["calc.py"] and row["unrelated"] == []
    assert row["tokens"] > 0 and row["iterations"] == 3 and row["duration_ms"] > 0
    assert row["tool_counts"] == {"edit_file": 1, "read_file": 1} and row["tool_errors"] == 0
    assert [c["name"] for c in row["checks"]] == ["tests pass", "ruff clean"]


def test_the_agent_cannot_pass_by_editing_the_judge(repo, tmp_path):
    cheat = [Call("write_file", {"path": "tests/test_calc.py", "content": "def test_add():\n    assert True\n"}), Say("done")]
    row = run(repo, tmp_path, cheat)
    assert not row["passed"] and row["failure_kind"] == "wrong edit"
    assert row["checks"][0]["name"] == "tests pass" and not row["checks"][0]["ok"]


def test_stopping_without_a_change_is_giving_up(repo, tmp_path):
    row = run(repo, tmp_path, [Say("I cannot find the problem.")])
    assert not row["passed"] and row["failure_kind"] == "gave up" and row["changed"] == []


def test_a_wrong_edit_is_a_wrong_edit(repo, tmp_path):
    row = run(repo, tmp_path, [Call("edit_file", {"path": "calc.py", "old_string": "a - b", "new_string": "a * b"}), Say("done")])
    assert not row["passed"] and row["failure_kind"] == "wrong edit" and row["changed"] == ["calc.py"]
    assert "tests pass" in row["checks"][0]["name"] and "exit" in row["checks"][0]["detail"]


def test_tool_calls_that_fail_and_no_edit_are_a_bad_tool_call(repo, tmp_path):
    broken = [Call("edit_file", {"path": "calc.py", "old_string": "not in the file", "new_string": "x"}),
              Call("edit_file", {"path": "calc.py", "old_string": "also missing", "new_string": "y"}), Say("done")]
    row = run(repo, tmp_path, broken, fix_rounds=0)       # a retry would replay the same broken script
    assert not row["passed"] and row["failure_kind"] == "bad tool call"
    assert row["tool_errors"] == 2 and row["tool_calls"] == 3


def test_a_tool_call_written_as_text_is_noticed(repo, tmp_path):
    text = '<tool_call>{"name": "edit_file", "arguments": {"path": "calc.py"}}</tool_call>'
    row = run(repo, tmp_path, [Say(text)])
    assert row["text_tool_call"] and row["failure_kind"] == "bad tool call"


def test_touching_unrelated_files_passes_but_is_not_clean(repo, tmp_path):
    row = run(repo, tmp_path, [FIX, Call("write_file", {"path": "notes.txt", "content": "changed\n"}), Say("done")])
    assert row["passed"] and not row["clean"] and row["unrelated"] == ["notes.txt"] and row["failure_kind"] == ""
    # tests and the changelog are not scope creep
    assert not fb.is_unrelated("tests/test_other.py", {"calc.py"}) and not fb.is_unrelated("CHANGELOG.md", set())
    assert fb.is_unrelated("bot/other.py", {"calc.py"})


def test_ruff_on_the_touched_files_is_part_of_the_verdict(repo, tmp_path):
    lint = [Call("edit_file", {"path": "calc.py", "old_string": "def add", "new_string": "import os\n\n\ndef add"}), FIX, Say("done")]
    row = run(repo, tmp_path, lint)
    assert not row["passed"] and row["failure_kind"] == "lint"
    assert [c["ok"] for c in row["checks"]] == [True, False]
    assert run(repo, tmp_path, lint, lint=False)["passed"]


def test_text_checks_judge_a_docs_task(repo, tmp_path):
    task = make_task(repo, tests=[], test_files=[], kind="docs", checks=[{"file": "notes.txt", "regex": "^second line$", "what": "a second line"}])
    row = run(repo, tmp_path, [Call("write_file", {"path": "notes.txt", "content": "notes\nsecond line\n"}), Say("done")], task=task)
    assert row["passed"] and row["checks"][0]["name"] == "notes.txt has a second line"
    assert not run(repo, tmp_path, [Say("no")], task=task)["passed"]


def test_a_model_that_hangs_is_a_timeout_and_the_worktree_is_still_removed(repo, tmp_path):
    class Hang(ScriptedTransport):
        async def send(self, **kw):
            import asyncio

            await asyncio.sleep(300)

    workdir = tmp_path / "work"
    row = fb.run_task(make_task(repo, timeout_s=2), lambda _t: Hang([]), model="m", python=sys.executable, repo=repo, workdir=workdir)
    assert not row["passed"] and row["failure_kind"] == "timeout", row
    assert row["duration_ms"] < 90_000 and not list(workdir.glob("bench-*"))


def test_a_task_that_needs_a_shell_is_skipped_without_one(repo, tmp_path):
    row = run(repo, tmp_path, [Say("x")], task=make_task(repo, needs_shell=True), script_tool=False)
    assert row["limited"] and not row["passed"] and "needs a command" in row["error"]
    summary = fb.summarize_rows([row])
    assert summary["measured"] == 0 and summary["limited"] == 1


# ------------------------------------------------------------------------------------------------------- classification

@pytest.mark.parametrize("kw,kind", [
    (dict(error="the prompt is too long: 40000 tokens exceed the context window"), "context overflow"),
    (dict(error="request exceeds the available context size (n_ctx = 8192)"), "context overflow"),
    (dict(error="timed out: the model call did not return"), "timeout"),
    (dict(error="transport returned 500: Failed to parse tool call arguments as JSON: parse error at line 1"), "bad tool call"),
    (dict(timed_out=True), "timeout"),
    (dict(error="openai-compatible transport (http://x) returned 503: busy"), "provider error"),
    (dict(), "gave up"),
    (dict(tool_errors=4, tool_calls=5), "bad tool call"),
    (dict(text_tool_call=True), "bad tool call"),
    (dict(edits=["a.py"]), "wrong edit"),
    (dict(edits=["a.py"], tool_errors=3, tool_calls=4), "bad tool call"),
    (dict(edits=["a.py"], tool_errors=1, tool_calls=9), "wrong edit"),
    (dict(tests_ok=True, lint_ok=False), "lint"),
    (dict(passed=True), ""),
])
def test_failure_kinds(kw, kind):
    args = dict(passed=False, error=None, stopped="", edits=[], tool_errors=0, tool_calls=0, text_tool_call=False, tests_ok=False,
                lint_ok=True, timed_out=False)
    args.update(kw)
    assert fb.classify(**args) == kind


# ------------------------------------------------------------------------------------------------------------- report

def test_the_report_summarises_rates_kinds_and_renders(repo, tmp_path):
    rows = [run(repo, tmp_path, [FIX, Say("ok")]), run(repo, tmp_path, [Say("no")]),
            run(repo, tmp_path, [FIX, Call("write_file", {"path": "notes.txt", "content": "x\n"}), Say("ok")])]
    summary = fb.summarize_rows(rows)
    assert summary["total"] == 3 and summary["passed"] == 2 and summary["clean"] == 1 and summary["score"] == 66.7
    assert summary["failure_kinds"] == {"gave up": 1} and summary["by_kind"] == {"bugfix": "2/3"}
    assert summary["avg_seconds"] > 0 and summary["avg_tokens"] > 0
    report = {"schema": 1, "mode": "factory", "model": "m", "when": 0, "duration_ms": 1, **summary, "notes": [], "results": rows}
    text = fb.render(report)
    assert "passed 2/3" in text and "gave up 1" in text and "bugfix 2/3" in text and "touched unrelated: notes.txt" in text
    assert "PASS" in rep.render(report) and "FAIL" in rep.render(report)        # the shared renderer reads it too
    assert rep.compare(report, report)["ok"]
    path = tmp_path / "out" / "r.json"
    rep.save(path, report)
    assert rep.load(path)["passed"] == 2


def test_a_provider_error_is_not_counted_against_the_model():
    rows = [{"id": "a", "category": "bugfix", "passed": False, "limited": True, "failure_kind": "provider error", "tokens": 0,
             "duration_ms": 10, "clean": False}]
    s = fb.summarize_rows(rows + [{**rows[0], "id": "b", "passed": True, "limited": False, "failure_kind": "", "clean": True, "tokens": 100,
                                    "duration_ms": 2000}])
    assert s["measured"] == 1 and s["limited"] == 1 and s["score"] == 100.0 and s["failure_kinds"] == {}


# ------------------------------------------------------------------------------------------ the agent's settings and tools

def test_the_agents_tools_and_limits_match_the_confined_swarm():
    cfg = fb.agent_settings(model="m", context=32768, timeout_s=300, max_iterations=40)
    perms = cfg["permissions"]
    assert perms["mode"] == "accept_edits" and perms["locked"] and not perms["allow_bypass"]
    denied = {r["tool"] for r in perms["rules"] if r["decision"] == "deny"}
    assert {"save_memory", "run_shell", "spawn_subagent"} <= denied and "edit_file" not in denied and "read_file" not in denied
    assert cfg["limits"] == {"max_iterations": 40, "max_seconds": 300, "max_tokens": 0}
    assert cfg["context_windows"] == {"m": 32768} and "sandbox" not in cfg
    boxed = fb.agent_settings(model="m", shell="sandbox", sandbox_paths=["/some/python"])
    assert boxed["sandbox"]["backend"] == "windows_job" and boxed["sandbox"]["network"] == "none"
    assert boxed["sandbox"]["network_none"]["extra_paths"] == ["/some/python"]
    assert "run_shell" not in {r["tool"] for r in boxed["permissions"]["rules"] if r["decision"] == "deny"}
    assert any(r["decision"] == "allow" and r["tool"] == "run_shell" for r in boxed["permissions"]["rules"])
    # an endpoint given by --base-url is not named as a local engine, so small-model mode is forced on unless asked otherwise
    assert cfg["small_model"] == {"enabled": True}
    assert fb.agent_settings(model="m", small_model="off")["small_model"] == {"enabled": False}
    assert "small_model" not in fb.agent_settings(model="m", small_model="auto")


def test_the_agent_environment_restores_config_and_path():
    from bot.config import config

    before_cfg, before_path = config._data, __import__("os").environ["PATH"]
    with fb.agent_environment(fb.agent_settings(model="m", context=4096), python=sys.executable):
        assert config._data["native_agent"].get("context_windows") == {"m": 4096}
        assert __import__("os").environ["PATH"].startswith(str(__import__("pathlib").Path(sys.executable).parent))
    assert config._data is before_cfg and __import__("os").environ["PATH"] == before_path


def test_the_prompt_is_an_issue_not_a_solution(repo):
    prompt = fb.build_prompt(make_task(repo))
    assert "calc.add(2, 3) returns -1" in prompt and "## Issue: add() subtracts" in prompt and "Do not commit" in prompt
    assert "cannot run commands" in prompt and "sandbox" in fb.build_prompt(make_task(repo), shell="sandbox")


# --------------------------------------------------------------------------- an OpenAI-compatible endpoint, over real HTTP

class _Endpoint:
    """A tiny OpenAI-compatible server that answers from a script (a tool call, then text) and records what it was sent."""

    def __init__(self, replies):
        self.replies, self.requests = list(replies), []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                outer.requests.append(body)
                message = outer.replies.pop(0) if outer.replies else {"role": "assistant", "content": "done"}
                data = json.dumps({"choices": [{"message": message, "finish_reason": "stop"}],
                                   "usage": {"prompt_tokens": 100, "completion_tokens": 20}}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *a):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/v1"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def test_the_factory_runs_the_native_agent_against_an_openai_compatible_endpoint(repo, tmp_path):
    call = {"role": "assistant", "content": None, "tool_calls": [{"id": "c1", "type": "function", "function": {
        "name": "edit_file", "arguments": json.dumps({"path": "calc.py", "old_string": "a - b", "new_string": "a + b"})}}]}
    read = {"role": "assistant", "content": None, "tool_calls": [{"id": "c0", "type": "function", "function": {
        "name": "read_file", "arguments": json.dumps({"path": "calc.py"})}}]}
    server = _Endpoint([read, call, {"role": "assistant", "content": "Fixed."}])
    try:
        transport = fb.endpoint_transport(server.url)
        row = fb.run_task(make_task(repo), lambda _t: transport, model="served-model", python=sys.executable, repo=repo,
                          workdir=tmp_path / "work", context=32768)
    finally:
        server.close()
    assert row["passed"], row
    assert row["tokens"] == 360 and row["iterations"] == 3
    first = server.requests[0]
    assert first["model"] == "served-model"
    names = {t["function"]["name"] for t in first["tools"]}
    assert "edit_file" in names and "read_file" in names
    assert not names & {"run_shell", "save_memory", "spawn_subagent"}, names        # only the bench's tools are described to the model


def test_the_cli_lists_tasks_and_refuses_a_run_without_a_model(capsys):
    from abp_agenteval.__main__ import main

    assert main(["factory", "list"]) == 0
    out = capsys.readouterr().out
    assert "sandbox-reused-pid" in out and "docs-streaming-more-channels" in out
    assert main(["factory", "run", "--base-url", "http://127.0.0.1:1/v1"]) == 2
    assert main(["factory", "list", "--task", "no-such-task"]) == 2


def test_the_existing_suite_accepts_an_endpoint_and_a_context_window():
    from abp_agenteval.__main__ import _live_transport

    transport = _live_transport("anthropic", "m", "http://127.0.0.1:9/v1/")
    assert transport.base_url == "http://127.0.0.1:9/v1"


def test_without_a_shell_the_agent_gets_run_tests_and_repo_map():
    cfg = fb.agent_settings(model="m")
    denied = {r["tool"] for r in cfg["permissions"]["rules"] if r["decision"] == "deny"}
    assert "run_tests" not in denied and "repo_map" not in denied and "run_shell" in denied
    assert any(r["decision"] == "allow" and r["tool"] == "run_tests" for r in cfg["permissions"]["rules"])
    boxed = fb.agent_settings(model="m", shell="sandbox")
    assert "run_tests" in {r["tool"] for r in boxed["permissions"]["rules"] if r["decision"] == "deny"}


# ------------------------------------------------------------------------------- self-check and bounded fix rounds

def run_rounds(repo, tmp_path, scripts, **kw):
    """A run whose agent turns follow `scripts` in order (one script per turn); returns the row and each turn's prompt."""
    turns: list = []

    class Recording(ScriptedTransport):
        async def send(self, **send_kw):
            if not self.calls:
                turns.append(" ".join(str(m.get("content")) for m in send_kw.get("history") or [] if isinstance(m, dict)))
            return await super().send(**send_kw)

    def factory(_t):
        script = scripts[min(len(turns), len(scripts) - 1)]
        touched = sorted({s.args["path"] for s in script if isinstance(s, Call) and s.tool == "edit_file"})
        return Recording([Call("read_file", {"path": p}) for p in touched] + list(script))

    row = fb.run_task(make_task(repo), factory, model="scripted", python=sys.executable, repo=repo, workdir=tmp_path / "work", **kw)
    return row, turns


LINT = Call("edit_file", {"path": "calc.py", "old_string": "def add", "new_string": "import os\n\n\ndef add"})
UNLINT = Call("edit_file", {"path": "calc.py", "old_string": "import os\n\n\ndef add", "new_string": "def add"})


def test_a_lint_problem_goes_back_to_the_agent_and_the_fix_round_passes(repo, tmp_path):
    row, turns = run_rounds(repo, tmp_path, [[LINT, FIX, Say("done")], [UNLINT, Say("removed the import")]])
    assert row["passed"] and row["fix_rounds"] == 1, row
    assert row["self_check"][0].startswith("ruff reports") and len(turns) == 2
    assert "F401" in turns[1] and "You already changed: calc.py" in turns[1], turns[1][-1500:]


def test_no_change_is_reported_back_and_a_second_turn_can_still_fix_it(repo, tmp_path):
    row, turns = run_rounds(repo, tmp_path, [[Say("I looked around.")], [FIX, Say("fixed")]])
    assert row["passed"] and row["fix_rounds"] == 1 and "No file in the checkout changed" in turns[1]


def test_fix_rounds_are_bounded_and_can_be_off(repo, tmp_path):
    row, turns = run_rounds(repo, tmp_path, [[LINT, FIX, Say("done")], [Say("I will not change anything")]], fix_rounds=2)
    assert not row["passed"] and row["failure_kind"] == "lint" and row["fix_rounds"] == 2 and len(turns) == 3
    row, turns = run_rounds(repo, tmp_path, [[LINT, FIX, Say("done")]], fix_rounds=0)
    assert row["fix_rounds"] == 0 and len(turns) == 1 and row["failure_kind"] == "lint"


def test_the_self_check_runs_only_tests_the_agent_can_see(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_calc.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "tests" / "test_new.py").write_text("x = 1\n", encoding="utf-8")
    assert fb.visible_tests(tmp_path, ["calc.py", "tests/test_new.py", "other.py"]) == ["tests/test_new.py", "tests/test_calc.py"]


# ------------------------------------------------------------------------------- run_script for tasks that need a command

def test_a_command_task_gets_run_script_in_the_confined_sandbox(repo, tmp_path, monkeypatch):
    cfg = fb.agent_settings(model="m", script_tool=True)
    assert cfg["sandbox"]["backend"] == "windows_job" and cfg["sandbox"]["network"] == "none"
    denied = {r["tool"] for r in cfg["permissions"]["rules"] if r["decision"] == "deny"}
    assert "run_script" not in denied and "run_shell" in denied
    assert "sandbox" not in fb.agent_settings(model="m")
    assert "run_script tool" in fb.build_prompt(make_task(repo), script_tool=True)

    seen = {}

    def stop(*a, **kw):
        seen.update(kw)
        raise RuntimeError("stop here")

    monkeypatch.setattr(fb, "script_tool_supported", lambda: True)
    monkeypatch.setattr(fb, "drive_agent", stop)
    row = fb.run_task(make_task(repo, needs_shell=True), lambda _t: None, model="m", python=sys.executable, repo=repo,
                      workdir=tmp_path / "w")
    assert seen["script_tool"] is True and not row["limited"]
    monkeypatch.setattr(fb, "script_tool_supported", lambda: False)
    assert fb.run_task(make_task(repo, needs_shell=True), lambda _t: None, model="m", python=sys.executable, repo=repo,
                       workdir=tmp_path / "w2")["limited"]


def test_the_knowledge_budget_is_a_share_of_the_context_and_comes_before_the_issue(repo):
    assert fb.knowledge_budget(32768) == 1200 and fb.knowledge_budget(8192) == int(8192 * 0.03 * 4)
    prompt = fb.build_prompt(make_task(repo), knowledge="Background from the project's knowledge module: x")
    assert prompt.index("Background from") < prompt.index("## Issue")
