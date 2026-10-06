"""abp_native: the Rust fast paths and the pure-Python ones must agree exactly.

Every function that moved from `bot/` into `abp_native/_py.py` and on into Rust has a pair
here: the same seeded inputs go to both implementations and the results must be identical -
the vectors byte for byte, the texts character for character. That is the whole contract:
a caller cannot tell which implementation ran.

hypothesis is not in this project's dependencies, so the inputs come from seeded generators
(`_text`, `_credential`, ...) instead: same cases every run, and a failure is reproducible
from the seed in the assertion message.

Run with ABP_NATIVE=0 and these skip - there is no Rust to compare against.
"""
from __future__ import annotations

import random

import pytest

import abp_native
import abp_native._py as py
import abp_native_core as core

pytestmark = pytest.mark.skipif(not abp_native.rust, reason="the compiled extension is not installed")

SEEDS = (1, 2, 3, 5, 8, 13, 21, 34, 55, 89)

# A deliberately wide alphabet: the texts ABP hashes are documents, code, file paths and
# chat, not ASCII-only prose, and a fast path that only agrees on ASCII is not a fast path.
_ALPHABET = list(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    " \t\n\r.,;:!?()[]{}<>/\\|-_=+*&^%$#@'\"`~"
    "áéíöüñçßÅØЖЩあア漢한글٣٤٥९०१२٣éö"      # Latin-1, Cyrillic, Greek, CJK, Hangul, non-ASCII digits
    "éèêë́﻿😀🚀"
)
_WORDS = [
    "cluster", "deploy", "staging", "Server", "GPU", "RX 7900", "don't", "user's", "42", "3.14",
    "src/module.py", "bot/agent_runtime/trace.py", "ERROR", "warning:", "https://example.test/a?b=1",
    "api_key", "Bearer", "sk-", "AKIA", "-----BEGIN", "PRIVATE KEY-----", "[email]", "[ip]",
    "ada@example.test", "10.0.0.12", "+1 555 123 4567", "192.168.1.254", "007", "١٢٣", "",
]


def _text(rng: random.Random, max_len: int = 400) -> str:
    if rng.random() < 0.35:                       # assembled from realistic fragments
        return " ".join(rng.choice(_WORDS) for _ in range(rng.randrange(0, 30)))
    return "".join(rng.choice(_ALPHABET) for _ in range(rng.randrange(0, max_len)))


def _texts(seed: int, count: int = 60, max_len: int = 400) -> list[str]:
    rng = random.Random(seed)
    return [_text(rng, max_len) for _ in range(count)]


def _pairs(rng: random.Random) -> list[tuple[str, str]]:
    return [(f"NAME_{i}", "".join(rng.choice(_ALPHABET) for _ in range(rng.randrange(0, 20))))
            for i in range(rng.randrange(0, 12))]


# ---- hashed TF-IDF vectors ------------------------------------------------------------------------- #

@pytest.mark.parametrize("seed", SEEDS)
def test_vectors_are_byte_for_byte_the_same(seed):
    texts = _texts(seed)
    assert core.hash_vectors(texts) == py.hash_vectors(texts)


@pytest.mark.parametrize("seed", SEEDS[:4])
def test_vectors_agree_on_documents_the_size_of_a_real_one(seed):
    rng = random.Random(seed)
    texts = [" ".join(rng.choice(_WORDS) for _ in range(rng.randrange(200, 1200))) for _ in range(6)]
    assert core.hash_vectors(texts) == py.hash_vectors(texts)


def test_vectors_of_nothing_and_of_punctuation_are_all_zero():
    for texts in ([], [""], ["", "   ", "...", "\n\t"]):
        assert core.hash_vectors(texts) == py.hash_vectors(texts)


def test_vectors_keep_the_python_row_width():
    assert core.vector_dim() == py.VECTOR_DIM
    rows = core.hash_vectors(["a b c", "d e f"])
    assert [len(row) for row in rows] == [py.VECTOR_DIM * 4] * 2


def test_vectors_still_survive_the_twenty_thousand_word_cap():
    rng = random.Random(99)
    long_text = " ".join(rng.choice(_WORDS) for _ in range(20_050))
    assert core.hash_vectors([long_text]) == py.hash_vectors([long_text])


# ---- memory duplicate detection ---------------------------------------------------------------------- #

@pytest.mark.parametrize("seed", SEEDS)
def test_normalised_words_are_the_same_words(seed):
    rng = random.Random(seed)
    for _ in range(80):
        text = _text(rng, 120)
        assert core.normalise_words(text) == py.normalise_words(text)


@pytest.mark.parametrize("seed", SEEDS)
def test_the_same_memory_is_found_in_both_implementations(seed):
    rng = random.Random(seed)
    for _ in range(120):
        target = _text(rng, 60)
        rows = [(rng.choice(["pending", "approved", "rejected"]), _text(rng, 60)) for _ in range(rng.randrange(0, 8))]
        rows.append(("approved", target))                       # an exact match is always in there
        overlap = rng.choice([0.5, 0.85, 0.95, 1.0])
        assert core.memory_duplicate(target, rows, overlap) == py.memory_duplicate(target, rows, overlap)


@pytest.mark.parametrize("seed", SEEDS[:5])
def test_the_duplicate_check_separates_the_same_facts_with_other_numbers(seed):
    base = "the server on port {} serves {} requests per minute"
    for left, right in ((8080, 8081), (1, 2), (10, 100)):
        target, row = base.format(left, left), base.format(right, right)
        rows = [("approved", row)]
        assert core.memory_duplicate(target, rows, 0.85) == -1
        assert py.memory_duplicate(target, rows, 0.85) == -1
        assert core.memory_duplicate(target, rows, 0.85) == py.memory_duplicate(target, rows, 0.85)


# ---- redaction -------------------------------------------------------------------------------------- #

_NAMES = [
    "DASHBOARD_TOKEN", "MY_api-key", "AUTH_KEY", "auth_key", "PASSWORD", "passwd", "private_key",
    "SESSION_KEY", "signing-key", "access_key", "credential", "secret", "Path", "HOME", "", "PIN",
    "TOKENIZER", "no_secret_here_at_all", "python", "x", "AUTHOR",
]
@pytest.mark.parametrize("name", _NAMES)
def test_secret_names_agree(name):
    assert core.is_secret_name(name) == py.is_secret_name(name)



@pytest.mark.parametrize("seed", SEEDS)
def test_credential_shapes_are_removed_the_same_way(seed):
    rng = random.Random(seed)
    shapes = [
        "sk-abcdefghijklmnopqrstuvwx", "pk-0123456789abcdef0123", "rk-abcdefghij0123456789",
        "ghp_abcdefghijklmnopqrstuvwxyz012345", "gho_" + "a" * 30, "xoxb-1234567890-abcdefghij",
        "xoxp-123456789012-abcdefghijk", "AKIA0123456789ABCDEF", "AIza" + "b" * 33,
        "Authorization: Bearer abcdefghijklmnopqrstuvwx", "Bearer  abcdefghijklmnopqrstuvwxyz0123",
        'api_key: "hunter2hunter2"', "apikey=abcdef123456", "secret = abcdefghi",
        "password: 'letmein123'", 'token:"abcd1234efgh"', "-----BEGIN RSA PRIVATE KEY-----",
        "-----BEGIN PRIVATE KEY-----\nabc\ndef\n-----END PRIVATE KEY-----",
        "no credentials at all here",
    ]
    for _ in range(120):
        text = " ".join(rng.choice(shapes + [_text(rng, 80)]) for _ in range(rng.randrange(1, 6)))
        assert core.scrub_credentials(text) == py.scrub_credentials(text)


@pytest.mark.parametrize("seed", SEEDS)
def test_pii_is_masked_the_same_way(seed):
    rng = random.Random(seed)
    bits = [
        "ada@example.test", "a.b+c%d_e@sub.domain.co.uk", "not-an-email", "@handle", "user@host",
        "10.0.0.12", "192.168.1.254", "1.2.3", "999.999.999.999", "v1.2.3.4", "10.0.0",
        "+1 555 123 4567", "555-123-4567", "(555) 123.4567", "+44 20 7946 0958", "12345",
        "call me on 5551234567 tomorrow", "1e10", "0x1f", "٣٤٥٦٧٨", "١٢٣٤٥٦٧٨٩٠",
    ]
    for _ in range(150):
        text = " ".join(rng.choice(bits + [_text(rng, 60)]) for _ in range(rng.randrange(1, 5)))
        assert core.scrub_pii(text) == py.scrub_pii(text)


def test_scrubbing_nothing_changes_nothing():
    for text in ("", "plain words only", "1234"):
        assert core.scrub_credentials(text) == py.scrub_credentials(text) == text
        assert core.scrub_pii(text) == py.scrub_pii(text) == text


@pytest.mark.parametrize("text", [
    # A phone match that only exists once the run of digits, spaces and dots gives one
    # character back - the first span the engine tries fails its trailing assertion.
    "44 20 7946 25412345",
    "call 555 123 4567 and 12345678",
    "a1b2c3d4e5f6g7h8",
    "++15551234567", "+15551234567", "15551234567", "1 555 123 4567 89",
    "555.123.4567.", ".5551234567.", "id=12345", "x1-2345-67890",
    "١٢٣٤٥٦٧٨٩", "+٩١٢٣٤٥٦٧٨٩",
    "no digits but plenty of words here",
])
def test_the_phone_and_ip_shapes_agree(text):
    assert core.scrub_pii(text) == py.scrub_pii(text)


@pytest.mark.parametrize("text", [
    "password = 'letmein123'", 'token:"abcd1234efgh"', "api_key=abcdef123456",
    "-----BEGIN OPENSSH PRIVATE KEY-----\nb3Blbn\n-----END OPENSSH PRIVATE KEY-----",
    "sk-AAAAAAAAAAAAAAAAAAAA", "pk-bbbbbbbbbbbbbbbbbbbb", "rk-cccccccccccccccccccc",
    "gho_0123456789abcdefghijklmno", "xoxr-123456789012-abcdefghijk",
    "AIzaSy" + "c" * 33, "Authorization: bearer abcdefghijklmnopqrstuvwxyz",
])
def test_the_credential_shapes_agree(text):
    assert core.scrub_credentials(text) == py.scrub_credentials(text)
