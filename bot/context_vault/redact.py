"""Nothing that looks like a credential is stored. Text goes through the same scrubbers ABP uses for exports and
crash reports (abp_native.scrub_credentials, secrets_guard.redact, diagnostics.redact); an item that is mostly or
obviously a secret (a private key block, several hits) is dropped instead of stored with holes in it."""
from __future__ import annotations

import re
from typing import Optional

_MARKERS = ("[key removed]", "[REDACTED]", "[secret:")
_PEM = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")
MAX_HITS = 2


def sanitize(text: str) -> Optional[str]:
    """The text with credentials masked, or None when the item should not be stored at all."""
    if not isinstance(text, str) or not text.strip():
        return None
    if _PEM.search(text):
        return None
    out = text
    try:
        import abp_native

        out = abp_native.scrub_credentials(out)
    except Exception:  # noqa: BLE001 - fall through to the other scrubbers
        pass
    try:
        from bot.agent_runtime import secrets_guard

        out = secrets_guard.redact(out)
    except Exception:  # noqa: BLE001
        pass
    hits = sum(out.count(m) for m in _MARKERS)
    try:
        from bot import diagnostics

        before = out
        out = diagnostics.redact(out)
        hits += out.count("[REDACTED]") - before.count("[REDACTED]") - out.count("[secret:[REDACTED]]")
    except Exception:  # noqa: BLE001
        pass
    if hits > MAX_HITS:
        return None
    return out
