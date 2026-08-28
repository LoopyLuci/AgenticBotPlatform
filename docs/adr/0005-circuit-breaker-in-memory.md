# ADR-0005: The per-instance circuit breaker's state is in-memory, not persisted

**Status:** Accepted
**Date:** 2026-08-28

## Context

`bot/router.py`'s circuit breaker tracks consecutive failures per bot
instance and trips after 5 in a row, to stop a crash-looping backend from
being retried forever (see the changelog entry the same day for the
incident class this addresses). The state — a failure count and an
"opened at" timestamp — needs to live somewhere.

## Decision

Kept as a plain `dict[int, _CircuitState]` on the `Router` singleton,
never written to the database. The same pattern the Support Bot's
confirm-token store (`SupportBot._pending`) already uses for
short-lived, non-critical state.

## Consequences

A server restart silently resets every breaker to closed — which is the
*correct* behavior here, not a gap: a restart is itself a fresh start
worth giving a real backend another chance, and there is no value in a
breaker that stays open across a restart the operator explicitly chose
to perform. The cost is that breaker state isn't visible to, or
survivable across, a multi-process deployment — irrelevant today (one
process per install) and worth revisiting only if ADR-0003's
single-process assumption is ever revisited too.
