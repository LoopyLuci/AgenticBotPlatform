"""ABP's native fast paths: the CPU-bound pure-Python work the profile found, in Rust.

    native/abp-native/          the crate (PyO3 + maturin); `python native/abp-native/build.py
                                --venv <python.exe>` builds and installs it
    abp_native/_py.py           the pure-Python implementations, unchanged in behaviour
    this module                 the one import every caller makes

Each function below is the Rust implementation when the compiled extension is installed and
the pure-Python one otherwise, so ABP works with or without it - a checkout, a source
install and a wheel behave the same, and there is nothing to switch on. `ABP_NATIVE=0`
turns the extension off (read once, at import, like every other ABP on/off switch) so the
Python path can be exercised on its own; `python -m abp_native` prints which one is live.

The functions are deliberately narrow: no clock, no filesystem, no environment, no network.
That is what the profile said they were (pure CPU string and number work), and it is what
`tests/test_native_parity.py` needs to hold Rust and Python to identical results.
"""
from __future__ import annotations

import logging
import os

from abp_native import _py

log = logging.getLogger("abp_native")

VECTOR_DIM = _py.VECTOR_DIM


def _disabled() -> bool:
    return os.environ.get("ABP_NATIVE", "").strip().lower() in ("0", "off", "false", "no")


def _load():
    """The compiled extension, or None - with one line saying which, in the ABP log."""
    if _disabled():
        log.info("abp_native: ABP_NATIVE is off, using the Python implementations")
        return None
    try:
        import abp_native_core
    except ImportError:
        log.info("abp_native: %s is not installed, using the Python implementations",
                 "abp_native_core")
        return None
    log.info("abp_native: using %s (Rust %s)", "abp_native_core", abp_native_core.build_info()[0])
    return abp_native_core


core = _load()
rust = core is not None


def active() -> str:
    """Which implementation is live: "rust" or "python"."""
    return "rust" if core is not None else "python"


def describe() -> list[str]:
    """One line per fast path with the implementation behind it (for `python -m abp_native`)."""
    return [f"{name}: {active()}" for name in
            ("hash_vectors", "normalise_words", "memory_duplicate", "is_secret_name",
             "scrub_credentials", "scrub_pii")]


def hash_vectors(texts: list[str]) -> list[bytes]:
    """`VECTOR_DIM` little-endian float32 values per text (a row of the hashed TF-IDF
    matrix): signed feature hashing over words, word pairs and character trigrams, L2
    normalised. What the memory fabric, the knowledge base and the file-server index store."""
    if core is not None:
        return core.hash_vectors(texts)
    return _py.hash_vectors(texts)


def normalise_words(text: str) -> list[str]:
    """`text` as the words a memory is compared on: apostrophes dropped, everything that is
    not alphanumeric a space, lowercased, runs of spaces collapsed."""
    if core is not None:
        return core.normalise_words(text)
    return _py.normalise_words(text)


def memory_duplicate(target: str, rows: list[tuple[str, str]], overlap: float) -> int:
    """The index of the first `(status, content)` row that says what `target` says, else -1.

    A row counts as the same memory when its normalised text is identical, or when the two
    share at least `overlap` of their words and their digit-bearing tokens match exactly -
    which is why "the port is 8080" and "the port is 8081" stay two memories."""
    if core is not None:
        return core.memory_duplicate(target, rows, overlap)
    return _py.memory_duplicate(target, rows, overlap)


def is_secret_name(name: str) -> bool:
    """Does this environment variable's name look like a credential?"""
    if core is not None:
        return core.is_secret_name(name)
    return _py.is_secret_name(name)


def scrub_credentials(text: str) -> str:
    """`text` with anything shaped like a credential replaced, whatever its shape."""
    if core is not None:
        return core.scrub_credentials(text)
    return _py.scrub_credentials(text)


def scrub_pii(text: str) -> str:
    """`text` with e-mail addresses, IPv4 addresses and phone numbers masked."""
    if core is not None:
        return core.scrub_pii(text)
    return _py.scrub_pii(text)
