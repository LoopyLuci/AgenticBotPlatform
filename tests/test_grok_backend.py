"""Grok Build (xAI's `grok` CLI) as a delegating backend, against a stand-in executable that records how it was called.
The error event and exit status match a live grok 1.0.62 run (402 "usage balance exhausted")."""
from __future__ import annotations

import asyncio
import json
import os
import stat
import sys

import pytest

from bot.backends.base import BackendError
from bot.backends.external_agent_backend import GrokBackend
from bot.router import VALID_BACKENDS

STAND_IN = r'''
import json, os, sys
mode = os.environ.get("STAND_IN_MODE", "ok")
a = sys.argv[1:]
prompt = open(a[a.index("--prompt-file") + 1], encoding="utf-8").read() if "--prompt-file" in a else None
open(os.environ["STAND_IN_LOG"], "w").write(json.dumps({"argv": a, "cwd": os.getcwd(), "prompt": prompt}))
if mode == "error":
    print(json.dumps({"type": "error", "message": "API error (status 402 Payment Required): Grok Build usage balance exhausted", "cwd": "/ws"}))
    sys.stderr.write("Error: Internal error"); sys.exit(1)
print(json.dumps({"type": "result", "session_id": "0b9c5d1e-sess", "result": "the grok answer"}))
'''


@pytest.fixture
def exe(tmp_path, monkeypatch):
    script = tmp_path / "stand_in.py"
    script.write_text(STAND_IN)
    if os.name == "nt":
        wrapper = tmp_path / "grok.cmd"
        wrapper.write_text(f'@"{sys.executable}" "{script}" %*\r\n')
    else:
        wrapper = tmp_path / "grok"
        wrapper.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n')
        wrapper.chmod(wrapper.stat().st_mode | stat.S_IEXEC)
    log = tmp_path / "log.json"
    monkeypatch.setenv("STAND_IN_LOG", str(log))
    monkeypatch.setenv("STAND_IN_MODE", "ok")
    return str(wrapper), lambda: json.loads(log.read_text())


def ask(backend, prompt="do it", **ctx):
    return asyncio.run(backend.ask(prompt, context=ctx, timeout_s=30))


def test_grok_is_a_selectable_backend():
    assert "grok" in VALID_BACKENDS


def test_headless_call_reply_and_session(exe, tmp_path):
    binary, called = exe
    res = ask(GrokBackend(binary, model="grok-4.7-build-fast"), cwd=str(tmp_path))
    assert res.text == "the grok answer"
    assert res.raw["desktop_session_key"] == "0b9c5d1e-sess"
    argv = called()["argv"]
    assert argv[0] == "--prompt-file" and called()["prompt"] == "do it" and "-p" not in argv
    assert not os.path.exists(argv[1])                                       # the prompt file is gone after the turn
    assert argv[argv.index("--output-format") + 1] == "json"
    assert argv[argv.index("--permission-mode") + 1] == "default"
    assert argv[argv.index("--model") + 1] == "grok-4.7-build-fast"
    assert "--resume" not in argv


def test_a_later_turn_resumes_the_session(exe):
    binary, called = exe
    ask(GrokBackend(binary), desktop_session_key="0b9c5d1e-sess")
    argv = called()["argv"]
    assert argv[argv.index("--resume") + 1] == "0b9c5d1e-sess"


def test_the_error_event_becomes_a_clear_backend_error(exe, monkeypatch):
    binary, _ = exe
    monkeypatch.setenv("STAND_IN_MODE", "error")
    with pytest.raises(BackendError, match="usage balance exhausted"):
        ask(GrokBackend(binary))


def test_text_only_and_sandbox_options(exe):
    binary, called = exe
    ask(GrokBackend(binary, tools="", web_search=False, sandbox="workspace", permission_mode="plan"))
    argv = called()["argv"]
    assert argv[argv.index("--tools") + 1] == ""
    assert "--disable-web-search" in argv
    assert argv[argv.index("--sandbox") + 1] == "workspace"
    assert argv[argv.index("--permission-mode") + 1] == "plan"


def test_auto_approve_only_accepts_edits():
    assert GrokBackend("grok", auto_approve=True).permission_mode == "acceptEdits"


@pytest.mark.parametrize("kw", [{"permission_mode": "bypassPermissions"}, {"extra_args": ["--always-approve"]},
                                {"extra_args": ["--permission-mode", "bypassPermissions"]}])
def test_approve_everything_is_refused(kw):
    with pytest.raises(ValueError):
        GrokBackend("grok", **kw)


def test_the_router_builds_it_from_config():
    from bot.router import Router

    router = Router.__new__(Router)
    cfg = {"backends": {"grok": {"binary": "g", "model": "grok-4.7", "permission_mode": "acceptEdits", "tools": "", "web_search": False}}}
    g = Router._build_backend(router, "grok", cfg)
    assert isinstance(g, GrokBackend)
    assert (g.binary, g.model, g.permission_mode, g.tools, g.web_search) == ("g", "grok-4.7", "acceptEdits", "", False)
    assert Router._build_backend(router, "grok", cfg, model_override="grok-4.6").model == "grok-4.6"


@pytest.mark.parametrize("ctx, want", [
    ({"permission_mode": "plan", "autonomy": 5}, "plan"),                   # plan mode wins over any autonomy
    ({"permission_mode": "default", "autonomy": 2}, "default"),             # manual approval
    ({"permission_mode": "accept_edits", "autonomy": 3}, "auto"),           # auto-build: headless acceptEdits cancels, auto edits
    ({"permission_mode": "accept_edits", "autonomy": 5}, "auto"),           # Sovereign
    ({}, "dontAsk"),                                                        # no switch sent: the configured mode stays
])
def test_the_workspace_switches_set_grok_s_mode_for_each_turn(exe, ctx, want):
    binary, called = exe
    backend = GrokBackend(binary, permission_mode="dontAsk")
    ask(backend, **ctx)
    argv = called()["argv"]
    assert argv[argv.index("--permission-mode") + 1] == want
    assert backend.permission_mode == "dontAsk" and "bypassPermissions" not in argv


def test_a_real_grok_reply_is_parsed():
    """The success shape a live grok 1.0.62 `-p --output-format json` run printed (2026-10-24; cwd neutralised)."""
    out = json.dumps({"text": "I'll create `ok.txt` with the text `hi`.Created `ok.txt` with the contents `hi`.",
                      "stopReason": "end_turn", "sessionId": "01a16fcc-82f0-7561-9297-c7fba9cef210",
                      "requestId": "7c8284a6-32bd-44a0-9cef-47a5bee089ff", "cwd": "/ws/probe",
                      "usage": {"input_tokens": 16407, "output_tokens": 109, "total_tokens": 36457}, "num_turns": 1})
    b = GrokBackend("grok")
    assert b.parse_output(out).endswith("Created `ok.txt` with the contents `hi`.")
    assert b.extra_raw(out).get("desktop_session_key") == "01a16fcc-82f0-7561-9297-c7fba9cef210"
    assert b.tokens_used(out) == 36457                                      # Grok's own count, recorded with the turn


def test_a_multi_line_prompt_reaches_grok_whole(exe):
    """Live 2026-10-24: through the npm grok.cmd wrapper, `-p <prompt>` lost everything after the first newline."""
    binary, called = exe
    prompt = "line one\nline two\n\nTask: the part that went missing"
    ask(GrokBackend(binary), prompt=prompt)
    assert called()["prompt"] == prompt


def test_plain_grok_means_the_native_binary_when_it_is_installed(tmp_path, monkeypatch):
    (tmp_path / "bin").mkdir()
    native = tmp_path / "bin" / ("grok.exe" if os.name == "nt" else "grok")
    native.write_text("")
    monkeypatch.setenv("GROK_HOME", str(tmp_path))
    assert GrokBackend("grok").binary == str(native)
    assert GrokBackend("/opt/custom/grok").binary == "/opt/custom/grok"      # an explicit path is kept
