"""LF-INT: the conductor's workers and planner use codeintel slices and the context vault instead of blind reads."""
from __future__ import annotations

import json
from types import SimpleNamespace

import test_conductor as base
import test_context_vault as tcv
from test_conductor import run_async

from abp_agenteval import factory
from bot import context_vault
from bot.agent_runtime import small_model
from bot.config import config
from bot.conductor import llm, navctx, planner, run

repo = base.repo
ALWAYS = {**run.conductor_pkg.settings(), "context": {"adaptive": False}}      # today's behaviour: always pre-seed


def spec_for(repo, tmp_path, key="conductor:p:t1"):
    return {"plan_id": "p", "goal": "add a json flag to the status command", "id": "t1", "key": key, "title": "Add a json flag to status",
            "detail": "the status subcommand in cli.py gets --json", "files": ["cli.py"], "proof": {"command": "python -m pytest tests -q"},
            "branch": "feat/cond-t1", "worktree": str(tmp_path / "wt" / "t1"), "base": "main", "after": []}


def test_drive_agent_offers_the_codeintel_tools_to_the_conductor_only(tmp_path, monkeypatch):
    seen = []

    async def fake_run_turn(prompt, **kw):
        seen.append(set(kw["extra_context"]["allowed_tools"]))
        return SimpleNamespace(ok=True, reply="", tool_calls=[], tokens=0, iterations=0, stopped="", error=None)

    from abp_run import core

    monkeypatch.setattr(core, "run_turn", fake_run_turn)
    for extra in (None, navctx.tool_names()):
        factory.drive_agent("p", tmp_path, object(), "m", timeout_s=5, max_iterations=2, extra_tools=extra)
    plain, conductor = seen
    assert not plain & set(navctx.NAV_TOOLS) and "context_recall" not in plain       # the bench's own set is unchanged
    assert set(navctx.NAV_TOOLS) <= conductor and "context_recall" in conductor and "read_file" in conductor
    assert set(small_model.CONDUCTOR_EXTRA) == set(navctx.tool_names())


def test_worker_prompt_has_a_capped_pack_and_vault_block_and_a_retry_reuses_it(repo, tmp_path, monkeypatch):
    monkeypatch.setitem(config._data, "context_vault", {"enabled": True})
    vault = tcv.make(tmp_path / "v")
    context_vault.set_vault(vault)
    try:
        vault.add("status flags were once added by editing build() in cli.py", "decision", project="abp")
        prompts, builds = [], []
        real = navctx.task_pack
        monkeypatch.setattr(navctx, "task_pack", lambda *a, **k: (builds.append(1), real(*a, **k))[1])

        async def drive(prompt, wt):
            prompts.append(prompt)
            return "ok"

        worker = run.AgentLoopWorker(repo, drive=drive, context_cfg=ALWAYS)
        spec = spec_for(repo, tmp_path)
        run_async(worker(spec, ""))
        run_async(worker(spec, "Fix exactly: the proof failed because --json is missing"))
        first, retry = prompts
        assert "Code context for this task" in first and "build" in first                  # a context_pack with cli.py's symbols
        assert "Relevant context from earlier work" in first and "editing build() in cli.py" in first
        assert len(first) < 4 * 6000                                                      # pack (2500 tokens) + vault stay within budget
        assert len(builds) == 1                                                           # the retry reused the cached pack
        assert "Code context for this task" in retry and "--json is missing" in retry
        # the reviewer's finding is remembered in the vault for the next attempt
        navctx.capture("Task 'x' attempt 1 was rejected by the reviewer: the proof failed", "review", "abp")
        assert "the proof failed" in navctx.vault_block("x rejected reviewer proof")
    finally:
        context_vault.set_vault(None)
        vault.close()


def test_a_second_rejection_carries_the_first(repo, tmp_path):
    prompts = []

    async def drive(prompt, wt):
        prompts.append(prompt)
        return "ok"

    worker = run.AgentLoopWorker(repo, drive=drive, context_cfg={**run.conductor_pkg.settings(), "context": {"use_vault": False}})
    spec = spec_for(repo, tmp_path)
    for fb in ("", "first finding", "second finding"):
        run_async(worker(spec, fb))
    assert "first finding" in prompts[2] and "second finding" in prompts[2] and "Earlier rejections" in prompts[2]


def test_codeintel_failure_degrades_to_the_plain_prompt(repo, tmp_path, monkeypatch):
    from bot.codeintel import tools

    def boom(*a, **k):
        raise RuntimeError("cannot index")

    monkeypatch.setattr(tools, "context_pack", boom)
    prompts = []

    async def drive(prompt, wt):
        prompts.append(prompt)
        return "ok"

    run_async(run.AgentLoopWorker(repo, drive=drive, context_cfg=ALWAYS)(spec_for(repo, tmp_path), ""))
    assert "Code context" not in prompts[0] and "Task: Add a json flag" in prompts[0]


def test_planner_prompt_has_repo_symbols_and_invented_paths_are_rejected(repo, tmp_path):
    ctx = navctx.planning_context(repo, "add a --json flag to the status command")
    assert "cli.py" in ctx and "build" in ctx
    prompts = []

    async def model(prompt, **kw):
        prompts.append(prompt)
        files = ["nope/missing.py"] if len(prompts) == 1 else ["cli.py", "tests/test_new.py"]
        return json.dumps({"tasks": [{"id": "a", "title": "A", "files": files, "proof": "python -m pytest tests -q"}]}), "m/x"

    llm.set_completer(model)
    plan = run_async(planner.make_plan("add json flag", context=ctx, root=repo, save=False, context_cap=9000))
    assert "build" in prompts[0] and len(prompts) == 2 and "do not exist" in prompts[1] and "nope/missing.py" in prompts[1]
    assert plan.tasks[0].files == ["cli.py", "tests/test_new.py"] and plan.planner.startswith("model:")
    assert navctx.unknown_paths(["cli.py", "tests/new.py", "ghost/x.py"], navctx.tracked_files(repo)) == ["ghost/x.py"]


def test_the_report_shows_the_tool_mix(repo):
    mix = navctx.tool_mix([{"tool": "read_file"}, {"tool": "read_file"}, {"tool": "read_symbol"}])
    assert mix["counts"] == {"read_file": 2, "read_symbol": 1} and mix["navigation_calls"] == 1 and not mix["skipped_navigation"]
    assert navctx.tool_mix([{"tool": "read_file"}])["skipped_navigation"] is True
    assert navctx.usage_delta({"a": {"calls": 1, "tokens": 5}}, {"a": {"calls": 3, "tokens": 9}, "b": {"calls": 0, "tokens": 0}}) == {
        "a": {"calls": 2, "tokens": 4}}


def _drive_once(repo, tmp_path, spec, cfg):
    prompts = []

    async def drive(prompt, wt):
        prompts.append(prompt)
        return SimpleNamespace(ok=True, reply="ok", tool_calls=[], tokens=0, iterations=0, stopped="", error=None)

    res = run_async(run.AgentLoopWorker(repo, drive=drive, context_cfg=cfg)(spec, ""))
    return prompts[0], res["tool_mix"]


def _big_repo(repo, n=5, size=6000):
    for i in range(n):
        body = "".join(f"def func_{i}_{j}(x):\n    return x + {j}\n\n" for j in range(size // 40))
        base.write(repo, f"big/m{i}.py", body)
    base.git(repo, "add", "-A")
    base.git(repo, "commit", "-q", "-m", "big")


def test_small_task_skips_the_pack_but_still_offers_the_nav_tools(repo, tmp_path):
    prompt, mix = _drive_once(repo, tmp_path, spec_for(repo, tmp_path), {**run.conductor_pkg.settings(), "context": {"use_vault": False}})
    assert "Code context for this task" not in prompt and mix["context_pack_tokens"] == 0
    assert mix["adaptive"]["adaptive"] is True and mix["adaptive"]["tokens"] == 0 and "small localized" in mix["adaptive"]["reason"]
    assert set(navctx.NAV_TOOLS) <= set(navctx.tool_names())                               # still offered


def test_large_multi_file_task_seeds_the_pack_up_to_the_cap(repo, tmp_path):
    _big_repo(repo)
    spec = {**spec_for(repo, tmp_path), "files": [f"big/m{i}.py" for i in range(5)], "detail": "touch func_0_1 and func_3_2 across the modules"}
    cfg = {**run.conductor_pkg.settings(), "context": {"use_vault": False, "min_bytes_for_pack": 10000}}
    prompt, mix = _drive_once(repo, tmp_path, spec, cfg)
    assert "Code context for this task" in prompt and mix["context_pack_tokens"] > 0
    assert mix["adaptive"]["tokens"] == 2500 and mix["adaptive"]["files"] == 5 and "pre-seed 2500 of 2500" in mix["adaptive"]["reason"]
    assert mix["context_pack_tokens"] <= 2500


def test_pack_scales_between_zero_and_the_cap(repo, tmp_path):
    _big_repo(repo, n=3)
    files = [f"big/m{i}.py" for i in range(3)]
    mid = navctx.decide_pack(repo, "x", files, 2500, {"context": {"min_files_for_pack": 3, "min_bytes_for_pack": 10**9}})
    assert 400 <= mid["tokens"] < 2500                                                      # at the threshold: part of the cap
    assert navctx.decide_pack(repo, "x", files[:1], 2500, {"context": {"min_bytes_for_pack": 10**9}})["tokens"] == 0


def test_adaptive_off_always_seeds_and_codeintel_off_never_does(repo, tmp_path):
    prompt, mix = _drive_once(repo, tmp_path, spec_for(repo, tmp_path), {**ALWAYS, "context": {"adaptive": False, "use_vault": False}})
    assert "Code context for this task" in prompt and mix["adaptive"]["adaptive"] is False
    assert navctx.decide_pack(repo, "x", [], 2500, {"context": {"use_codeintel": False}})["tokens"] == 0


def test_planner_context_shrinks_for_a_small_single_file_goal_and_is_full_for_a_broad_one(repo):
    small = navctx.decide_plan(repo, "add a --json flag in cli.py")
    assert small["pack"] == 0 and small["map"] < 1500 and "small single-file" in small["reason"]
    broad = navctx.decide_plan(repo, "rework the status, build and reporting across cli.py, bot/old.py and tests/test_cli.py")
    assert broad["pack"] == 3000 and broad["map"] == 1500
    off = navctx.decide_plan(repo, "add a --json flag in cli.py", {"context": {"adaptive": False}})
    assert off["pack"] == 3000 and off["map"] == 1500
    assert navctx.planning_context(repo, "add a --json flag in cli.py") != ""                  # a small map is still given
