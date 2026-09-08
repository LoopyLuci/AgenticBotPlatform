"""NativeAgentBackend.ask()'s SessionStart/UserPromptSubmit hook wiring
(Phase E of the Claude API/Claude Code parity plan) — SessionStart fires
only on a genuinely new session; UserPromptSubmit fires every turn.
Both hooks' additionalContext must reach the real request.
"""
from __future__ import annotations

import asyncio
import sys
import textwrap

from bot import db
from bot.backends.custom_model_backend import CustomModelBackend


def _run(coro):
    return asyncio.run(coro)


def _hook_script(tmp_path, body: str) -> str:
    script = tmp_path / f"hook_{abs(hash(body)) % 100000}.py"
    script.write_text(textwrap.dedent(body), encoding="utf-8")
    return f'"{sys.executable}" "{script}"'


CONTEXT_HOOK = """
    import json, sys
    json.dump({"additionalContext": "hook-injected context"}, sys.stdout)
"""


class _FakeResponse:
    def __init__(self, data):
        self._data = data

    def raise_for_status(self):
        pass

    def json(self):
        return self._data


class _FakeAsyncClient:
    def __init__(self, responses):
        self._responses = list(responses)
        self.requests = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, json=None, headers=None):
        self.requests.append(json)
        return _FakeResponse(self._responses.pop(0))


def _install(monkeypatch, responses):
    fake = _FakeAsyncClient(responses)
    monkeypatch.setattr("bot.agent_runtime.transports.openai_compatible.httpx.AsyncClient", lambda *, timeout: fake)
    return fake


def _backend():
    return CustomModelBackend(provider_name="local_ollama", model_id="llama3.1", base_url="http://127.0.0.1:11434/v1")


def test_session_start_hook_fires_on_a_new_session_and_reaches_the_system_prompt(temp_db, monkeypatch, tmp_path):
    db.add_agent_hook("SessionStart", _hook_script(tmp_path, CONTEXT_HOOK))
    fake = _install(monkeypatch, [{"choices": [{"message": {"role": "assistant", "content": "ok"}}]}])

    _run(_backend().ask("hi", context={"cwd": str(tmp_path / "ws")}))

    sent_messages = fake.requests[0]["messages"]
    system_messages = [m for m in sent_messages if m["role"] == "system"]
    assert any("hook-injected context" in m["content"] for m in system_messages)


def test_session_start_hook_does_not_fire_on_an_existing_session(temp_db, monkeypatch, tmp_path):
    db.add_agent_hook("SessionStart", _hook_script(tmp_path, CONTEXT_HOOK))
    fake = _install(monkeypatch, [{"choices": [{"message": {"role": "assistant", "content": "ok"}}]}])

    _run(_backend().ask("hi", context={"cwd": str(tmp_path / "ws"), "desktop_session_key": "custom-existing"}))

    sent_messages = fake.requests[0]["messages"]
    system_messages = [m for m in sent_messages if m["role"] == "system"]
    assert not any("hook-injected context" in m.get("content", "") for m in system_messages)


def test_user_prompt_submit_hook_reaches_the_user_message(temp_db, monkeypatch, tmp_path):
    db.add_agent_hook("UserPromptSubmit", _hook_script(tmp_path, CONTEXT_HOOK))
    fake = _install(monkeypatch, [{"choices": [{"message": {"role": "assistant", "content": "ok"}}]}])

    _run(_backend().ask("what's up?", context={"cwd": str(tmp_path / "ws")}))

    sent_messages = fake.requests[0]["messages"]
    user_message = sent_messages[-1]
    assert "hook-injected context" in user_message["content"]
    assert "what's up?" in user_message["content"]


def test_no_hooks_configured_behaves_exactly_as_before(temp_db, monkeypatch, tmp_path):
    fake = _install(monkeypatch, [{"choices": [{"message": {"role": "assistant", "content": "ok"}}]}])

    _run(_backend().ask("hi", context={"cwd": str(tmp_path / "ws")}))

    assert fake.requests[0]["messages"][-1]["content"] == "hi"
