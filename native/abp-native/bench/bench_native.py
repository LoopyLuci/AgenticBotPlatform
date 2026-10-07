"""Before and after, per fast path: the pure-Python implementation against the compiled one.

    python native/abp-native/bench/bench_native.py              # table
    python native/abp-native/bench/bench_native.py --json       # machine-readable
    ABP_NATIVE=0 python native/abp-native/bench/bench_native.py # same numbers, Python both sides

"before" is `abp_native._py` - the implementation ABP had before the profile moved it, which
is also the fallback when the extension is not installed. "after" is the compiled extension,
or the Python one when there is none (the table then says so rather than pretending).

The inputs are the shapes the profile measured: a memory or two, a knowledge chunk, a file
from a share, a transcript block, a whole export.
"""
from __future__ import annotations

import argparse
import json
import os
import random
import sys
import timeit
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent.parent
sys.path.insert(0, str(REPO))

import abp_native  # noqa: E402
import abp_native._py as py  # noqa: E402

WORDS = ("cluster deploy staging production server backup gpu memory log token pipeline agent tool trace export "
         "dashboard session install version checksum entropy vector index chunk summary entity vault sentinel "
         "postgres compose nightly invoices receipts ledger backup restore checksum").split()


def prose(rng: random.Random, words: int) -> str:
    return " ".join(rng.choice(WORDS) for _ in range(words))


def build_cases(seed: int = 20260917) -> dict:
    rng = random.Random(seed)
    memory = " ".join(rng.choice(WORDS) for _ in range(rng.randrange(8, 25)))
    chunk = "\n\n".join(f"## Section {i}\n\n{prose(rng, 400)}" for i in range(8))          # ~13 KB
    document = " ".join(rng.choice(WORDS) for _ in range(2500))                            # ~17 KB
    def noisy(i: int) -> str:
        return rng.choice([
            f"## Deployment step {i}", prose(rng, 120),
            "sk-" + "".join(rng.choice("abcdef0123456789") for _ in range(24)),
            "token: " + "".join(rng.choice("abcdef0123456789") for _ in range(18)),
            "owner@example.test", "10.0.0.12", "+1 555 123 4567", "-----BEGIN RSA " + "PRIVATE KEY-----",
        ])
    export_block = " ".join(noisy(i) for i in range(120))
    stored = [(("approved" if i % 7 else "pending"), prose(rng, 14) + f" record {i}") for i in range(400)]
    env = {f"VAR_{i}": prose(rng, 2) for i in range(60)}
    env.update({"DASHBOARD_TOKEN": "a-real-looking-dashboard-token", "MY_API_KEY": "another-real-looking-key",
                "PATH": "not-a-secret", "HOME": "not-a-secret-either"})
    secrets = [(k, v) for k, v in env.items() if k in ("DASHBOARD_TOKEN", "MY_API_KEY")]
    return {"memory": memory, "chunk": chunk, "document": document, "export_block": export_block,
            "stored": stored, "environ": env, "secrets": secrets,
            "new_memory": prose(rng, 14) + " and a few more words about the cluster"}


CASES = [
    # (label, python callable, native callable, arguments, repeats)
    ("hash_vectors: a memory", lambda: py.hash_vectors([C["memory"]]), lambda: abp_native.hash_vectors([C["memory"]]), None),
    ("hash_vectors: a knowledge chunk (13 KB)", lambda: py.hash_vectors([C["chunk"]]), lambda: abp_native.hash_vectors([C["chunk"]]), None),
    ("hash_vectors: a file from a share (17 KB)", lambda: py.hash_vectors([C["document"]]), lambda: abp_native.hash_vectors([C["document"]]), None),
    ("memory_duplicate: 400 stored memories", lambda: py.memory_duplicate(C["new_memory"], C["stored"], 0.85),
     lambda: abp_native.memory_duplicate(C["new_memory"], C["stored"], 0.85), None),
    ("is_secret_name: 60 environment variables", lambda: [py.is_secret_name(n) for n in C["environ"]],
     lambda: [abp_native.is_secret_name(n) for n in C["environ"]], None),
    ("known_secrets: the filter over 60 variables",
     lambda: {k: v for k, v in C["environ"].items() if py.is_secret_name(k) and len(v) >= 8},
     lambda: {k: v for k, v in C["environ"].items() if abp_native.is_secret_name(k) and len(v) >= 8}, None),

    ("scrub_credentials: a transcript block", lambda: py.scrub_credentials(C["export_block"]),
     lambda: abp_native.scrub_credentials(C["export_block"]), None),
    ("scrub_pii: a transcript block", lambda: py.scrub_pii(C["export_block"]),
     lambda: abp_native.scrub_pii(C["export_block"]), None),
    ("all of them, once per export block", lambda: [py.normalise_words(C["memory"]), py.hash_vectors([C["memory"]]),
                                                   py.memory_duplicate(C["new_memory"], C["stored"], 0.85),
                                                   [py.is_secret_name(n) for n in C["environ"]],
                                                   py.scrub_credentials(C["export_block"]), py.scrub_pii(C["export_block"])],
     lambda: [abp_native.normalise_words(C["memory"]), abp_native.hash_vectors([C["memory"]]),
              abp_native.memory_duplicate(C["new_memory"], C["stored"], 0.85),
              [abp_native.is_secret_name(n) for n in C["environ"]],
              abp_native.scrub_credentials(C["export_block"]), abp_native.scrub_pii(C["export_block"])], None),
]


def measure(fn, repeats: int = 9) -> float:
    """The best of `repeats` runs, in milliseconds: the least noise-contaminated answer."""
    return min(timeit.repeat(fn, number=1, repeat=repeats)) * 1000


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args(argv)

    global C
    C = build_cases()

    rows = []
    for label, before_fn, after_fn, _ in CASES:
        before = measure(before_fn)
        after = measure(after_fn)
        rows.append({"case": label, "python_ms": round(before, 3), "native_ms": round(after, 3),
                     "speedup": round(before / after, 2) if after else None})
        if before_fn() != after_fn():                      # the parity tests say this cannot happen
            print(f"WARNING: {label}: the two implementations disagree", file=sys.stderr)

    if args.json:
        print(json.dumps({"implementation": abp_native.active(), "rows": rows}, indent=2))
        return 0

    which = "Rust" if abp_native.rust else "Python (the extension is not installed: both columns are Python)"
    print(f"abp_native: after = {which}\n")
    print(f"{'case':44s} {'before ms':>10} {'after ms':>10} {'speedup':>9}")
    for row in rows:
        print(f"{row['case']:44s} {row['python_ms']:10.3f} {row['native_ms']:10.3f} {row['speedup']:8.2f}x")
    return 0


if __name__ == "__main__":
    os.environ.setdefault("PYTHONHASHSEED", "0")
    sys.exit(main())
