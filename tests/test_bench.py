"""The benchmark (abp_agenteval/bench.py): several models, repeated runs, provenance, resuming, and the leaderboard page.
Real models are not called here; scripted transports play models that are always right, sometimes wrong, and rate-limited."""
from __future__ import annotations

import json
from dataclasses import replace

import pytest

from abp_agenteval import bench, page
from abp_agenteval.__main__ import main
from abp_agenteval.scripted import ScriptedTransport
from abp_agenteval.suite import seed_suite
from abp_agenteval.task import Say
from bot.agent_runtime.usage_limits import RateLimited

TASK_IDS = ("create_file", "read_and_answer", "stay_in_workspace")


@pytest.fixture
def tasks():
    return [t for t in seed_suite() if t.id in TASK_IDS]


class _Limited(ScriptedTransport):
    async def send(self, **kw):
        raise RateLimited("free allowance used up for today")


def fake_models(calls: dict):
    """make_transport for bench.run: 'good' follows every golden trajectory; 'flaky' gives up on create_file on odd runs;
    'limited' is out of allowance; 'broken' cannot start."""
    def make(model: str):
        if model == "broken":
            raise ValueError("no such provider")
        calls[model] = calls.get(model, 0) + 1
        run_no = calls[model]

        def per_task(task):
            if model == "limited":
                return _Limited([])
            if model == "flaky" and task.id == "create_file" and run_no % 2 == 1:
                return ScriptedTransport([Say("I could not do that.")])
            return ScriptedTransport(task.script)
        return per_task, f"api-{model}"
    return make


def run_bench(tmp_path, tasks, models, repeats=2, calls=None):
    calls = {} if calls is None else calls
    # bench.run builds one factory per model; the 'flaky' factory must know which repeat it is on, so rebuild per repeat.
    done = {"ran": 0, "skipped": 0, "incomplete": 0}
    for k in range(1, repeats + 1):
        got = bench.run(models, k, tmp_path, tasks, fake_models(calls), log=lambda _m: None)
        for key in done:
            done[key] += got[key]
    return done


def test_runs_are_written_with_provenance_and_resumed(tmp_path, tasks):
    done = run_bench(tmp_path, tasks, ["good"], repeats=2)
    assert done == {"ran": 2, "skipped": 1, "incomplete": 0}
    files = sorted(p.name for p in tmp_path.glob("*.json"))
    fp = bench.fingerprint(tasks)
    assert files == [f"good__{fp}__r1.json", f"good__{fp}__r2.json"]
    report = json.loads((tmp_path / files[0]).read_text())
    assert report["bench"]["fingerprint"] == fp and report["bench"]["repeat"] == 1
    assert report["bench"]["abp_version"] and "commit" in report["bench"] and report["mode"] == "live"
    again = bench.run(["good"], 2, tmp_path, tasks, fake_models({}), log=lambda _m: None)
    assert again == {"ran": 0, "skipped": 2, "incomplete": 0, "unavailable": 0, "limited": 0},         "a restarted benchmark does not redo finished runs"


def test_the_leaderboard_shows_spread_reliability_and_categories(tmp_path, tasks):
    run_bench(tmp_path, tasks, ["good", "flaky"], repeats=2)
    s = bench.summarize(bench.load(tmp_path))
    good, flaky = s["models"]
    assert good["model"] == "good" and good["mean"] == 100.0 and good["reliable"] == 3 and good["runs"] == 2
    assert flaky["min"] < flaky["max"] and flaky["stdev"] is not None
    assert flaky["reliable"] == 2 and flaky["ever"] == 3 and flaky["flaky"] == ["create_file"]
    assert set(good["categories"]) == {"files", "safety"}
    text = bench.render(s)
    assert text.index("good") < text.index("flaky")


def test_a_rate_limited_run_does_not_count_and_the_next_model_still_runs(tmp_path, tasks):
    done = bench.run(["limited", "broken", "good"], 2, tmp_path, tasks, fake_models({}), log=lambda _m: None)
    assert done["incomplete"] == 1 and done["ran"] == 3
    runs = bench.load(tmp_path)
    assert [r["model"] for r in runs if r["incomplete"]] == ["limited"]
    s = bench.summarize(runs)
    assert [m["model"] for m in s["models"]] == ["good"] and s["incomplete"] == 1


def test_runs_of_another_suite_are_never_averaged_in(tmp_path, tasks):
    run_bench(tmp_path, tasks, ["good"], repeats=1)
    changed = [replace(t, prompt=t.prompt + " (reworded)") if t.id == "create_file" else t for t in tasks]
    assert bench.fingerprint(changed) != bench.fingerprint(tasks)
    import time

    time.sleep(0.01)
    bench.run(["flaky"], 1, tmp_path, changed, fake_models({}), log=lambda _m: None)
    s = bench.summarize(bench.load(tmp_path))
    assert [m["model"] for m in s["models"]] == ["flaky"] and s["left_out"] == 1


def test_the_label_is_not_what_is_sent_to_the_provider(tmp_path, tasks):
    """A run is labelled provider/model, but the provider receives its own model id (it would reject the label)."""
    sent = []

    class Spy(ScriptedTransport):
        async def send(self, *, model, **kw):
            sent.append(model)
            return await super().send(model=model, **kw)

    make = lambda _m: ((lambda task: Spy(task.script)), "qwen/qwen3-coder:free")   # noqa: E731
    bench.run(["openrouter/qwen/qwen3-coder:free"], 1, tmp_path, tasks[:1], make, log=lambda _m: None)
    assert sent and set(sent) == {"qwen/qwen3-coder:free"}
    assert bench.load(tmp_path)[0]["model"] == "openrouter/qwen/qwen3-coder:free"


def test_the_leaderboard_page(tmp_path, tasks):
    run_bench(tmp_path, tasks, ["good", "flaky"], repeats=2)
    out = tmp_path / "site" / "index.html"
    assert page.main([], str(out), bench_dir=str(tmp_path)) == 0
    html = out.read_text(encoding="utf-8")
    assert "Leaderboard" in html and "good" in html and "flaky" in html and "Passed only sometimes" in html
    assert bench.fingerprint(tasks) in html and "No other product" in html
    solo = tmp_path / "solo"
    run_bench(solo, tasks, ["good"], repeats=1)
    page.main([], str(out), bench_dir=str(solo))
    assert "Only one model so far" in out.read_text(encoding="utf-8")
    empty = tmp_path / "empty"
    empty.mkdir()
    page.main([], str(out), bench_dir=str(empty))
    assert "No complete benchmark runs yet" in out.read_text(encoding="utf-8")


def test_the_command_line(tmp_path, tasks, capsys, monkeypatch):
    run_bench(tmp_path, tasks, ["good"], repeats=1)
    assert main(["bench", "summary", "--dir", str(tmp_path)]) == 0
    assert "good" in capsys.readouterr().out
    assert main(["bench", "summary", "--dir", str(tmp_path), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["models"][0]["model"] == "good"
    monkeypatch.setattr(bench, "resolve_models", lambda spec: [])
    assert main(["bench", "run", "--models", "auto", "--dir", str(tmp_path)]) == 2
    assert "no models" in capsys.readouterr().err


def test_auto_means_the_routers_candidates_and_a_bare_name_is_refused(monkeypatch):
    from bot import model_router

    monkeypatch.setattr(model_router, "candidate_models", lambda: ["openrouter/a:free", "ollama/b"])
    assert bench.resolve_models("auto") == ["openrouter/a:free", "ollama/b"]
    assert bench.resolve_models(" x/y , z/w ,") == ["x/y", "z/w"]
    with pytest.raises(ValueError, match="names no configured provider"):
        bench.live_factory("just-a-model")


def test_a_run_cut_short_by_a_limit_is_redone_next_time(tmp_path, tasks):
    """It used to be written and then skipped for good, so a long benchmark never really resumed."""
    first = bench.run(["limited"], 1, tmp_path, tasks, fake_models({}), log=lambda _m: None)
    assert first["incomplete"] == 1
    fp = bench.fingerprint(tasks)
    path = tmp_path / f"limited__{fp}__r1.json"
    assert path.exists() and not bench.finished(path)
    # The allowance is back: the same file is redone and now counts.
    path.rename(tmp_path / f"good__{fp}__r1.json.tmp")
    (tmp_path / f"good__{fp}__r1.json.tmp").rename(tmp_path / f"good__{fp}__r1.json")
    again = bench.run(["good"], 1, tmp_path, tasks, fake_models({}), log=lambda _m: None)
    assert again["ran"] == 1 and again["skipped"] == 0 and bench.finished(tmp_path / f"good__{fp}__r1.json")


def test_the_probe_leaves_out_missing_models_and_retries_limited_ones(tmp_path, tasks):
    answers = {"good": "", "missing": "returned 404: model 'missing' not found", "limited": "limited: returned 429"}
    logs = []
    done = bench.run(["missing", "limited", "good"], 1, tmp_path, tasks, fake_models({}), log=logs.append,
                     probe=lambda factory, api_model: answers[api_model.removeprefix("api-")])
    assert done["unavailable"] == 1 and done["limited"] == 1 and done["ran"] == 1
    assert [r["model"] for r in bench.load(tmp_path)] == ["good"], "a model that is not there is never scored 0"
    assert any("missing: not available" in m for m in logs) and any("tried again next time" in m for m in logs)
