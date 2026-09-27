"""HermesCliBackend against a stand-in `hermes` executable that records its argv and writes a --usage-file the way
the real CLI does (see bot/backends/hermes_cli_backend.py's docstring for what was confirmed live against a real
Hermes Agent install: --resume genuinely continues a session, --usage-file's session_id is the value to resume with,
and --model/--reasoning are real top-level flags)."""
from __future__ import annotations

import asyncio
import json
import os
import stat
import sys
from pathlib import Path

import pytest

from bot.backends.base import BackendError
from bot.backends.hermes_cli_backend import HermesCliBackend

STAND_IN = r'''
import json, os, sys, time

mode = os.environ.get("STAND_IN_MODE", "ok")
argv = sys.argv[1:]
usage_path = argv[argv.index("--usage-file") + 1]
open(os.environ["STAND_IN_LOG"], "w").write(json.dumps(argv))

def write_usage(**extra):
    data = {"total_tokens": 42, "session_id": os.environ.get("STAND_IN_SESSION", "20260101_000000_abcdef")}
    data.update(extra)
    open(usage_path, "w").write(json.dumps(data))

if mode == "hang":
    time.sleep(60)
elif mode == "fail":
    sys.stderr.write("boom: bad flag"); sys.exit(2)
elif mode == "api_failed":
    print("API call failed: rate limited")
    write_usage()
elif mode == "no_usage_file":
    print("OK")
else:
    print("OK")
    write_usage()
'''


@pytest.fixture
def exe(tmp_path, monkeypatch):
    script = tmp_path / "stand_in.py"
    script.write_text(STAND_IN)
    if os.name == "nt":
        wrapper = tmp_path / "hermes.cmd"
        wrapper.write_text(f'@"{sys.executable}" "{script}" %*\r\n')
    else:
        wrapper = tmp_path / "hermes"
        wrapper.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n')
        wrapper.chmod(wrapper.stat().st_mode | stat.S_IEXEC)
    log = tmp_path / "log.json"
    monkeypatch.setenv("STAND_IN_LOG", str(log))
    monkeypatch.setenv("STAND_IN_MODE", "ok")
    monkeypatch.delenv("STAND_IN_SESSION", raising=False)

    def called():
        return json.loads(log.read_text())

    return str(wrapper), called


def ask(backend, prompt="hi", **ctx):
    return asyncio.run(backend.ask(prompt, context=ctx, timeout_s=30))


def test_a_plain_call_has_no_resume_or_reasoning_flag(exe):
    binary, called = exe
    result = ask(HermesCliBackend(binary))
    assert result.text == "OK"
    assert result.tokens == 42
    argv = called()
    assert "--resume" not in argv and "--reasoning" not in argv


def test_the_model_flag_is_passed_through(exe):
    binary, called = exe
    ask(HermesCliBackend(binary, model="anthropic/claude-sonnet-5"))
    argv = called()
    assert argv[argv.index("--model") + 1] == "anthropic/claude-sonnet-5"


def test_a_prior_session_is_resumed_and_the_same_id_comes_back(exe, monkeypatch):
    binary, called = exe
    monkeypatch.setenv("STAND_IN_SESSION", "20260101_000000_abcdef")
    result = ask(HermesCliBackend(binary), desktop_session_key="20260101_000000_abcdef")
    argv = called()
    assert argv[argv.index("--resume") + 1] == "20260101_000000_abcdef"
    assert result.raw["desktop_session_key"] == "20260101_000000_abcdef"


def test_a_first_call_has_no_resume_flag_but_still_reports_the_new_session_id(exe, monkeypatch):
    binary, called = exe
    monkeypatch.setenv("STAND_IN_SESSION", "20260202_111111_ffffff")
    result = ask(HermesCliBackend(binary))                      # no desktop_session_key in context: first-ever call
    assert "--resume" not in called()
    assert result.raw["desktop_session_key"] == "20260202_111111_ffffff"   # bot/router.py persists this for next time


def test_the_effort_ladder_maps_straight_through_to_reasoning(exe):
    binary, called = exe
    ask(HermesCliBackend(binary), effort="high")
    argv = called()
    assert argv[argv.index("--reasoning") + 1] == "high"


def test_an_unrecognized_effort_value_sets_no_reasoning_flag(exe):
    binary, called = exe
    ask(HermesCliBackend(binary), effort="not-a-real-level")
    assert "--reasoning" not in called()


def test_hermes_exiting_zero_with_an_api_failure_message_is_still_reported_as_an_error(exe, monkeypatch):
    binary, _ = exe
    monkeypatch.setenv("STAND_IN_MODE", "api_failed")
    with pytest.raises(BackendError, match="hermes reported a failure"):
        ask(HermesCliBackend(binary))


def test_a_nonzero_exit_is_reported_with_stderr(exe, monkeypatch):
    binary, _ = exe
    monkeypatch.setenv("STAND_IN_MODE", "fail")
    with pytest.raises(BackendError, match="hermes exited 2: boom: bad flag"):
        ask(HermesCliBackend(binary))


def test_a_missing_usage_file_still_returns_the_answer_with_no_session_key(exe, monkeypatch):
    binary, _ = exe
    monkeypatch.setenv("STAND_IN_MODE", "no_usage_file")
    result = ask(HermesCliBackend(binary))
    assert result.text == "OK"
    assert result.raw is None


def test_binary_not_found_is_a_clear_error():
    with pytest.raises(BackendError, match="not found on PATH"):
        ask(HermesCliBackend("definitely-not-a-real-hermes-binary-xyz"))


def test_a_hung_process_is_killed_at_the_timeout(exe, monkeypatch):
    binary, _ = exe
    monkeypatch.setenv("STAND_IN_MODE", "hang")
    with pytest.raises(BackendError, match="timed out after 1s"):
        asyncio.run(HermesCliBackend(binary).ask("hi", timeout_s=1))


def test_the_usage_file_is_always_cleaned_up(exe, tmp_path):
    binary, called = exe
    ask(HermesCliBackend(binary))
    argv = called()
    usage_path = Path(argv[argv.index("--usage-file") + 1])
    assert not usage_path.exists()
