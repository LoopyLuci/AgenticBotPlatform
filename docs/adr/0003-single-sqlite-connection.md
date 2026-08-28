# ADR-0003: One global SQLite connection with an app-level lock, not a pool

**Status:** Accepted
**Date:** 2026-08-21 (present since the first public release)

## Context

BotServer runs as a single Python process per install. Every write —
jobs, telemetry, chat history, config changes — needs to go somewhere,
and SQLite (`check_same_thread=False`, WAL mode) was chosen for zero
setup cost (no separate database server to install, configure, or back
up separately from the app itself).

## Decision

One global `sqlite3.Connection`, guarded by a single `threading.Lock`
(`db._lock`) around every write. No connection pool, no SQLAlchemy, no
ORM — plain SQL through a thin set of functions in `bot/db.py`.

## Consequences

This is simple, has no connection-lifecycle bugs to debug, and WAL mode
means reads never block on a write in progress. The real cost is a
single-writer ceiling: every write serializes through one lock, which is
invisible at today's traffic (one person, a handful of bot instances) but
would become the bottleneck if this ever needed to handle genuinely high
write volume across many concurrent bot instances.

If that day comes, the fix is not "add SQLite connection pooling"
(SQLite fundamentally doesn't parallelize writes, pool or not) — it's
either batching high-frequency writes (telemetry, connection logs) to
reduce lock acquisitions, or a storage-engine seam that lets a future
Postgres backend take over write-heavy tables while SQLite stays for
everything else. Neither has been needed yet, so neither has been built;
this ADR exists so that a future contributor immediately understands
*why* the current design is fine today rather than re-deriving it under
pressure during an actual scaling incident.
