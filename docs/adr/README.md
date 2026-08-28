# Architecture decision records

A short, dated record of *why* a real decision was made, for when the
reasoning has outlived the memory of whoever made it. Written after the
fact for decisions already in the codebase, and going forward for new
ones — see the template at the bottom.

These are not proposals or discussions; a decision only gets an ADR once
it's actually been made and shipped. If a later decision reverses one,
add a new ADR and mark the old one **Superseded by ADR-00xx** rather than
editing history.

| # | Title | Status |
|---|---|---|
| [0001](0001-pairing-tokens-not-shared-secrets.md) | Server-linking uses short-lived pairing tokens, not shared secrets | Accepted |
| [0002](0002-numpy-over-scikit-learn.md) | Support Bot's neural classifier: NumPy from scratch, not scikit-learn | Accepted |
| [0003](0003-single-sqlite-connection.md) | One global SQLite connection with an app-level lock, not a pool | Accepted |
| [0004](0004-debug-signed-android-release.md) | Android release builds are signed with the debug key, not a Play identity | Accepted |
| [0005](0005-circuit-breaker-in-memory.md) | The per-instance circuit breaker's state is in-memory, not persisted | Accepted |
| [0006](0006-local-cicd-not-cloud.md) | CI/CD runs 100% locally, not on a cloud runner | Accepted |

## Template for a new ADR

```markdown
# ADR-00xx: <short, decision-shaped title>

**Status:** Accepted | Superseded by ADR-00yy
**Date:** YYYY-MM-DD

## Context
What problem forced this decision? What constraints were in play?

## Decision
What was actually decided, stated plainly.

## Consequences
What this makes easier, what it makes harder, and what it deliberately
gives up. Include the alternative(s) considered and why they lost.
```
