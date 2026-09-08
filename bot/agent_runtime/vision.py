"""Image and document understanding support for the native agent loop
(bot/backends/native_backend.py) — validates and base64-encodes inbound
image/document bytes before a ProviderTransport ever sees them, so every
transport's own serialization code can assume it's already dealing with
a real, in-range attachment rather than re-validating itself.

A caller (bot/handlers.py's Telegram photo/document paths) supplies raw
`{"data": bytes, "mime_type": str}` entries via context["images"]/
context["documents"]; NativeAgentBackend.ask() runs them through
prepare()/prepare_documents() before handing them to the transport.
Anything oversized or an unsupported format is silently dropped (never
an error — a picture or PDF failing to attach shouldn't fail the whole
turn), with the drop count surfaced back to the caller so it can append
one honest note to the prompt. Documents (PDF/text, via prepare_documents())
are scoped to AnthropicTransport only — see its own supports_documents
flag — unlike images, which every transport this codebase talks to
already serializes.
"""

from __future__ import annotations

import base64
from typing import Any, Optional

# Deliberately smaller than bot/attachments.py's general MAX_ATTACHMENT_BYTES
# (5GB, sized for arbitrary file uploads) — an image is base64-inlined
# directly into the prompt payload (~33% size inflation) rather than
# streamed to disk, and both Anthropic's and OpenAI's real vision APIs
# document much smaller practical limits (single-digit MB) for inline
# image data. 10MB of raw bytes is comfortably inside what either
# provider accepts while still covering any real phone-camera photo.
MAX_IMAGE_BYTES = 10 * 1024 * 1024

# The formats both Anthropic's and OpenAI-compatible vision APIs
# document support for — matches this deployment's actual real usage
# (Telegram photos always arrive as image/jpeg) with headroom for a
# document sent as an image file.
SUPPORTED_MIME_TYPES = frozenset({"image/jpeg", "image/png", "image/gif", "image/webp"})

# Deliberately smaller than the general attachment cap for the same
# reason MAX_IMAGE_BYTES is — Anthropic's real PDF/document limits are
# far smaller than bot/attachments.py's 5GB general file cap, and a
# document is base64-inlined directly into the prompt payload just like
# an image is (no upload-once Files API step — see the Claude API/Claude
# Code parity plan's explicit "why not adopt the Files API" note).
MAX_DOCUMENT_BYTES = 32 * 1024 * 1024

# Anthropic's `document` content block accepts PDFs and plain text/
# markdown — scoped to AnthropicTransport only (see supports_documents
# on ProviderTransport); no other transport this codebase talks to has
# a standardized equivalent.
SUPPORTED_DOCUMENT_MIME_TYPES = frozenset({"application/pdf", "text/plain", "text/markdown"})


def prepare(images: Optional[list[dict[str, Any]]]) -> tuple[list[dict[str, str]], int]:
    """Returns (valid, dropped_count). `valid` entries are
    {"mime_type": str, "data_b64": str} — ready for a transport to embed
    directly, no further validation needed downstream."""
    return _prepare(images, SUPPORTED_MIME_TYPES, MAX_IMAGE_BYTES)


def prepare_documents(documents: Optional[list[dict[str, Any]]]) -> tuple[list[dict[str, str]], int]:
    """Same shape/contract as prepare(), for PDF/text documents instead
    of images — see SUPPORTED_DOCUMENT_MIME_TYPES/MAX_DOCUMENT_BYTES."""
    return _prepare(documents, SUPPORTED_DOCUMENT_MIME_TYPES, MAX_DOCUMENT_BYTES)


def _prepare(items: Optional[list[dict[str, Any]]], supported_mime_types: frozenset, max_bytes: int) -> tuple[list[dict[str, str]], int]:
    if not items:
        return [], 0
    valid: list[dict[str, str]] = []
    dropped = 0
    for item in items:
        data = item.get("data")
        mime_type = item.get("mime_type")
        if not isinstance(data, (bytes, bytearray)) or mime_type not in supported_mime_types or len(data) > max_bytes:
            dropped += 1
            continue
        valid.append({"mime_type": mime_type, "data_b64": base64.b64encode(bytes(data)).decode("ascii")})
    return valid, dropped


def dropped_note(dropped_count: int, kind: str = "image") -> Optional[str]:
    """A one-line, honest note to append to the prompt when one or more
    images/documents couldn't be processed — so the agent (and the human
    reading its reply) knows an attachment was present but silently
    skipped, rather than it just vanishing with no trace."""
    if dropped_count <= 0:
        return None
    plural = "s" if dropped_count != 1 else ""
    return f"[Note: {dropped_count} attached {kind}{plural} could not be processed — too large or an unsupported format.]"
