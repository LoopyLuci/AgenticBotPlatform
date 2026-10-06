"""The pure-Python implementations of ABP's native fast paths.

These are the reference: what `tests/test_native_parity.py` compares the compiled extension
against on random inputs, and what runs whenever the extension is not installed (or when
ABP_NATIVE says not to). They are the code ABP had before the profile moved it to Rust
(`docs/perf/hotspots.md`), kept in one place so there is exactly one definition of what each
function means.

Everything here is pure: strings and numbers in, strings and numbers out. No clock, no
filesystem, no environment - that is what made these worth moving, and what makes it
possible to hold both implementations to the same results.
"""
from __future__ import annotations

import hashlib
import math
import re

VECTOR_DIM = 1024
MAX_WORDS = 20_000

_WORD = re.compile(r"[\w']+", re.UNICODE)


# ---- hashed TF-IDF vectors (bot/fileserver/index.py) ------------------------------------------------------------- #

def _features(text: str) -> dict[str, float]:
    words = [w.lower() for w in _WORD.findall(text)][:MAX_WORDS]
    feats: dict[str, float] = {}
    for w in words:
        feats["w:" + w] = feats.get("w:" + w, 0) + 1
        padded = f"#{w}#"
        for i in range(len(padded) - 2):
            g = "c:" + padded[i:i + 3]
            feats[g] = feats.get(g, 0) + 0.3
    for a, b in zip(words, words[1:]):
        k = f"b:{a} {b}"
        feats[k] = feats.get(k, 0) + 0.7
    return feats


def hash_vectors(texts: list[str]) -> list[bytes]:
    """`DIM` little-endian float32 values per text: signed feature hashing, L2 normalised."""
    import numpy as np      # only this function needs it; the importers of the rest should not pay for it

    out = np.zeros((len(texts), VECTOR_DIM), dtype=np.float32)
    for i, t in enumerate(texts):
        for f, c in _features(t).items():
            h = int.from_bytes(hashlib.blake2b(f.encode(), digest_size=8).digest(), "little")
            out[i, h % VECTOR_DIM] += (1.0 if (h >> 63) & 1 else -1.0) * (1 + math.log(c))
    out /= np.maximum(np.linalg.norm(out, axis=1, keepdims=True), 1e-9)
    return [out[i].astype(np.float32).tobytes() for i in range(len(texts))]


# ---- memory duplicate detection (bot/memory.py) ------------------------------------------------------------------- #

def _norm(text: str) -> str:
    text = text.replace("'", "").replace("’", "")          # user's and users are the same word
    return " ".join("".join(c.lower() if c.isalnum() else " " for c in text).split())


def normalise_words(text: str) -> list[str]:
    """`_norm(text).split()`."""
    return _norm(text).split()


def _numbers(normalised: str) -> list[str]:
    return [t for t in normalised.split() if any(c.isdigit() for c in t)]


def memory_duplicate(target: str, rows: list[tuple[str, str]], overlap: float) -> int:
    """The index of the first `(status, content)` row that says what `target` says, else -1."""
    normalised = _norm(target)
    if not normalised:
        return -1
    words = set(normalised.split())
    numbers = _numbers(normalised)
    for index, (status, content) in enumerate(rows):
        if status not in ("pending", "approved"):
            continue
        other = _norm(content)
        if other == normalised:
            return index
        other_words = set(other.split())
        shared = len(words & other_words) / max(len(words | other_words), 1)
        if numbers == _numbers(other) and shared >= overlap:
            return index
    return -1


# ---- redaction (bot/agent_runtime/secrets_guard.py, session_export.py, trajectory.py) ------------------------------ #

_SECRET_NAME = re.compile(
    r"(?i)(token|secret|passw(or)?d|passwd|api[_-]?key|private[_-]?key|credential|auth(orization)?[_-]?key|"
    r"access[_-]?key|session[_-]?key|signing[_-]?key)"
)
BORING = re.compile(r"^(true|false|none|null|yes|no|on|off|\d+)$", re.I)
CREDENTIAL_SHAPES = [(re.compile(p), r) for p, r in [
    (r"(?s)-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", "[private key removed]"),
    (r"\b(?:sk|pk|rk)-[A-Za-z0-9_\-]{16,}", "[key removed]"),
    (r"\bgh[pousr]_[A-Za-z0-9]{20,}", "[token removed]"),
    (r"\bxox[abprs]-[A-Za-z0-9\-]{10,}", "[token removed]"),
    (r"\bAKIA[0-9A-Z]{16}\b", "[key removed]"),
    (r"\bAIza[0-9A-Za-z_\-]{30,}", "[key removed]"),
    (r"(?i)\bBearer\s+[A-Za-z0-9._~+/=\-]{16,}", "Bearer [token removed]"),
    (r"(?i)\b(api[_-]?key|secret|token|password|passwd)\b(\\?\"?\s*[:=]\s*\\?\"?)[^\s\\\"',;]{6,}", r"\1\2[removed]"),
]]
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
IPV4 = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
PHONE = re.compile(r"(?<![\w.])\+?\d[\d\s().-]{8,16}\d(?![\w.])")


def is_secret_name(name: str) -> bool:
    return bool(_SECRET_NAME.search(name or ""))


def scrub_credentials(text: str) -> str:
    for rx, repl in CREDENTIAL_SHAPES:
        text = rx.sub(repl, text)
    return text


def scrub_pii(text: str) -> str:
    text = EMAIL.sub("[email]", text)
    text = IPV4.sub("[ip]", text)
    return PHONE.sub("[phone]", text)
