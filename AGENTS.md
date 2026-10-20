# AGENTS.md — working in AgenticBotPlatform

Onboarding for an AI agent (or human) picking up this repo. Read this first, then the local planning docs
(see **State & handover** below). Keep this file public-safe: **no secrets, no machine-specific paths,
no usernames** — a leak guard scans the tree and the pipeline will reject a push that adds any.

## What this is

A platform for running many independent chat bots at once, each on its own messaging channels, each routed
through one of several interchangeable backends (Anthropic API, Claude Code CLI, Claude Desktop automation,
Hermes Agent). Python backend under `bot/`, a Tauri desktop app under `desktop-app/`, a generated TypeScript
SDK under `sdk/`. The current focus is the **Local Factory** (local models + confined agent swarms + coding
tooling) and its safety layer.

## Setup, build, test

```bash
python -m pip install -r requirements.txt -r requirements-dev.txt     # into a venv
python -m pytest -q                                                   # full suite (parallel if xdist present)
python -m ruff check .                                                # lint gate (must be clean)
python -m pytest tests/test_<area>.py -q -p no:cacheprovider          # one area
```

- Run tests and `ruff` before every commit; both are hard gates in CI.
- The Rust app checks (`cargo fmt --check`, `cargo clippy -D warnings`) run only when `desktop-app/` changes.

## Critical rules (do not violate)

1. **`push == deploy`.** A git `pre-push` hook runs `scripts/local_pipeline.py`, which **stops the live
   instance, rebuilds, runs every gate, and redeploys** on success. Never push unless you intend to ship,
   and only with the owner's go. A failed gate aborts the push and restarts the old bundle — live is safe.
   Preferred: `python scripts/local_pipeline.py` then `git push`. A green, deployed run stamps its exact
   commit, so the push uploads at once instead of idling until GitHub drops the SSH connection. If a hooked
   push drops after a green run, just `git push` again (never `--no-verify`). See `docs/cicd/README.md`.
2. **Never run agent swarms unconfined.** Leaf workers must be OS-level confined (deny-by-default FS, no
   network) before they touch a real task; the owner approves first. See `bot/agent_runtime/` (sandbox,
   appcontainer, wsl_vm) and the confinement tests.
3. **Safety tooling is propose-only.** The red/blue stack under `bot/redblue/` only detects and *proposes*
   reversible containment; it never auto-patches code or executes containment live (dry-run by default).
   Keep it that way — durable changes are human-gated.
4. **Public repo stays clean.** No secrets, machine paths, or real usernames in tracked files. Config files
   that hold secrets (`config/*.yaml`) are local/gitignored — never commit or echo their contents. Test
   fixtures use neutral paths (`/home/...`, `/ws/...`), not real ones.
5. **Don't remove a feature, screen, or command** without an explicit owner decision.
6. **Commit worker output before any `git checkout`/baseline compare** — an uncommitted tree gets clobbered.

## CI/CD: the self-healing pipeline

`scripts/local_pipeline.py` is change-aware and self-healing. A test that fails under the heavy load a push
creates is re-run **serially, with the machine settled, up to a few attempts**; passing on any attempt =
flake (reported, not hidden), failing every attempt = real failure that blocks. Diagnostics land in
`logs/local_pipeline/` (startup env snapshot, per-flake telemetry JSON, crash reports). Full detail and the
tuning env vars are in [`docs/cicd/README.md`](docs/cicd/README.md).

> If a push fails on a test that passes when run alone, it's a flake the settle/backoff should absorb — tune
> `ABP_PIPELINE_FLAKY_ATTEMPTS` / `_SETTLE_S` or quiet the machine. **Never weaken a real assertion to get a
> green push.** An environment-coupled test (one that assumes a local server is off) should be made
> backend-agnostic instead.

## Repo map (where things live)

| Path | What |
|---|---|
| `bot/` | Python backend: agent runtime, backends, dashboard, localai, modelmistress, redblue, hotreload |
| `bot/agent_runtime/` | native sandbox + confinement (sandbox, appcontainer, sandbox_ns, wsl_vm), subagents |
| `bot/redblue/` | EscapeArtist/WatchDog safety stack (schemas, verifier, events, detector, watchdog, taxonomy, redteam, containment, journal) — propose-only |
| `bot/hotreload.py` | every new `bot/*` module must be classified here (a completeness test enforces it) |
| `scripts/` | `local_pipeline.py` (CI/CD), `publish_release.py`, `release_guard.py`, `stage_bundle.py` |
| `desktop-app/` | Tauri app (Rust) | 
| `sdk/` | generated TS SDK — regenerate from the OpenAPI, don't hand-edit |
| `docs/` | contributor docs; `docs/cicd/` is the pipeline reference |
| `tests/` | pytest suite; add tests with any change |

## State & handover

The **current live bundle, in-flight work, and next steps are tracked in local-only (gitignored) planning
docs**: `PLAN.md` and `TODO.md` at the repo root (read the top of `TODO.md` first). Sensitive design
material (e.g. the red/blue offensive-capability design) is also local-only and must not be committed.
A handover agent should read those plus the owner's persistent memory index before starting.

## Conventions

- Match the surrounding code's style, naming, and comment density; prefer the dedicated test/lint tools.
- New `bot/` module → classify it in `bot/hotreload.py`.
- API route change → regenerate the SDK and `docs/api/openapi.json` (the pipeline checks they match).
- New route, page, action, module or settings section → `python scripts/gen_capabilities.py` (the capability index
  in `docs/capabilities.*`; `tests/test_capabilities.py` fails until it is current).
- Keep `TODO.md`/`PLAN.md` current as work lands, with the commit.
