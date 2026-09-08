"""NativeAgentBackend.ask()'s image-understanding wiring (Phase D of the
native-BotServer-agents plan) — context["images"] threaded into a
vision-capable transport's user_message(), or dropped with an honest
note when the transport can't handle it. Uses CustomModelBackend (a real
OpenAICompatibleTransport, supports_vision=True) for the real end-to-end
path, and a small stub transport for the "no vision support" path.
"""
from __future__ import annotations

import asyncio

from bot.backends.custom_model_backend import CustomModelBackend
from bot.backends.native_backend import NativeAgentBackend


def _run(coro):
    return asyncio.run(coro)


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
        self.requests.append({"url": url, "json": json})
        return _FakeResponse(self._responses.pop(0))


def _install(monkeypatch, responses):
    fake = _FakeAsyncClient(responses)
    monkeypatch.setattr("bot.agent_runtime.transports.openai_compatible.httpx.AsyncClient", lambda *, timeout: fake)
    return fake


def _backend():
    return CustomModelBackend(provider_name="local_ollama", model_id="llava", base_url="http://127.0.0.1:11434/v1")


def test_a_valid_image_is_threaded_into_the_wire_request(temp_db, monkeypatch, tmp_path):
    fake = _install(monkeypatch, [
        {"choices": [{"message": {"role": "assistant", "content": "a cat"}}]},
    ])
    images = [{"data": b"\xff\xd8\xff fake jpeg", "mime_type": "image/jpeg"}]

    result = _run(_backend().ask("what is this?", context={"cwd": str(tmp_path / "ws"), "images": images}))

    assert result.text == "a cat"
    sent_content = fake.requests[0]["json"]["messages"][-1]["content"]
    assert any(block.get("type") == "image_url" for block in sent_content)


def test_an_oversized_image_is_dropped_with_a_note(temp_db, monkeypatch, tmp_path):
    from bot.agent_runtime import vision

    fake = _install(monkeypatch, [
        {"choices": [{"message": {"role": "assistant", "content": "no image, huh"}}]},
    ])
    images = [{"data": b"x" * (vision.MAX_IMAGE_BYTES + 1), "mime_type": "image/jpeg"}]

    _run(_backend().ask("what is this?", context={"cwd": str(tmp_path / "ws"), "images": images}))

    sent_content = fake.requests[0]["json"]["messages"][-1]["content"]
    assert isinstance(sent_content, str)  # dropped image -> no image blocks, plain text with the note appended
    assert "could not be processed" in sent_content


def test_no_images_behaves_exactly_as_before(temp_db, monkeypatch, tmp_path):
    fake = _install(monkeypatch, [
        {"choices": [{"message": {"role": "assistant", "content": "ok"}}]},
    ])
    _run(_backend().ask("hi", context={"cwd": str(tmp_path / "ws")}))
    assert fake.requests[0]["json"]["messages"][-1]["content"] == "hi"


class _NoVisionTransport:
    """A minimal ProviderTransport stub with supports_vision left at the
    base class default (False) — still accepts the `images` kwarg (every
    real transport must, per base.py's contract), it just always ignores
    it, matching how a real non-vision transport behaves."""

    supports_vision = False

    def __init__(self):
        self.sent_history: list = []

    def user_message(self, text, *, images=None):
        return {"role": "user", "content": text}

    def tool_result_messages(self, results):
        return []

    async def send(self, *, history, **kwargs):
        from bot.agent_runtime.transports.base import NormalizedResponse

        self.sent_history = list(history)  # copy: `history` keeps getting appended to after this call returns
        return NormalizedResponse(text="ok", assistant_message={"role": "assistant", "content": "ok"})


def test_a_transport_with_no_vision_support_drops_every_image_with_a_note(temp_db, tmp_path):
    transport = _NoVisionTransport()
    backend = NativeAgentBackend(transport, model="whatever")
    images = [{"data": b"real bytes", "mime_type": "image/jpeg"}]

    result = _run(backend.ask("what is this?", context={"cwd": str(tmp_path / "ws"), "images": images}))

    assert result.text == "ok"
    sent_prompt = transport.sent_history[-1]["content"]
    assert "could not be processed" in sent_prompt


def test_a_vision_capable_transport_gets_no_note_when_the_image_is_valid(temp_db, tmp_path):
    transport = _NoVisionTransport()
    transport.supports_vision = True
    backend = NativeAgentBackend(transport, model="whatever")
    images = [{"data": b"real bytes", "mime_type": "image/jpeg"}]

    _run(backend.ask("what is this?", context={"cwd": str(tmp_path / "ws"), "images": images}))

    sent_prompt = transport.sent_history[-1]["content"]
    assert "could not be processed" not in sent_prompt
    assert sent_prompt == "what is this?"
