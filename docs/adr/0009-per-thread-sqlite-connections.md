# ADR-0009: Per-thread SQLite connections; blocking handlers run off the event loop

**Status:** Accepted — supersedes [ADR-0003](0003-single-sqlite-connection.md)
**Date:** 2026-09-27

## Context

ADR-0003 kept one process-wide `sqlite3.Connection` behind `db._lock`. It
also claimed that WAL mode means "reads never block on a write in
progress". With only one connection that is not true: WAL isolates
*connections* from each other, and one connection is one serialized
stream of statements.

The larger problem was where those statements ran. The dashboard called
`db.*` directly from about 190 `async def` route handlers, on the event
loop thread. Every query, and every wait of up to `busy_timeout` (5 s)
while another process (the MCP server, the CI/CD recorder) held the
write lock, froze all HTTP routes, WebSockets, platform adapters and
schedulers at once.

## Decision

- `bot/db.get_conn()` returns the **primary** connection on the thread that
  opened it (the event loop), and a **per-thread** connection to the same
  file on any other thread. Replacing the primary (tests pointing
  `DB_PATH` elsewhere, a snapshot restore, a sentinel repair) invalidates
  every thread connection through a generation counter.
  `close_conn()` closes all of them.
- In-process writes still serialize behind `db._lock`, exactly as before.
  Cross-connection contention is handled by SQLite's own locking plus
  `busy_timeout`.
- Dashboard route handlers that never `await` are plain `def`, so FastAPI
  runs them in its threadpool. Background work they trigger (WebSocket
  broadcasts, push notifications) reaches the loop through
  `bot.tasks.spawn` / `spawn_soon`, which work from any thread.

## Consequences

The event loop no longer blocks on disk or lock waits for those routes.
Each worker thread holds one extra open connection for its lifetime,
which is cheap (the threadpool is bounded).

A write that leaves an implicit transaction open without committing
would now hold SQLite's write lock on its own connection instead of
sharing it. A scan when this change was made found no such function in
`bot/db.py`, and every writer commits before returning. Keep it that way.

The Postgres seam that ADR-0003 described as the next step is still the
right next step if write volume ever needs it.
