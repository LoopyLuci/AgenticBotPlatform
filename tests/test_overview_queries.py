"""The dashboard overview's time filters are index-friendly ranges (not
date(column) = ..., which scanned the whole jobs table on every 5-second poll)
and still count exactly the right rows at the day boundaries."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from bot import db


def _job(status: str, created: datetime, tokens: int = 0, backend: str = "api") -> None:
    db.get_conn().execute(
        "INSERT INTO jobs(action_type, backend, status, created_at, tokens, duration_ms) VALUES ('q', ?, ?, ?, ?, 100)",
        (backend, status, created.isoformat(timespec="seconds"), tokens))
    db.get_conn().commit()


def test_today_counts_respect_the_utc_day_boundary(temp_db):
    now = datetime.now(timezone.utc)
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    _job("success", midnight, tokens=5)                     # first second of today: counted
    _job("success", midnight - timedelta(seconds=1), tokens=100)  # last second of yesterday: not
    _job("failed", now, tokens=7)
    ov = db.get_overview()
    assert ov["completed_today"] == 1 and ov["failed_today"] == 1
    assert ov["tokens_today"] == 12
    assert db.get_jobs_by_backend_today() == {"api": 2}


def test_week_success_rate_and_timeseries(temp_db):
    now = datetime.now(timezone.utc)
    _job("success", now - timedelta(days=2))
    _job("failed", now - timedelta(days=3))
    _job("failed", now - timedelta(days=8))  # outside the 7-day window
    assert db.get_overview()["success_rate_7d"] == 50.0
    _job("success", now - timedelta(hours=1))
    assert sum(b["completed"] for b in db.get_jobs_timeseries_24h()) == 1


def test_the_overview_queries_use_indexes(temp_db):
    conn = db.get_conn()
    start, end = (datetime.now(timezone.utc).date().isoformat(), "9999")
    plan = " ".join(r[3] for r in conn.execute(
        "EXPLAIN QUERY PLAN SELECT COUNT(*) FROM jobs WHERE status='success' AND created_at >= ? AND created_at < ?",
        (start, end)))
    assert "idx_jobs_status_created" in plan and "SCAN jobs" not in plan


def test_schema_v2_adds_the_index_to_an_existing_v1_database(temp_db):
    conn = db.get_conn()
    conn.execute("DROP INDEX idx_jobs_status_created")
    conn.execute("PRAGMA user_version = 1")
    conn.commit()
    db.init_db()
    assert db.schema_version() == 2
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    assert "idx_jobs_status_created" in names
