# Where ABP's CPU goes, and what moved to Rust

py-spy 0.4.2 (`record --native --gil`, 100 Hz) over four workloads, and what the profile said
to do about it. Every number below is a measurement from this repository on a 12-core
desktop CPU (32 GB, Windows, CPython 3.11); both a flamegraph SVG and a speedscope JSON were
kept for each profile, and `native/abp-native/bench/bench_native.py` reproduces the before
and after table in the last section.

Reproducing a profile needs a driver that puts ABP in the state being measured. The drivers
used here are throwaway scripts (seed a throwaway database, then drive one workload), because
the workloads are ABP's own: startup, the dashboard under a scripted load, the background
loops, and the slowest tests.

## The four profiles

### 1. Startup - 3.6 s, and it is all import machinery

Marked wall clock for each step of `bot.main.run()` up to the dashboard being ready:

| step | ms |
| --- | --- |
| `import bot.main` | 139 |
| `_open_database()` (the ~790-line schema script plus the migration probes) | 40 |
| `_log_build_provenance()` (a `git rev-parse` subprocess) | 53 |
| `diagnostics.build_status()` | 38 |
| `plugins.load_enabled()` | 3 |
| `dashboard.server.build_app()` (55 route modules imported and registered) | 2365 |
| Support Bot warm-up (first-use training) | 964 |
| **total** | **3575** |

Samples: 371, 3 710 ms of on-CPU time.

| self time | % of samples | where |
| --- | --- | --- |
| 810 ms | 21.8% | `typing.py` |
| 310 ms | 8.4% | pydantic `_generate_schema.py` |
| 170 ms | 4.6% | `re/_parser.py`, pydantic `fields.py`, `fields.py`'s `_config`, `inspect.py` |
| 160 ms | 4.3% | `warnings.py` |
| 140 ms | 3.8% | `typing_extensions.py`, `dataclasses.py` |
| 110 ms | 3.0% | pydantic `_model_construction.py` |
| 90 ms | 2.4% | `bot/support_bot/nn_model.py` |

**Not Rust-shaped.** Every hot frame is CPython building typing and pydantic objects while
importing, and native `PyType_GenericAlloc`/`PyObject_GC_Del` underneath them. A faster
implementation of ABP's own functions cannot make `import` cheaper; the lever here is
importing fewer modules before the dashboard answers (and `tests/test_fast_startup.py`
already guards the order). The one ABP-shaped item, the `git rev-parse` subprocess, is I/O.

### 2. The dashboard under a scripted load - redaction is the answer

The real `build_app()` behind a `TestClient`, against a throwaway database seeded with 300
memories, 30 knowledge documents, 12 bot instances and 60 agent runs with 54 events each.
Requests only (the seeding is a separate phase, so its own cost does not pollute the
profile): status, overview, bots, chat send, memory search, memory context, sessions, a real
agent turn through `POST /api/chat/send-to-bot` against a scripted transport, and the
trajectory listing the CLI uses.

Samples: 1121, 11 210 ms of on-CPU time.

| self time | % of samples | where |
| --- | --- | --- |
| 1920 ms | 17.1% | `secrets_guard.is_secret_name` |
| 810 ms | 7.2% | psutil `_pswindows.exe` (mostly `ZwQuerySystemInformation`) |
| 780 ms | 7.0% | the `known_secrets` dict comprehension over `os.environ` |
| 440 ms | 3.9% | `typing.py` (FastAPI's per-request validation) |
| 400 ms | 3.6% | `pathlib.py` (`mkdir`/`stat` from the tokenjuice spill dir and the vault) |
| 390 ms | 3.5% | `json/decoder.py` |
| 370 ms | 3.3% | `session_export._clean` |
| 370 ms | 3.3% | starlette `routing.py` |
| 360 ms | 3.2% | `copy.py` (`config.current`'s `deepcopy` on every read) |
| 320 ms | 2.9% | `trajectory.scrub_pii` |
| 270 ms | 2.4% | `memoryfabric/store.py` |

Per module: `secrets_guard.py` **25.6%**, psutil 7.2%, `session_export.py` 3.4%,
`trajectory.py` 2.9%, `memoryfabric/store.py` 2.4%.

Mean latency per call: `/healthz` 2.8 ms, `/api/overview` 4.5 ms, `/api/sessions` 4.7 ms,
chat send 5.3 ms, `/api/bots` 15.8 ms, `/api/memory/search` 238 ms, `/api/memory/context`
249 ms, an agent turn 354 ms, trajectory listing + export 267 ms.

The reason `is_secret_name` is first: `redact()` asks it once per environment variable, and
`redact()` runs once per text block of every export. A 1200-message session is 3600 calls,
so one export is about 200 000 name tests. `trajectory.export` on the seeded database was
453 ms of which that scan is a quarter.

**Worth moving:** `is_secret_name` (25.6% of the profile, pure string matching).
**Not worth moving:** psutil's process-table walk (7.2%, and it is syscalls - `ZwQuerySystemInformation`
alone is 7.1% - a Rust version would make the same calls); `json.loads` and starlette routing
(per-request framework work); `config.current`'s deepcopy (a caching fix, not a port); the
memory endpoints (SQLite reads of 300 rows plus one numpy matmul, with the vectors already
cached).

### 3. The background loops - two pure-Python scans and the same redaction

One pass of the recurring work at the volumes a busy install reaches: the memory fabric's
minute tick (400 memories across 13 vault files), a knowledge-base ingest of a 60-section
document, the Sentinel's 6-hourly log redaction over a 3 MB log tail and its code digest over
every `.py` in `bot/` and `abp_cicd/`, 200 error records through the bug hunter, the file
server's guard and index over 60 documents, 40 context-window measurements, five TokenJuice
compressions of real tool output, a 1200-message export, and 20 duplicate-memory checks
against 400 stored memories.

Samples: 3182, 31 820 ms of on-CPU time.

| self time | % of samples | where |
| --- | --- | --- |
| 3930 ms | 12.3% | `session_export._clean` (eight regexes + `secrets_guard.redact`) |
| 3450 ms | 10.8% | `secrets_guard.is_secret_name` |
| 2340 ms | 7.4% | `sentinel/security.redact_log` |
| 2090 ms | 6.6% | `bot/memory.py`: the per-character normaliser inside `find_duplicate` |
| 1750 ms | 5.5% | `bot/memory.py`: the Jaccard set comprehension |
| 1550 ms | 4.9% | `bot/memory.py`: the digit-token generator (`_numbers`) |
| 1260 ms | 4.0% | the `known_secrets` dict comprehension |
| 980 ms | 3.1% | `fileserver/index.py._features` (the per-word trigram loop) |
| 700 ms | 2.2% | `memoryfabric/knowledge.entities` |
| 330 ms | 1.0% | `trajectory.scrub_pii` |
| 300 ms | 0.9% | `fileserver/index.py.hash_vectors` |

Per module: `bot/memory.py` **24.9%**, `secrets_guard.py` 15.7%, `session_export.py` 12.7%,
`knowledge.py` 9.0%, `sentinel/security.py` 7.5%, `fileserver/index.py` 7.3%,
`trajectory.py` 3.2%.

Per item, one call: `memory.find_duplicate` **29 ms** against 400 stored memories (it
normalises every stored memory per lookup, one character at a time);
`fileserver.index.hash_vectors` **2.0 ms** for a 5 KB document (about 17 000 features built
and hashed in Python); `session_export.render` of 1200 messages **164 ms**;
`trajectory.to_chat(pii=True)` **749 ms**; `security.redact_log` over the log tail 206 ms,
every six hours; `knowledge.entities` 5.6 ms per document; `guard.entropy` 0.22 ms per file
read.

**Worth moving:** `find_duplicate` (24.9%), the credential shapes and PII patterns
(15.9% together), the hashed-TF-IDF features (7.3%).
**Not worth moving:** `security.redact_log` (7.4% of the profile, but it runs every six hours
- 206 ms per 6 h is not a cost anyone feels), `knowledge.entities` (5.6 ms per document, and
the regexes are the readable part of that module), `guard.entropy` (0.22 ms per changed
file, every five minutes).

### 4. The 30 slowest tests - import and YAML, not ABP

`pytest --durations=30` over the memory, knowledge, trajectory, trace, export, TokenJuice,
file-server, context-window and Sentinel tests: 160 passed in 47 s, and the slowest are
dominated by waits rather than work.

| slowest test | s |
| --- | --- |
| `test_the_service_runs_every_schedule_and_keeps_the_server_up` | 6.66 |
| `test_watchdog_names_the_code_that_blocks_the_loop` | 6.21 |
| `test_transfer_remotes_another_abp_webdav_and_a_share` | 3.39 |
| `test_folder_and_notes_sources_sync_into_the_vault_and_the_ledger` | 1.82 |
| `test_rest_api_uploads_resume_and_links` | 1.49 |
| `test_webdav_does_what_clients_need` | 1.47 |
| `test_sentinel_api` | 1.43 |

Samples over the same run: 1874, 18 740 ms of on-CPU time across 39 threads.

| self time | % of samples | where |
| --- | --- | --- |
| 2050 ms | 10.9% | PyYAML `reader.py` |
| 1960 ms | 10.5% | PyYAML `scanner.py` |
| 1160 ms | 6.2% | `typing.py` |
| 1140 ms | 6.1% | `pathlib.py` |
| 810 ms | 4.3% | `dataclasses.py` |
| 1250 ms | 6.6% | pydantic `_generate_schema.py` and `fields.py` |
| 520 ms | 2.8% | `inspect.py` |

**Nothing here is ABP's CPU.** The suite's cost is PyYAML parsing fixture config, module
imports, and the sleeps and subprocesses in the scheduling and watchdog tests. There is no
hot spot in this profile worth a Rust fast path.

## What moved to Rust, and what it bought

`abp_native` (the `abp_native` package at the repository root and the `native/abp-native`
crate) now holds six functions in four places. Every one keeps the Python implementation as
its fallback and as the parity reference; `ABP_NATIVE=0` selects the Python one; the package
says which is live in the ABP log and through `python -m abp_native`.

Bit-for-bit identical results were the requirement, and two of them needed the *reference's*
arithmetic reproduced rather than improved:

* the vectors are `float32`, and `np.linalg.norm(out, axis=1)` is a pairwise sum whose
  accumulation order is part of the answer, so `pairwise_sum` reproduces numpy's `add.reduce`
  (blocks of 128, eight accumulators, and numpy's exact grouping of the final eight - Rust
  would otherwise read that expression left to right and get a different number);
* `str.isalnum()` is Unicode general category Letter or Number, `\w` is that plus the
  underscore, and `str.isdigit()` adds 128 superscript and circled characters - none of which
  is what `char::is_alphanumeric()` returns, so the word, whitespace and digit tests are
  written as `\p{L}`/`\p{N}` classes;
* `trajectory`'s phone pattern has two look-arounds the Rust regex crate does not have, so
  `replace_phone` walks the same decisions Python's backtracking engine walks (take the `+`,
  then 16 run characters, give one back at a time) rather than taking the first span offered.

`tests/test_native_parity.py` proves it: seeded generators over a deliberately wide
alphabet (Latin-1, Cyrillic, Greek, CJK, Hangul, non-ASCII digits, emoji, both apostrophes)
through ten seeds, plus explicit regression cases for each trap above. 109 parity tests, 19
cargo tests, and 9 end-to-end tests through ABP's own modules.

| fast path | moved from | before | after | speedup |
| --- | --- | --- | --- | --- |
| `hash_vectors` (a memory) | `fileserver/index.py` | 0.218 ms | 0.037 ms | 5.9x |
| `hash_vectors` (a 13 KB knowledge chunk) | `fileserver/index.py` | 10.252 ms | 1.623 ms | 6.3x |
| `hash_vectors` (a 17 KB file from a share) | `fileserver/index.py` | 7.334 ms | 1.532 ms | 4.8x |
| `memory_duplicate` (400 stored memories) | `memory.py` | 8.005 ms | 1.079 ms | 7.4x |
| `is_secret_name` (60 environment variables) | `secrets_guard.py` | 0.030 ms | 0.008 ms | 3.8x |
| `scrub_credentials` (a transcript block) | `session_export.py` | 2.539 ms | 0.048 ms | 52.6x |
| `scrub_pii` (a transcript block) | `trajectory.py` | 0.986 ms | 0.124 ms | 7.9x |

End to end, on the same drivers the profiles used:

| path | before | after | speedup |
| --- | --- | --- | --- |
| `fileserver.index.hash_vectors`, 60 documents of ~5 KB | 122.3 ms | 23.4 ms | 5.2x |
| `memory.find_duplicate` x20 against 400 memories | 527.7 ms | 86.6 ms | 6.1x |
| `session_export.render`, a 1200-message session | 183.4 ms | 17.8 ms | 10.3x |
| `trajectory.to_chat(pii=True)`, 1200 messages | 748.7 ms | 275.2 ms | 2.7x |
| `secrets_guard.redact` x400 | 160.7 ms | 95.5 ms | 1.7x |
| trajectory listing + export, 60 runs (dashboard driver) | 453.2 ms | 267.2 ms | 1.7x |
| an agent turn against a scripted transport | 367.3 ms | 353.6 ms | 1.0x |

The embedder in these runs is the always-there hashed TF-IDF one, pinned: on a machine with a
local embedding model `memoryfabric.store.embedder()` answers over HTTP instead, and the
same ingest then costs 18 s of model time in both implementations - which is the point of
having a fallback embedder that needs no model.

## What was left in Python, and why

* **The secret-value replacement in `secrets_guard.redact`.** Measured at parity (1.0x with two
  secrets over a 20 KB block, 1.4x with twenty): `str.replace` is already C, and a handful of
  needles over a few hundred kilobytes is not what costs anything. The cost in that function
  is the name scan, which did move. A version that also removed the known values was written,
  measured, and dropped.
* **`trace.summarize`'s `tool_counts`.** It really is quadratic - a comprehension that rescans
  every tool event once per distinct tool name - but the fix is a `Counter` in Python, and
  the function's own cost is dwarfed by the redaction it feeds.
* **`fileserver.guard.entropy`.** Shannon entropy over a 64 KiB buffer, 0.22 ms per changed
  file every five minutes. Real CPU, no reason to move it.
* **`context_window.measure_chars`.** 0.7 ms per call, at most twice per turn.
* **TokenJuice's compressors.** 1-4 ms per tool result. A Rust version of `_log` and `_search`
  would clear 3x on the function, but the per-turn effect is a few milliseconds and the parity
  surface is five regexes with edge cases; not worth it at this volume.
* **`security.redact_log`.** Seven regexes over the log tail, 206 ms every six hours.
* **Startup and the test suite.** Import machinery, PyYAML, sleeps. See above.
* **Everything I/O.** psutil's process table, SQLite aggregates (`stats.get_overview` runs
  eight queries per call, and the dashboard polls it), the `git rev-parse` at boot, platform
  connections, the embedder's HTTP round trips.

## Keeping it honest

* `cargo fmt --check`, `cargo clippy --all-targets -- -D warnings` and `cargo test` are green
  (19 tests), and `native/abp-native/build.py --check` runs all three.
* `tests/test_native_parity.py` compares Rust against the Python reference on seeded random
  inputs; `tests/test_native_integration.py` checks the fast paths through the modules that
  call them, and that `ABP_NATIVE=0` really does turn the extension off (in a subprocess,
  because the choice is made once at import).
* Every module that changed was tested both ways - memory fabric, memories, knowledge base,
  trajectory and exports, the file server, traces, the agent-eval suite, permissions,
  sandbox, TokenJuice, the context window, the Sentinel, and the new tests: **437 passed**
  with the extension, **328 passed** with `ABP_NATIVE=0` (the 109 parity tests skip, as they
  should: there is no Rust to compare against).
* Nothing here is required: a checkout with no `native/` build works exactly as before, one
  line apart in the log saying the Python implementations are in use.
