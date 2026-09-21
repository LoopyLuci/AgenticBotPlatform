"""OpenCode and OpenClaw as delegating backends (roadmap P5), against a stand-in executable that records how it
was called. The real programs are not installed here; their command lines come from their documentation."""
from __future__ import annotations

import asyncio
import json
import os
import stat
import sys
import time
from pathlib import Path

import pytest

from bot.backends.base import BackendError
from bot.backends.external_agent_backend import OpenClawBackend, OpenCodeBackend, _find_text
from bot.router import VALID_BACKENDS

STAND_IN = r'''
import json, os, sys, time
mode = os.environ.get("STAND_IN_MODE", "ok")
open(os.environ["STAND_IN_LOG"], "w").write(json.dumps({"argv": sys.argv[1:], "cwd": os.getcwd()}))
if mode == "hang":
    time.sleep(60)
if mode == "fail":
    sys.stderr.write("boom: bad flag"); sys.exit(2)
if mode == "empty":
    sys.exit(0)
if mode == "json":
    print("log line before the envelope")
    print(json.dumps({"status": "completed", "result": {"payload": {"text": "the answer"}}, "usage": {"tokens": 5}}))
elif mode == "json-error":
    print(json.dumps({"status": "error", "error": {"message": "model unavailable"}, "text": "model unavailable"}))
else:
    print("\x1b[32mhello from the stand-in\x1b[0m")
'''


@pytest.fixture
def exe(tmp_path, monkeypatch):
    script = tmp_path / "stand_in.py"
    script.write_text(STAND_IN)
    if os.name == "nt":
        wrapper = tmp_path / "agent.cmd"
        wrapper.write_text(f'@"{sys.executable}" "{script}" %*\r\n')
    else:
        wrapper = tmp_path / "agent"
        wrapper.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n')
        wrapper.chmod(wrapper.stat().st_mode | stat.S_IEXEC)
    log = tmp_path / "log.json"
    monkeypatch.setenv("STAND_IN_LOG", str(log))
    monkeypatch.setenv("STAND_IN_MODE", "ok")

    def called():
        return json.loads(log.read_text())

    return str(wrapper), called


def ask(backend, prompt="do it", **ctx):
    return asyncio.run(backend.ask(prompt, context=ctx, timeout_s=30))


def test_both_are_selectable_backend_names():
    assert "opencode" in VALID_BACKENDS and "openclaw" in VALID_BACKENDS


def test_opencode_is_run_non_interactively_in_the_working_folder(exe, tmp_path):
    binary, called = exe
    work = tmp_path / "work"
    work.mkdir()
    result = ask(OpenCodeBackend(binary, model="anthropic/claude-sonnet-5", agent="build"), "fix the bug", cwd=str(work))
    assert result.text == "hello from the stand-in", "colour codes are removed"
    argv = called()["argv"]
    assert argv[0] == "run" and argv[argv.index("--model") + 1] == "anthropic/claude-sonnet-5" and argv[argv.index("--agent") + 1] == "build"
    assert argv[argv.index("--dir") + 1] == str(work) and argv[-2:] == ["--", "fix the bug"] and "--auto" not in argv
    assert Path(called()["cwd"]).resolve() == work.resolve()


def test_auto_approval_is_only_passed_when_asked_for(exe):
    binary, called = exe
    ask(OpenCodeBackend(binary, auto_approve=True))
    assert "--auto" in called()["argv"]


def test_a_prompt_that_looks_like_a_flag_is_not_one(exe):
    binary, called = exe
    ask(OpenCodeBackend(binary), "--dangerous-flag please")
    assert called()["argv"][-2:] == ["--", "--dangerous-flag please"]


def test_openclaw_gets_one_session_selector_and_json_output(exe, monkeypatch):
    binary, called = exe
    monkeypatch.setenv("STAND_IN_MODE", "json")
    result = ask(OpenClawBackend(binary, model="m1"), "hello", desktop_session_key="sess-9")
    assert result.text == "the answer"
    argv = called()["argv"]
    assert argv[:3] == ["agent", "--message", "hello"] and "--json" in argv and argv[argv.index("--session-key") + 1] == "abp:sess-9"
    assert argv[argv.index("--model") + 1] == "m1" and argv[argv.index("--timeout") + 1] == "30"
    ask(OpenClawBackend(binary, agent="ops"), "hello")
    argv = called()["argv"]
    assert argv[argv.index("--agent") + 1] == "ops" and "--session-key" not in argv


def test_extra_args_are_appended_for_version_differences(exe):
    binary, called = exe
    ask(OpenCodeBackend(binary, extra_args=["--format", "json"]))
    assert called()["argv"][-2:] == ["--format", "json"]


def test_failures_are_reported_with_what_the_program_said(exe, monkeypatch):
    binary, _ = exe
    monkeypatch.setenv("STAND_IN_MODE", "fail")
    with pytest.raises(BackendError, match="status 2: boom: bad flag"):
        ask(OpenCodeBackend(binary))
    monkeypatch.setenv("STAND_IN_MODE", "empty")
    with pytest.raises(BackendError, match="no text"):
        ask(OpenCodeBackend(binary))
    monkeypatch.setenv("STAND_IN_MODE", "json-error")
    with pytest.raises(BackendError, match="reported error: model unavailable"):
        ask(OpenClawBackend(binary))
    with pytest.raises(BackendError, match="not found on PATH"):
        ask(OpenCodeBackend("definitely-not-installed-xyz"))


def test_a_hung_program_is_killed_at_the_timeout(exe, monkeypatch):
    binary, _ = exe
    monkeypatch.setenv("STAND_IN_MODE", "hang")
    started = time.monotonic()
    with pytest.raises(BackendError, match="timed out after 2s"):
        asyncio.run(OpenCodeBackend(binary).ask("x", timeout_s=2))
    assert time.monotonic() - started < 20


def test_finding_the_text_in_an_unknown_envelope():
    assert _find_text({"status": "ok", "final": {"text": "deep"}}) == "deep"
    assert _find_text({"payloads": [{"text": "first"}, {"text": "last"}]}) == "last"
    assert _find_text({"nothing": 1}) is None


def test_the_router_builds_them_from_config(monkeypatch):
    from bot.router import Router

    router = Router.__new__(Router)
    cfg = {"backends": {"opencode": {"binary": "oc", "model": "a/b", "auto_approve": True, "extra_args": ["-x"]}, "openclaw": {"binary": "cl"}}}
    oc = Router._build_backend(router, "opencode", cfg)
    assert isinstance(oc, OpenCodeBackend) and (oc.binary, oc.model, oc.auto_approve, oc.extra_args) == ("oc", "a/b", True, ["-x"])
    assert isinstance(Router._build_backend(router, "openclaw", cfg, model_override="m"), OpenClawBackend)
