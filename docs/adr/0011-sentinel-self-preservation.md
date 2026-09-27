# ADR-0011: Sentinel, one self-preservation loop that detects, backs up and repairs

**Status:** Accepted
**Date:** 2026-09-27

## Context

ABP already had many separate protections: the platform supervisor restarts
crashed bots, `bot/diagnostics.py` writes crash reports, retention prunes,
snapshots are available on request, and hot-reload refuses bad config. But
nothing covered the failures that end an install rather than one request:

- a corrupted database, or a disk that fills up;
- a crash loop after a bad config edit or plugin;
- a wedged event loop, where the process is alive but serving nothing;
- a vulnerable dependency that nobody is watching;
- a lost disk, when no backup existed to restore from;
- an app downgrade, after which the database is newer than the code.

It also had no single place that answered "is this install OK?"

## Decision

`bot/sentinel/` is one background loop, started by `bot.main` and configured
under `sentinel:` in `config/backends.yaml`. Every key has a safe default in
code. It covers three kinds of duty:

| | Duty | Module |
|---|---|---|
| detect | DB `quick_check`, disk space, WAL size | `repair.check_database` |
| | OSV.dev scan of installed packages and shipped lockfiles (PyPI, crates.io, npm, Maven) | `cve` |
| | dashboard exposure, token strength, secret-file permissions, secrets in logs, code tampering | `security` |
| | every error fingerprinted; new, regressed and spiking issues | `bug_hunter` |
| | event-loop stalls (with the blocking stack), hangs, memory growth | `watchdog` |
| preserve | verified, rotated, optionally mirrored backups (DB, provider store, vault key, `.env`, config, Android keystore) | `backup` |
| | last-known-good config, saved after every healthy boot | `bootguard` |
| repair | REINDEX, then row salvage, then restore of the newest verified backup | `repair.repair_database` |
| | crash loop: safe mode (config rolled back, plugins off for one boot) | `bootguard` |
| | hang: stacks dumped, exit 75, supervised restart | `watchdog` + `guardian` / desktop app |
| | too-new database after a downgrade: restore a backup this version can read | `bot.main._open_database` |
| | owner-only permissions; redaction of leaked secrets | `security` |
| | upgrade a vulnerable package, smoke-test ABP, roll back on failure (opt-in) | `cve.fix_python` |

Design rules:

- **The journal is not the database.** `data/sentinel/journal.jsonl` must keep
  working when the database is what broke.
- **Never delete evidence.** Damaged databases are quarantined, and files
  replaced by a restore or safe mode are kept beside the originals.
- **Only verified backups are restore candidates.** Every set is integrity-
  checked and hashed when written, and old sets are re-verified to catch bit
  rot.
- **Alerts are de-duplicated.** A persisting condition alerts once per six
  hours, and clears when the condition goes away. Warnings and worse reach
  paired phones.
- **Something outside the loop has to restart a wedged process.** The desktop
  app and `python -m bot.sentinel.guardian` (used by `scripts/run.*` and
  Docker) both restart with backoff and give up on a hopeless crash loop.
  `ABP_SUPERVISED=1` tells the watchdog that exiting is safe.
- **Package upgrades are opt-in** (`sentinel.cve.auto_fix`). Findings always
  alert. Changing the software itself is a choice a person makes, or turns on.

## Consequences

The status is visible in the dashboard's Resilience page and via
`/api/sentinel/*`. The daily backup costs one online copy of the database
(seconds at today's sizes). The CVE scan needs network access to
api.osv.dev; offline, it keeps the last result and retries.

What this does not do: it cannot prevent loss of data newer than the last
backup when salvage fails (lower `backup.every_hours` for more safety). It
also cannot protect against losing the disk unless `backup.mirror_dir` points
somewhere else.
