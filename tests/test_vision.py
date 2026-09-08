"""bot/agent_runtime/vision.py — image validation/base64-prep for the
native agent loop's image-understanding support (Phase D of the
native-BotServer-agents plan)."""
from __future__ import annotations

import base64

from bot.agent_runtime import vision


def test_no_images_returns_empty():
    valid, dropped = vision.prepare(None)
    assert valid == []
    assert dropped == 0
    valid, dropped = vision.prepare([])
    assert valid == []
    assert dropped == 0


def test_a_valid_image_is_base64_encoded():
    data = b"\xff\xd8\xff fake jpeg bytes"
    valid, dropped = vision.prepare([{"data": data, "mime_type": "image/jpeg"}])
    assert dropped == 0
    assert len(valid) == 1
    assert valid[0]["mime_type"] == "image/jpeg"
    assert base64.b64decode(valid[0]["data_b64"]) == data


def test_unsupported_mime_type_is_dropped():
    valid, dropped = vision.prepare([{"data": b"whatever", "mime_type": "application/pdf"}])
    assert valid == []
    assert dropped == 1


def test_oversized_image_is_dropped():
    oversized = b"x" * (vision.MAX_IMAGE_BYTES + 1)
    valid, dropped = vision.prepare([{"data": oversized, "mime_type": "image/png"}])
    assert valid == []
    assert dropped == 1


def test_mixed_batch_keeps_the_valid_ones_and_drops_the_rest():
    good = {"data": b"real image bytes", "mime_type": "image/png"}
    bad_mime = {"data": b"whatever", "mime_type": "text/plain"}
    bad_size = {"data": b"x" * (vision.MAX_IMAGE_BYTES + 1), "mime_type": "image/jpeg"}
    valid, dropped = vision.prepare([good, bad_mime, bad_size])
    assert len(valid) == 1
    assert dropped == 2


def test_dropped_note_is_none_when_nothing_was_dropped():
    assert vision.dropped_note(0) is None


def test_dropped_note_singular_and_plural():
    assert "1 attached image " in vision.dropped_note(1)
    assert "images" in vision.dropped_note(2)
