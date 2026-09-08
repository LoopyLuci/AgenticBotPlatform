"""Image-understanding support for the native agent loop
(bot/backends/native_backend.py) — validates and base64-encodes inbound
image bytes before a ProviderTransport ever sees them, so every
transport's own image-serialization code can assume it's already dealing
with a real, in-range image rather than re-validating itself.

A caller (currently only bot/handlers.py's Telegram photo path) supplies
raw `{"data": bytes, "mime_type": str}` entries via context["images"];
NativeAgentBackend.ask() runs them through prepare() before handing them
to the transport. Anything oversized or an unsupported format is
silently dropped (never an error — a picture failing to attach shouldn't
fail the whole turn), with the drop count surfaced back to the caller so
it can append one honest note to the prompt.
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


def prepare(images: Optional[list[dict[str, Any]]]) -> tuple[list[dict[str, str]], int]:
    """Returns (valid, dropped_count). `valid` entries are
    {"mime_type": str, "data_b64": str} — ready for a transport to embed
    directly, no further validation needed downstream."""
    if not images:
        return [], 0
    valid: list[dict[str, str]] = []
    dropped = 0
    for image in images:
        data = image.get("data")
        mime_type = image.get("mime_type")
        if not isinstance(data, (bytes, bytearray)) or mime_type not in SUPPORTED_MIME_TYPES or len(data) > MAX_IMAGE_BYTES:
            dropped += 1
            continue
        valid.append({"mime_type": mime_type, "data_b64": base64.b64encode(bytes(data)).decode("ascii")})
    return valid, dropped


def dropped_note(dropped_count: int) -> Optional[str]:
    """A one-line, honest note to append to the prompt when one or more
    images couldn't be processed — so the agent (and the human reading
    its reply) knows an image was present but silently skipped, rather
    than the image just vanishing with no trace."""
    if dropped_count <= 0:
        return None
    plural = "s" if dropped_count != 1 else ""
    return f"[Note: {dropped_count} attached image{plural} could not be processed — too large or an unsupported format.]"
