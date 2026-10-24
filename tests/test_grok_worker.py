"""Grok as a Factory worker (bot/conductor/grok_worker.py): advisor mode hands Grok's plan to the confined local worker; confined
mode runs Grok in its own AppContainer with internet, only when the owner approved it; auto falls back to advisor."""
from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path

import pytest

from bot.agent_runtime import appcontainer
from bot.conductor import grok_worker
from bot.conductor.grok_worker import GrokUnavailable, GrokWorker, resolve_mode


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


class FakeLocal:
    def __init__(self, wt: Path):
        self.wt, self.seen = wt, []

    def prepare(self, spec):
        self.wt.mkdir(parents=True, exist_ok=True)
        if not (self.wt / ".git").exists():
            _git(self.wt, "init", "-q")
            _git(self.wt, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "base")
        return self.wt, subprocess.run(["git", "rev-parse", "HEAD"], cwd=self.wt, capture_output=True, text=True).stdout.strip()

    async def __call__(self, spec, feedback):
        self.seen.append((spec, feedback))
        return {"branch": spec["branch"], "summary": "local did it"}

    @staticmethod
    def _commit(wt, spec):
        _git(wt, "add", "-A")
        _git(wt, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", spec["title"])


SPEC = {"id": "t1", "key": "k1", "title": "Add slugify", "detail": "a helper", "goal": "slugs", "branch": "b1",
        "files": ["lib.py", "../outside.txt"], "proof": {"command": "pytest tests/test_lib.py"}}


def test_off_and_unknown_modes_are_refused():
    for mode in ("off", "yolo", None):
        with pytest.raises(GrokUnavailable):
            resolve_mode({"mode": mode})


def test_auto_without_approval_is_advisor_and_confined_without_it_refuses():
    assert resolve_mode({"mode": "auto"}) == "advisor"
    with pytest.raises(GrokUnavailable, match="network_approved"):
        resolve_mode({"mode": "confined"})


def test_auto_with_approval_is_confined_when_available(monkeypatch, tmp_path):
    monkeypatch.setattr(appcontainer, "is_supported", lambda: True)
    monkeypatch.setattr(grok_worker, "grok_exe", lambda: tmp_path / "grok.exe")
    assert resolve_mode({"mode": "auto", "network_approved": True}) == "confined"


def test_advisor_plan_reaches_the_local_worker_with_file_text_but_nothing_outside(tmp_path):
    wt = tmp_path / "wt"
    local = FakeLocal(wt)
    local.prepare(SPEC)
    (wt / "lib.py").write_text("def old(): pass\n")
    (tmp_path / "outside.txt").write_text("SECRET")
    prompts = []

    async def advise(prompt, *, model, timeout_s):
        prompts.append(prompt)
        return "1. add slugify to lib.py"

    w = GrokWorker(local, cfg={"grok": {"mode": "advisor", "model": "grok-4.7"}}, advise=advise)
    out = asyncio.run(w(SPEC, "too long"))
    assert out["grok"]["mode"] == "advisor" and out["summary"] == "local did it"
    spec, feedback = local.seen[0]
    assert spec["advice"] == "1. add slugify to lib.py" and feedback == "too long"
    assert "def old(): pass" in prompts[0] and "SECRET" not in prompts[0] and "too long" in prompts[0]


def test_a_failed_advisor_still_lets_the_local_worker_run(tmp_path):
    local = FakeLocal(tmp_path / "wt")

    async def advise(prompt, *, model, timeout_s):
        raise RuntimeError("402 usage balance exhausted")

    out = asyncio.run(GrokWorker(local, cfg={"grok": {"mode": "advisor"}}, advise=advise)(SPEC, ""))
    assert out["summary"] == "local did it" and local.seen[0][0]["advice"] == ""


def test_confined_runs_in_its_own_container_with_internet_and_commits(monkeypatch, tmp_path):
    monkeypatch.setattr(appcontainer, "is_supported", lambda: True)
    home = tmp_path / "grokhome"
    (home / "bin").mkdir(parents=True)
    (home / "bin" / "grok.exe").write_text("")
    monkeypatch.setenv("GROK_HOME", str(home))
    local = FakeLocal(tmp_path / "wt")
    calls = []

    async def run_confined(argv, timeout_s):
        calls.append(argv)
        wt = Path(argv[argv.index("--workspace") + 1])
        assert (wt / ".abp-grok-prompt.txt").read_text().startswith("Task: Add slugify")
        (wt / "lib.py").write_text("def slugify(s): return s\n")
        return 0, '{"text": "done", "sessionId": "s1", "stopReason": "end_turn"}'

    w = GrokWorker(local, cfg={"grok": {"mode": "confined", "network_approved": True}}, run_confined=run_confined)
    out = asyncio.run(w(SPEC, ""))
    argv = calls[0]
    assert "--internet" in argv and argv[argv.index("--profile") + 1] == appcontainer.AGENT_PROFILE_NAME
    assert argv[argv.index("--write-file") + 1] == str(home / "auth.json")
    assert str(Path.home()) not in [argv[i + 1] for i, a in enumerate(argv) if a in ("--read", "--write")]
    cmd = argv[-1]
    assert "--always-approve" not in cmd and "bypassPermissions" not in cmd and "--permission-mode auto" in cmd
    assert out["summary"] == "done" and out["empty"] is False and out["shell"] == "grok-confined"
    assert not (tmp_path / "wt" / ".abp-grok-prompt.txt").exists()


def test_a_failed_confined_turn_is_not_the_tasks_fault(monkeypatch, tmp_path):
    from bot.conductor import llm

    monkeypatch.setattr(appcontainer, "is_supported", lambda: True)
    monkeypatch.setattr(grok_worker, "grok_exe", lambda: tmp_path / "grok.exe")

    async def run_confined(argv, timeout_s):
        return 1, '{"type":"error","message":"API error (status 402 Payment Required): Grok Build usage balance exhausted"}'

    w = GrokWorker(FakeLocal(tmp_path / "wt"), cfg={"grok": {"mode": "confined", "network_approved": True}}, run_confined=run_confined)
    with pytest.raises(llm.WorkerModelError, match="usage balance exhausted"):
        asyncio.run(w(SPEC, ""))


def test_a_cancelled_confined_turn_fails_and_a_pretty_printed_reply_is_read(monkeypatch, tmp_path):
    """Live (2026-10-24): the launcher returns stdout+stderr, Grok's JSON is pretty-printed over many lines, and a turn that
    stopped before acting says stopReason "cancelled" - that is a failed turn, not an empty success."""
    from bot.conductor import llm

    monkeypatch.setattr(appcontainer, "is_supported", lambda: True)
    monkeypatch.setattr(grok_worker, "grok_exe", lambda: tmp_path / "grok.exe")
    reply = json.dumps({"text": "I'll implement shout.", "stopReason": "cancelled", "sessionId": "s"}, indent=2)
    stderr_tail = "\nsome stderr line\n"

    async def run_confined(argv, timeout_s):
        return 0, reply + stderr_tail

    assert grok_worker._reply(reply + stderr_tail) == "I'll implement shout."
    w = GrokWorker(FakeLocal(tmp_path / "wt"), cfg={"grok": {"mode": "confined", "network_approved": True}}, run_confined=run_confined)
    with pytest.raises(llm.WorkerModelError, match="cancelled"):
        asyncio.run(w(SPEC, ""))


def test_auto_falls_back_to_advisor_when_confined_grok_cannot_act(monkeypatch, tmp_path):
    """Live 2026-10-24: inside the AppContainer an edit to an existing file is cancelled. Auto must keep the run going on the
    advisor path (the local worker does the work) and stay there; an explicit `confined` still fails loudly."""
    monkeypatch.setattr(appcontainer, "is_supported", lambda: True)
    monkeypatch.setattr(grok_worker, "grok_exe", lambda: tmp_path / "grok.exe")
    monkeypatch.setattr(grok_worker, "_confined_broken", "")
    cancelled = json.dumps({"text": "I'll implement it.", "stopReason": "cancelled"}, indent=2)
    confined_calls, advice_calls = [], []

    async def run_confined(argv, timeout_s):
        confined_calls.append(argv)
        return 0, cancelled

    async def advise(prompt, *, model, timeout_s):
        advice_calls.append(prompt)
        return "the plan"

    cfg = {"grok": {"mode": "auto", "network_approved": True}}
    w = GrokWorker(FakeLocal(tmp_path / "wt"), cfg=cfg, run_confined=run_confined, advise=advise)
    assert w.mode == "confined"
    out = asyncio.run(w(SPEC, ""))
    assert out["grok"]["mode"] == "advisor" and len(confined_calls) == 1 and len(advice_calls) == 1
    assert GrokWorker(FakeLocal(tmp_path / "wt2"), cfg=cfg).mode == "advisor"           # the next run skips confined
    from bot.conductor import llm

    explicit = GrokWorker(FakeLocal(tmp_path / "wt3"), cfg={"grok": {"mode": "confined", "network_approved": True}},
                          run_confined=run_confined)
    with pytest.raises(llm.WorkerModelError, match="cancelled"):
        asyncio.run(explicit(SPEC, ""))


def test_the_launcher_only_offers_the_two_profiles():
    argv = appcontainer.launcher_argv("py", "x", Path("/ws"), Path("/ws"), internet=True, profile=appcontainer.AGENT_PROFILE_NAME,
                                      write_files=["/h/auth.json"])
    assert argv[argv.index("--profile") + 1] == appcontainer.AGENT_PROFILE_NAME and "--internet" in argv
    assert "--profile" not in appcontainer.launcher_argv("py", "x", Path("/ws"), Path("/ws"))
    assert "--internet" not in appcontainer.launcher_argv("py", "x", Path("/ws"), Path("/ws"))
