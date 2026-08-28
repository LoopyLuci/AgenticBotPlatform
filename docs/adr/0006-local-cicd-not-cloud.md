# ADR-0006: CI/CD runs 100% locally, not on a cloud runner

**Status:** Accepted
**Date:** 2026-08-28

## Context

This project used a GitHub Actions workflow (`.github/workflows/ci.yml`)
for a short time — byte-compiling, running `pytest`, `pip-audit`, and the
Rust `fmt`/`clippy`/`cargo check` checks, plus a Docker image build and a
bare-metal boot smoke test, all on GitHub's own cloud runners, triggered
on every push to `main`. The explicit requirement is that BotServer's
own CI/CD have no dependency on any third-party cloud service — it must
run entirely on the machine that owns the code.

## Decision

Replaced the GitHub Actions workflow with `scripts/local_pipeline.py`,
triggered by a `pre-push` git hook (`scripts/git-hooks/pre-push`,
installed via `scripts/install_git_hooks.sh`/`.ps1`) — the same trigger
point (push to `main`) the retired workflow fired on, just local. It runs
the same checks directly on the developer's machine, and — uniquely,
since there's no separate deployment target the way a cloud pipeline
would have — goes one step further into real CD: on an all-green result
it rebuilds the production app and redeploys it to this same machine
(restarting whichever OS service is registered, or relaunching the plain
executable that was already running).

Getting this working surfaced a real, previously-undiagnosed bug on
Windows: `cargo check`/`clippy`/`build` all re-copy `tauri.conf.json`'s
bundled `.venv` into `target/release/` (see the existing comment on
`terminate_child` in `lib.rs` about the venv's own child-process
behavior), which can never succeed while a previously-built instance is
still running and holding its own copy of those files loaded — not a
flaky, occasional failure but a 100%-reproducible one. `local_pipeline.py`
now stops any running instance before its Rust check step and restores or
redeploys it afterward depending on the outcome, which a cloud runner's
always-fresh, always-isolated environment coincidentally could never have
exposed.

## Consequences

Nothing about verifying or shipping a change here depends on GitHub's
infrastructure, an internet connection reaching it, or its uptime — a
red pipeline or a successful deploy both happen without leaving this
machine. The cost: there's no longer a second, independent environment
(GitHub's ephemeral Ubuntu/Windows runners) exercising the checks —
findings that only a genuinely clean environment surfaces (like the
tauri_build resource-path and flaky-monotonic-clock bugs the old workflow
caught earlier in this project's history) won't be caught this way
anymore. That tradeoff is accepted deliberately as the explicit cost of
requiring zero cloud dependency, not an oversight.
