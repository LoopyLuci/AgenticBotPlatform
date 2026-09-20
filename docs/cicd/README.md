# ABP CI/CD platform — design

> **Status: design, mostly not built.** This page is the agreed plan. Every
> section says what exists today and what does not, so nothing here should be
> read as a description of current behaviour. Settings for the planned features
> are specified in [configuration.md](configuration.md).

## What exists today

| Piece | State |
|---|---|
| Local pipeline (`scripts/local_pipeline.py`): pytest, Rust, Android, Docker checks; change-aware skipping; one-at-a-time lock; flaky re-run; step timeouts | **Built** |
| Transactional release (`scripts/publish_release.py`, `scripts/release_guard.py`): pre-flight, lock healing, journal, rollback, `--resume`, gate before tagging, bundle smoke test, signed updates verified after upload | **Built** |
| Update signing, HTTPS-only pinned download URL | **Built** (`updater.rs`) |
| Diagnostics, crash reports, support bundle, Prometheus counters | **Built** (`bot/diagnostics.py`) |
| Process supervision with backoff, config/code hot reload with a denylist | **Built** |
| Telemetry event store (`abp_cicd`): append-only SQLite, hash chain, allow-listed schema with redaction, export, retention | **Built** (build step 1) |
| Release and pipeline runs recorded as traces (steps, timings, decisions, rollbacks, flaky re-runs) | **Built** (build step 1) |
| Control-plane API `/api/cicd/*` (summary, runs, run detail, explain, step stats, decisions, workers, events, SSE stream, chain check) | **Built** (build step 1) |
| CLI `python -m abp_cicd` (local or over HTTP, `--json` identical to the API) and the parity test between service, API and CLI | **Built** (build step 1) |
| TUI, `ABP_CI-CD_GUI`, ML workers, update policy engine, A/B slots, standby instance | **Not built** — this document |

## Principles

1. **ML is advisory and never in the trust path.** A model may reorder work,
   predict durations, tune timeouts inside bounded ranges, classify failures
   and skip *optional* work. It can never skip a mandatory check, weaken a
   security gate, or approve an update. Deterministic rules sit underneath and
   take over whenever a model is missing, wrong or low-confidence.
2. **Rules first, statistics second, ML third.** Most of the available speed-up
   is deterministic (see *Speed* below); ML is layered on top once data exists.
3. **The data outlives everything.** Models are retrained, code is replaced;
   the event log, in open formats, is the durable asset.
4. **One API, many clients.** The GUI, TUI, CLI, MCP tools and a host's own
   tooling are all clients of the same control-plane API. None has private access.
5. **Measure guarantees, don't assert them.** Recovery time, data-loss window
   and model recall are numbers produced by tests and audit sampling.

No system can honestly promise "any bug or crash" or a 100-year lifespan. The
goal is bounded, detected, automatically-recovered failure, and components that
can be replaced without losing the data.

## Decisions (accepted, not yet shipped)

Recorded here rather than as ADRs, because the ADR convention in
[docs/adr](../adr/README.md) is for decisions that have shipped. Each becomes an
ADR when its feature lands.

| Decision | Choice | Why |
|---|---|---|
| Desktop client | **Native Python GUI**, named `ABP_CI-CD_GUI`, built on **Tkinter/ttk** (standard library) behind a thin drawing interface so the toolkit can be swapped | No install step, no large dependency, nothing to break on a server that only ever needs the TUI. Qt would give richer charts but adds a heavy, separately-licensed dependency; the interface boundary keeps that option open |
| Standby instance | **`warm` for sidecar installs, `off` for desktop installs** | A warm standby doubles resource use, which suits an unattended server and not a laptop. Both are operator-selectable |
| Update and standby policy for embedded installs | **Host-owned and locked by default** when ABP runs as a submodule or sidecar | The host application owns its own upgrade lifecycle; ABP must never change its own version or topology unless the host says so |

## Architecture

```
            ┌──────────────── control-plane API  /api/cicd/*  (OpenAPI, SSE) ────────────────┐
 clients:   │  ABP_CI-CD_GUI (Tkinter)   abp-cicd CLI   TUI (Textual)   MCP tools   host      │
            └───────────────┬───────────────────────────────────────────────────────────────┘
                            │
   pipeline engine ──► event store (SQLite WAL, append-only, versioned schema, JSONL/Parquet export)
   (steps, journal,        ▲                                  ▲
    rollback, resume)      │ heartbeats/decisions             │ metrics/traces (OpenTelemetry-shaped)
                    ML workers (supervised subprocesses, no network, resource-limited)
```

### Telemetry and data

- **Event-sourced, append-only.** Runs, steps, decisions, worker heartbeats,
  updates and failovers are events carrying a `schema_version`.
- **OpenTelemetry-shaped**: a trace per pipeline run, a span per step.
- **Allow-list schema**: a field is only recorded if the schema declares it, so
  a new field cannot leak a secret by accident. Existing redaction stays as a
  second layer. Local-only by default; export is opt-in.
- **Tamper-evident audit log** (each record includes the previous record's hash).
- **Every ML decision is recorded** with its inputs, output, confidence and the
  rule that would otherwise have applied.
- **Readers accept old schema versions indefinitely**; migrations are additive
  and covered by golden fixtures.

### Control-plane API and parity

Routes under `/api/cicd/`: `runs`, `runs/{id}`, `runs/{id}/explain`, `workers`,
`models`, `decisions`, `policy`, `snapshots`, `updates`, plus an SSE stream.
Auth reuses the existing dashboard token and device tiers (read-only, control,
admin). The OpenAPI document is the source of truth: the CLI is generated from
it, and a parity test enumerates every capability and **fails if the GUI or TUI
lacks one**. MCP tools wrap the same API so agents can answer "why did the
pipeline do that?" from data.

### ABP_CI-CD_GUI, CLI and TUI

- **GUI**: native Python, launched as `python -m abp_cicd.gui` (or a desktop
  shortcut). It only talks to the API, so it also works against a remote server.
- **CLI**: `abp-cicd status | runs | run <id> | workers | models | decisions |
  policy | snapshot | update`, with `--json` on every command.
- **TUI**: Textual, same panels, live via SSE.
- **Views (all three)**: pipeline Gantt with critical path; build-time trends
  with anomaly bands; cache hit rate; test-selection savings *and measured
  recall*; flake heatmap; failure classes over time; ML worker board (state,
  model version, confidence distribution, shadow vs champion); decision audit
  log; update and rollout timeline; snapshot gallery.
- **Screenshots and state snapshots** are captured at pipeline milestones (the
  app's own window via the debug port) and stored with the run, next to a state
  snapshot: code version, config, DB schema version, model versions.

## Speed: do this before any ML

| Measured bottleneck | Deterministic fix | ML layer, later |
|---|---|---|
| pytest ≈ 5 min for ~1600 tests | Coverage-based impact analysis (test-to-file map), then `pytest-xdist` after fixing shared ports and temp DB | Rank tests by predicted failure for a diff |
| Repeated `cargo tauri build` | Content-addressed step cache keyed by input hashes; `sccache`; Gradle build cache | Predict which steps a diff invalidates |
| Static timeouts | Set from measured P95/P99 per step | Duration and memory prediction |
| Hand-written lock/network error markers | Keep as fallback | Learned log classifier mapped to healing actions |

## ML workers

Each is a supervised subprocess with a heartbeat and a state machine
(`idle → training → serving → degraded → failed`), no network access and
resource limits.

| Worker | Job | Method | Guard |
|---|---|---|---|
| Failure classifier | Log → error class → healing action | TF-IDF + centroid (same stack as the Support Bot), Drain-style log templating | Low confidence → rule markers decide |
| Flake detector | Tests that fail then pass | Beta-Bernoulli posterior per test | Labels only; never hides a failure |
| Test selector | Order/propose a subset | Coverage map, then gradient-boosted ranking | Advisory; full run at a sampled rate and before every release |
| Duration/resource predictor | ETAs, adaptive timeouts | Quantile regression | Bounded to 0.5×–3× the static value |
| Anomaly detector | Drift in time, size, memory, failure rate | Robust z-score, EWMA, change-point | Alerts only |
| Release risk scorer | Rate a candidate release | Logistic regression / GBDT with contributions | May add checks, never remove one |
| Update health analyst | Judge a canary against a baseline | Statistical comparison | May trigger rollback; cannot approve alone |

**Lifecycle:** models are versioned, hashed, signed and registered with their
training range and metrics. New models run in **shadow mode**, then
champion/challenger, after replay against historical runs. Drift detection
watches live behaviour and a bad model rolls back automatically. Formats are
JSON/ONNX/safetensors — **never pickle**. A single setting turns all ML off and
the pipeline keeps working.

**Audit sampling** is what keeps the models honest: a random percentage of runs
ignore the model and run everything, which measures what it would have missed.

## Updates

Modes, channels, schedules and pinning are specified in
[configuration.md](configuration.md). The mechanism:

1. **A/B slots.** The new version installs into the inactive slot, migrations
   run against a *copy* of the data, a smoke test boots it, and only then is it
   swapped in atomically. The previous slot is kept.
2. **Expand-then-contract migrations**, so the previous version can still read
   the data and rollback is always possible.
3. **Automatic rollback** if the post-update health check fails.
4. **Security**: signature required (as today), HTTPS-only pinned URLs and size
   caps (as today); to add — provenance, an SBOM, dependency hash-pinning
   (`--require-hashes`), reproducible builds.
5. **Host control** for embedded installs: see *Embedded installs* below.

## Failover and self-healing

- **Guardian**: a tiny, dependency-free process owns the public port and
  supervises two instances; it health-probes both, drains connections and swaps
  the upstream atomically. It is deliberately small so it almost never changes.
- **Last-known-good (LKG)** = code version + config + DB snapshot + model
  versions that passed the health SLO for a soak period.
- **Crash loop**: more than *k* restarts in *t* seconds swaps to LKG,
  quarantines the bad version and reports it.
- **State**: SQLite has one writer, so the standby holds a read-only copy kept
  fresh by WAL shipping or the SQLite backup API. On failover it takes a lease
  with a fencing token, which prevents split-brain. **Recovery-point objective is
  seconds, not zero.**
- **Standby level**: `off`, `warm` (started, idle), `hot` (replicating).
- **Atomic writes everywhere** (temp file → fsync → rename), audited across the
  codebase. Config, skills, models and UI assets hot-reload with validation and
  fall back to the last good version. Core code changes go through a blue/green
  swap, not in-process reload (the existing hot-reload denylist exists for a reason).
- **Fault injection** (kill -9 at random points, disk full, corrupt DB, clock
  jump, partition, OOM) runs as a regular test and reports recovery time and
  data-loss window.

## Embedded installs (submodule / sidecar)

When ABP runs with `ABP_HOME` set or `ABP_PROFILE=sidecar`:

- The **host owns update and standby policy** via a policy file the host writes
  (`ABP_POLICY_FILE`). Keys it sets are **locked**: the dashboard, API, CLI and
  agents can read but not change them.
- Without a policy file the sidecar defaults are the safe ones: **no self-update,
  `standby: warm`**. ABP never updates itself inside a host's tree unless the
  host says so.
- The host can drive updates itself: `abp update --check --json` reports what is
  available without changing anything, so the host's own tooling decides.
- Everything is customisable through the same file; see
  [configuration.md](configuration.md) and [../embedding.md](../embedding.md).

## Security

Loopback or a unix socket only; scoped tokens; rate-limited API; signed,
hash-pinned model artifacts in safe formats; ML workers sandboxed with no
network; training data has provenance and no model can lower a mandatory check,
so poisoned data has a small blast radius; tamper-evident audit log.

## Built to be replaced

Narrow versioned interfaces (ports and adapters); API v1 stays additive and
deprecations have dates; open data formats with a plain-text export of
everything; minimal dependencies; tests as the executable specification
(golden fixtures); architecture decision records. Models are disposable; the
rules underneath keep working if a model or a tool disappears.

## Build order

| # | Step | Depends on |
|---|---|---|
| 1 | Event store, instrument `release_guard`/`local_pipeline`, read API, `abp-cicd status` | — |
| 2 | CLI, TUI, `ABP_CI-CD_GUI`, and the parity test | 1 |
| 3 | Deterministic speed-ups: coverage-based selection, `pytest-xdist`, step cache | 1 (to measure) |
| 4 | Update policy engine (modes, schedules, locked host policy), then A/B slots | — |
| 5 | Guardian, LKG snapshots, standby, fault-injection suite | 4 |
| 6 | Statistical ML: durations, anomalies, flakes | 1 with enough runs |
| 7 | Learned models: failure classifier, test selector, risk scorer; shadow mode + audit sampling first | 6 |

Steps 1–5 deliver most of the value with no ML. Steps 6–7 can only be evaluated
honestly once step 1 has been producing data.
