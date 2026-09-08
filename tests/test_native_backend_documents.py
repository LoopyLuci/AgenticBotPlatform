"""NativeAgentBackend.ask()'s document threading (Phase C of the Claude
API/Claude Code parity plan) — context["documents"] threaded into a
document-capable transport's user_message(), or dropped with an honest
"document" note when the transport can't handle it (either because it's
not Anthropic at all, or the document itself is oversized/unsupported).
"""
from __future__ import annotations

import asyncio

from bot.agent_runtime.transports.base import NormalizedResponse
from bot.backends.native_backend import NativeAgentBackend


def _run(coro):
    return asyncio.run(coro)


class _RecordingTransport:
    supports_vision = False
    supports_documents = False

    def __init__(self):
        self.sent_history: list = []

    def user_message(self, text, *, images=None, documents=None):
        return {"role": "user", "content": text, "_documents": documents}

    def tool_result_messages(self, results):
        return []

    async def send(self, *, history, **kwargs):
        self.sent_history = list(history)
        return NormalizedResponse(text="ok", assistant_message={"role": "assistant", "content": "ok"})


def test_a_transport_with_no_document_support_drops_every_document_with_a_note(temp_db, tmp_path):
    transport = _RecordingTransport()
    backend = NativeAgentBackend(transport, model="whatever")
    documents = [{"data": b"%PDF fake", "mime_type": "application/pdf"}]

    result = _run(backend.ask("what does this say?", context={"cwd": str(tmp_path / "ws"), "documents": documents}))

    assert result.text == "ok"
    sent_prompt = transport.sent_history[-1]["content"]
    assert "1 attached document could not be processed" in sent_prompt


def test_a_document_capable_transport_gets_it_threaded_through(temp_db, tmp_path):
    import base64

    transport = _RecordingTransport()
    transport.supports_documents = True
    backend = NativeAgentBackend(transport, model="whatever")
    documents = [{"data": b"%PDF fake", "mime_type": "application/pdf"}]

    _run(backend.ask("what does this say?", context={"cwd": str(tmp_path / "ws"), "documents": documents}))

    sent_entry = transport.sent_history[-1]
    assert "could not be processed" not in sent_entry["content"]
    assert sent_entry["_documents"] == [{"mime_type": "application/pdf", "data_b64": base64.b64encode(b"%PDF fake").decode("ascii")}]


def test_an_oversized_document_is_dropped_even_on_a_capable_transport(temp_db, tmp_path):
    from bot.agent_runtime import vision

    transport = _RecordingTransport()
    transport.supports_documents = True
    backend = NativeAgentBackend(transport, model="whatever")
    documents = [{"data": b"x" * (vision.MAX_DOCUMENT_BYTES + 1), "mime_type": "application/pdf"}]

    _run(backend.ask("what does this say?", context={"cwd": str(tmp_path / "ws"), "documents": documents}))

    sent_entry = transport.sent_history[-1]
    assert "could not be processed" in sent_entry["content"]
    assert sent_entry["_documents"] is None


def test_both_an_image_and_a_document_note_can_appear_together(temp_db, tmp_path):
    transport = _RecordingTransport()
    backend = NativeAgentBackend(transport, model="whatever")

    result = _run(backend.ask("look at these", context={
        "cwd": str(tmp_path / "ws"),
        "images": [{"data": b"real bytes", "mime_type": "image/jpeg"}],
        "documents": [{"data": b"%PDF fake", "mime_type": "application/pdf"}],
    }))

    assert result.text == "ok"
    sent_prompt = transport.sent_history[-1]["content"]
    assert "attached image" in sent_prompt
    assert "attached document" in sent_prompt


def test_no_documents_behaves_exactly_as_before(temp_db, tmp_path):
    transport = _RecordingTransport()
    backend = NativeAgentBackend(transport, model="whatever")

    _run(backend.ask("hi", context={"cwd": str(tmp_path / "ws")}))

    sent_entry = transport.sent_history[-1]
    assert sent_entry["content"] == "hi"
    assert sent_entry["_documents"] is None
