"""bot/agent_runtime/vision.py's PDF/document support (Phase C of the
Claude API/Claude Code parity plan) — prepare_documents() mirrors
prepare()'s validation/size-cap contract exactly, for PDF/text/markdown
instead of images.
"""
from __future__ import annotations

import base64

from bot.agent_runtime import vision


def test_no_documents_returns_empty():
    valid, dropped = vision.prepare_documents(None)
    assert valid == []
    assert dropped == 0


def test_a_valid_pdf_is_base64_encoded():
    data = b"%PDF-1.4 fake pdf bytes"
    valid, dropped = vision.prepare_documents([{"data": data, "mime_type": "application/pdf"}])
    assert dropped == 0
    assert len(valid) == 1
    assert valid[0]["mime_type"] == "application/pdf"
    assert base64.b64decode(valid[0]["data_b64"]) == data


def test_text_and_markdown_are_also_supported():
    valid, dropped = vision.prepare_documents([
        {"data": b"plain text", "mime_type": "text/plain"},
        {"data": b"# markdown", "mime_type": "text/markdown"},
    ])
    assert dropped == 0
    assert len(valid) == 2


def test_unsupported_mime_type_is_dropped():
    valid, dropped = vision.prepare_documents([{"data": b"whatever", "mime_type": "application/zip"}])
    assert valid == []
    assert dropped == 1


def test_oversized_document_is_dropped():
    oversized = b"x" * (vision.MAX_DOCUMENT_BYTES + 1)
    valid, dropped = vision.prepare_documents([{"data": oversized, "mime_type": "application/pdf"}])
    assert valid == []
    assert dropped == 1


def test_dropped_note_uses_the_document_kind_word():
    note = vision.dropped_note(1, kind="document")
    assert "document" in note
    assert "image" not in note


def test_dropped_note_defaults_to_image_kind_for_backward_compat():
    note = vision.dropped_note(1)
    assert "image" in note
