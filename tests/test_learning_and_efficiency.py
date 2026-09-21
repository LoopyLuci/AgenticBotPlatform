"""Trajectory export, the advisory router, and comparing wordings by running the evals (roadmap P8)."""
from __future__ import annotations

import asyncio
import json
import sys
from types import SimpleNamespace

import pytest

from abp_agenteval import compare as cmp
from abp_agenteval.scripted import ScriptedTransport
from abp_agenteval.task import Call, Say, Task
from abp_agenteval import graders as g
from bot import db, model_catalog, model_pricing, model_router
from bot.agent_runtime import tools, trajectory, usage_limits
from bot.backends.native_backend import NativeAgentBackend

pytestmark = pytest.mark.usefixtures("temp_db")


def run(coro):
    return asyncio.run(coro)


def do_run(tmp_path, steps, *, prompt="read the config", session="sess-1", files=None, approve_all=True):
    (tmp_path / "config.json").write_text('{"port": 8123, "owner": "alice@example.com"}')
    backend = NativeAgentBackend(ScriptedTransport(steps), model="m", name="t")
    return run(backend.ask(prompt, context={"cwd": str(tmp_path), "desktop_session_key": session, "source": "test"}))


# ---- trajectory export ---------------------------------------------------------------------------------------------
def test_a_finished_run_is_exported_as_a_chat_transcript_with_its_tool_calls(tmp_path):
    do_run(tmp_path, [Call("read_file", {"path": "config.json"}), Say("The port is 8123.")], prompt="What port?")
    records = trajectory.export()
    assert len(records) == 1
    rec = records[0]
    assert rec["outcome"] == "ok" and rec["tools"] == {"read_file": 1} and rec["iterations"] == 2 and rec["model"] == "m"
    roles = [m["role"] for m in rec["messages"]]
    assert roles[0] == "user" and "assistant" in roles and roles[-1] == "assistant" and rec["messages"][-1]["content"] == "The port is 8123."
    assert rec["messages"][0]["content"] == "What port?"
    assistant_calls = [m for m in rec["messages"] if m.get("tool_calls")]
    assert assistant_calls and assistant_calls[0]["tool_calls"][0]["name"] == "read_file"


def test_secrets_are_removed_and_pii_can_be_masked(tmp_path, monkeypatch):
    monkeypatch.setenv("MY_API_KEY", "server-secret-value-123")
    do_run(tmp_path, [Call("read_file", {"path": "config.json"}), Say("Owner is alice@example.com; key server-secret-value-123 sk-abcdefghijklmnopqrstu")],
           prompt="who owns it? call +1 555 123 4567 from 10.0.0.12")
    plain = json.dumps(trajectory.export())
    assert "server-secret-value-123" not in plain and "sk-abcdefghij" not in plain and "alice@example.com" in plain
    masked = json.dumps(trajectory.export(pii=True))
    assert "alice@example.com" not in masked and "[email]" in masked and "[phone]" in masked and "[ip]" in masked


def test_only_selected_runs_are_exported(tmp_path):
    do_run(tmp_path, [Say("hello")], prompt="hi", session="chat-only")
    do_run(tmp_path, [Call("read_file", {"path": "config.json"}), Say("done")], prompt="read it", session="with-tool")
    assert len(trajectory.export()) == 2
    assert [r["tools"] for r in trajectory.export(min_tool_calls=1)] == [{"read_file": 1}]
    assert trajectory.export(models=["other-model"]) == []
    assert len(trajectory.export(models=["m"])) == 2


def test_runs_with_denied_tool_calls_can_be_left_out(tmp_path, monkeypatch):
    from bot.agent_runtime import approval

    async def deny(*a, **k):
        return "deny"

    monkeypatch.setattr(approval, "request_approval", deny)
    do_run(tmp_path, [Call("write_file", {"path": "x.txt", "content": "y"}), Say("could not")], prompt="write x", session="denied-run")
    assert len(trajectory.export()) == 1 and trajectory.export(exclude_denied=True) == []


def test_a_session_with_several_runs_is_exported_once_with_the_whole_conversation(tmp_path):
    do_run(tmp_path, [Say("first answer")], prompt="first", session="same")
    do_run(tmp_path, [Say("second answer")], prompt="second", session="same")
    records = trajectory.export()
    assert len(records) == 1
    texts = [m["content"] for m in records[0]["messages"]]
    assert "first" in texts and "second" in texts and "second answer" in texts


def test_long_tool_output_is_shortened_and_identical_transcripts_are_written_once(tmp_path):
    (tmp_path / "big.txt").write_text("x" * 5000)
    do_run(tmp_path, [Call("read_file", {"path": "big.txt"}), Say("big")], prompt="read big", session="a")
    do_run(tmp_path, [Call("read_file", {"path": "big.txt"}), Say("big")], prompt="read big", session="b")
    records = trajectory.export(max_tool_chars=300)
    assert len(records) == 1, "the same transcript is not written twice"
    assert all(len(m["content"]) < 400 for m in records[0]["messages"] if m["role"] == "tool")


def test_the_command_line_refuses_without_confirmation_and_writes_jsonl(tmp_path, capsys):
    from abp_trajectory.__main__ import main

    do_run(tmp_path, [Say("hello")], prompt="hi")
    out = tmp_path / "runs.jsonl"
    assert main(["export", "--out", str(out)]) == 2 and not out.exists()
    assert "--confirm" in capsys.readouterr().err
    assert main(["export", "--out", str(out), "--confirm"]) == 0
    lines = out.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1 and json.loads(lines[0])["messages"][0]["content"] == "hi"


def test_other_transports_message_shapes_are_understood():
    openai = [{"role": "user", "content": "hi"},
              {"role": "assistant", "content": {"content": None, "tool_calls": [{"id": "c1", "function": {"name": "list_dir", "arguments": "{}"}}]}},
              {"role": "tool", "content": {"tool_call_id": "c1", "content": "a.txt"}}, {"role": "assistant", "content": {"content": "one file"}}]
    chat = trajectory.to_chat(openai)
    assert [m["role"] for m in chat] == ["user", "assistant", "tool", "assistant"] and chat[2]["name"] == "list_dir" and chat[2]["tool_call_id"] == "c1"


# ---- the router ---------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("task,expected", [
    ("hi, how are you?", "trivial"), ("what is the capital of France?", "trivial"),
    ("fix the failing test in auth.py, the traceback says KeyError", "coding"),
    ("Design the architecture for a multi-tenant billing system and analyse the trade-offs between the two database options " * 3, "hard_reasoning"),
    ("classify each of these 500 support tickets by urgency", "bulk"), ("summarise each of the following reviews", "bulk")])
def test_tasks_are_classified_from_their_words(task, expected):
    assert model_router.classify(task).task_class == expected


def test_images_and_long_material_decide_the_class():
    assert model_router.classify("what is this?", images=True).task_class == "vision"
    assert model_router.classify("summarise", context_tokens=90_000).task_class == "long_context"


CATALOG = {"p": {"id": "p", "models": {
    "big": {"id": "big", "tool_call": True, "reasoning": True, "modalities": {"input": ["text", "image"]}, "limit": {"context": 200000, "output": 8000},
            "cost": {"input": 3, "output": 15}},
    "cheap": {"id": "cheap", "tool_call": True, "modalities": {"input": ["text"]}, "limit": {"context": 32000, "output": 4000}, "cost": {"input": 0, "output": 0}},
    "notools": {"id": "notools", "tool_call": False, "modalities": {"input": ["text"]}, "limit": {"context": 200000, "output": 4000}, "cost": {"input": 0, "output": 0}},
}}}


@pytest.fixture
def catalog(monkeypatch):
    monkeypatch.setitem(model_pricing._memory_cache, "data", CATALOG)
    usage_limits._blocked_until.clear()
    yield
    usage_limits._blocked_until.clear()


def test_cheap_models_win_easy_tasks_and_strong_ones_win_hard_tasks(catalog):
    _, easy, _ = model_router.recommend("what is 2 + 2?", candidates=["p/big", "p/cheap"])
    assert easy[0].model == "p/cheap"
    _, hard, _ = model_router.recommend("Design the architecture and analyse the trade-offs of a queue-based ingestion pipeline for our repository. " * 3,
                                        candidates=["p/big", "p/cheap"])
    assert hard[0].model == "p/big"
    assert all(r.quality_source == "prior" for r in easy + hard), "with no measurements the quality is labelled a guess"


def test_models_that_cannot_do_the_job_are_left_out_with_a_reason(catalog):
    cls, ranked, skipped = model_router.recommend("fix the bug in the function", candidates=["p/big", "p/notools"])
    assert [r.model for r in ranked] == ["p/big"] and any("p/notools: no tool calling" in s for s in skipped)
    _, vision, skipped = model_router.recommend("what is in this photo?", candidates=["p/big", "p/cheap"], images=True)
    assert [r.model for r in vision] == ["p/big"] and any("cannot read images" in s for s in skipped)
    _, long, skipped = model_router.recommend("summarise this", candidates=["p/big", "p/cheap"], context_tokens=100_000)
    assert [r.model for r in long] == ["p/big"] and any("too small" in s for s in skipped)


def test_a_used_up_allowance_takes_a_model_out_of_the_running(catalog):
    usage_limits.note_rate_limited("p", "cheap", 600)
    _, ranked, skipped = model_router.recommend("what is 2 + 2?", candidates=["p/big", "p/cheap"])
    assert [r.model for r in ranked] == ["p/big"] and any("allowance is used up" in s for s in skipped)


def test_measured_eval_results_replace_the_guess(catalog):
    report = {"mode": "live", "results": [{"category": "coding", "passed": True}, {"category": "coding", "passed": True},
                                          {"category": "files", "passed": False}]}
    stored = model_router.record_eval_scores("p/cheap", report)
    assert stored["rates"] == {"coding": 1.0, "files": 0.0}
    _, ranked, _ = model_router.recommend("fix the bug in the function", candidates=["p/big", "p/cheap"])
    cheap = next(r for r in ranked if r.model == "p/cheap")
    assert cheap.quality_source == "measured" and cheap.quality == pytest.approx(0.5)
    assert next(r for r in ranked if r.model == "p/big").quality_source == "prior"


def test_a_scripted_eval_run_is_never_taken_as_a_measurement(catalog):
    model_router.record_eval_scores("p/cheap", {"mode": "scripted", "results": [{"category": "coding", "passed": True}]})
    assert model_router.measured_quality("p/cheap", "coding") is None


def test_the_description_says_what_is_guessed(catalog):
    cls, ranked, skipped = model_router.recommend("what is 2 + 2?", candidates=["p/big", "p/cheap"])
    text = model_router.describe(cls, ranked, skipped)
    assert "Task looks like: trivial" in text and "1. p/cheap" in text and "a guess" in text
    assert "No candidate model fits" in model_router.describe(cls, [], ["x: no tool calling"])


def test_the_agent_can_ask_for_advice_and_it_is_read_only(catalog):
    out = run(tools.execute_tool("suggest_model", {"task": "what is 2 + 2?", "candidates": ["p/big", "p/cheap"]}, workspace=None, instance_id=1))
    assert "p/cheap" in out and not tools.is_dangerous("suggest_model")


# ---- comparing wordings ------------------------------------------------------------------------------------------------------
def make_tasks():
    return [Task(id="t1", title="one", prompt="do it", script=[Say("ok")], graders=[g.finished_ok()], category="files"),
            Task(id="t2", title="two", prompt="do it", script=[Say("ok")], graders=[g.finished_ok()], category="files", config={"limits": {"max_steps": 9}})]


def test_variants_are_overlaid_on_each_task_and_the_tasks_own_settings_win():
    tasks = make_tasks()
    out = cmp.apply_variant(tasks, {"name": "v", "config": {"prompt": {"extra": "Be careful."}, "limits": {"max_steps": 3, "max_seconds": 50}}})
    assert out[0].config == {"prompt": {"extra": "Be careful."}, "limits": {"max_steps": 3, "max_seconds": 50}}
    assert out[1].config["limits"] == {"max_steps": 9, "max_seconds": 50}
    assert tasks[0].config == {}, "the originals are not changed"


def test_a_scripted_comparison_says_it_cannot_tell_variants_apart():
    variants = [{"name": "baseline", "config": {}}, {"name": "extra", "config": {"prompt": {"extra": "x"}}}]
    result = cmp.compare(make_tasks(), variants, lambda t: ScriptedTransport(t.script), mode="scripted", model="scripted")
    assert [v["passed"] for v in result["variants"]] == [2, 2] and result["variants"][1]["verdict"] == "no information (scripted)"
    assert "use --live" in cmp.render(result)


def test_live_differences_of_a_task_or_two_are_reported_as_noise_not_wins(monkeypatch):
    calls = {"n": 0}

    def fake_run_suite(tasks, make, *, mode, model, keep=False):
        calls["n"] += 1
        passed = 3 if calls["n"] == 1 else 4          # the second variant "passes one more"
        return {"mode": mode, "model": model, "passed": passed, "total": 10, "score": passed * 10.0, "tokens": 100 * calls["n"],
                "results": [{"id": f"t{i}", "passed": i < passed, "iterations": 2} for i in range(10)]}

    monkeypatch.setattr(cmp, "run_suite", fake_run_suite)
    result = cmp.compare([], [{"name": "a"}, {"name": "b"}], None, mode="live", model="m")
    assert result["variants"][1]["verdict"] == "within noise" and result["variants"][1]["gained"] == ["t3"]
    calls["n"] = 0
    monkeypatch.setattr(cmp, "run_suite", lambda *a, **k: fake_run_suite(*a, **k) if calls["n"] < 1 else {**fake_run_suite(*a, **k), "passed": 8,
                        "results": [{"id": f"t{i}", "passed": i < 8, "iterations": 1} for i in range(10)]})
    assert cmp.compare([], [{"name": "a"}, {"name": "b"}], None, mode="live", model="m")["variants"][1]["verdict"] == "better"


def test_variant_files_are_validated(tmp_path):
    good = tmp_path / "v.json"
    good.write_text(json.dumps([{"name": "a"}, {"name": "b", "config": {}}]))
    assert [v["name"] for v in cmp.load_variants(str(good))] == ["a", "b"]
    for bad in ('[]', '{"name": "a"}', '[{"config": {}}]', '[{"name": "a"}, {"name": "a"}]'):
        (tmp_path / "bad.json").write_text(bad)
        with pytest.raises(ValueError):
            cmp.load_variants(str(tmp_path / "bad.json"))


def test_tool_descriptions_can_be_overridden_by_configuration(monkeypatch):
    from bot.config import config

    original = next(s for s in tools.all_tool_schemas() if s["name"] == "edit_file")
    monkeypatch.setattr(config, "_data", {**config._data, "native_agent": {**(config._data.get("native_agent") or {}),
                                                                             "tool_descriptions": {"edit_file": "Change text. Read first.", "nonexistent": "x"}}})
    changed = next(s for s in tools.all_tool_schemas() if s["name"] == "edit_file")
    assert changed["description"] == "Change text. Read first." and changed["input_schema"] == original["input_schema"] and changed["name"] == "edit_file"
    assert {s["name"] for s in tools.all_tool_schemas()} == {s["name"] for s in [original] + [t for t in tools.all_tool_schemas() if t["name"] != "edit_file"]}


def test_the_compare_command_runs_end_to_end_in_scripted_mode(tmp_path, capsys):
    from abp_agenteval.__main__ import main

    variants = tmp_path / "v.json"
    variants.write_text(json.dumps([{"name": "baseline"}, {"name": "terse", "config": {"tool_descriptions": {"edit_file": "Edit."}}}]))
    assert main(["compare", "--variants", str(variants), "--task", "create_file", "--task", "read_and_answer"]) == 0
    out = capsys.readouterr().out
    assert "baseline" in out and "terse" in out and "no information (scripted)" in out
    assert main(["compare", "--variants", str(tmp_path / "missing.json")]) == 2


def test_the_route_command_gives_advice(catalog, monkeypatch):
    from bot import commands
    from bot.config import config

    monkeypatch.setattr(config, "_data", {**config._data, "native_agent": {**(config._data.get("native_agent") or {}), "router": {"candidates": ["p/big", "p/cheap"]}}})
    ctx = commands.CmdContext(instance_id=None, instance_name="t", user_id=1, chat_id=1, actor="t")
    out = run(commands.cmd_route(ctx, "what is 2 + 2?"))
    assert "Task looks like: trivial" in out and "1. p/cheap" in out
    assert "Usage:" in run(commands.cmd_route(ctx, "  "))
