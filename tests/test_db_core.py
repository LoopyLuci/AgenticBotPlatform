"""Core bot.db CRUD, against a real throwaway SQLite file (see conftest's
temp_db fixture) — never the live data/bot.db. Covers the paths this
project's real incidents have actually touched: scheduled-command
cascade-delete on instance removal, the retention pruning boundary
conditions, and audit-log-derived vacuum bookkeeping.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from bot import db


def _insert_instance(conn, name="test-bot", platform="telegram", backend="cli"):
    now = db._now()
    cur = conn.execute(
        "INSERT INTO bot_instances (name, platform, backend, enabled, credentials, created_at, updated_at) "
        "VALUES (?, ?, ?, 1, '{}', ?, ?)",
        (name, platform, backend, now, now),
    )
    conn.commit()
    return cur.lastrowid


def _insert_scheduled_command(conn, instance_id, chat_id="123"):
    now = db._now()
    conn.execute(
        "INSERT INTO scheduled_commands (instance_id, chat_id, kind, prompt, interval_s, next_run_at, enabled, created_at) "
        "VALUES (?, ?, 'heartbeat', 'ping', 300, ?, 1, ?)",
        (instance_id, chat_id, now, now),
    )
    conn.commit()


class TestScheduledCommandCascade:
    def test_delete_instance_removes_its_scheduled_commands(self, temp_db):
        conn = temp_db
        instance_id = _insert_instance(conn)
        _insert_scheduled_command(conn, instance_id)
        assert conn.execute("SELECT COUNT(*) c FROM scheduled_commands WHERE instance_id=?", (instance_id,)).fetchone()["c"] == 1

        from bot import bot_instances
        bot_instances.delete_instance(instance_id, actor="test")

        assert conn.execute("SELECT COUNT(*) c FROM scheduled_commands WHERE instance_id=?", (instance_id,)).fetchone()["c"] == 0

    def test_scheduled_commands_for_a_different_instance_are_untouched(self, temp_db):
        conn = temp_db
        keep_id = _insert_instance(conn, name="keep-me")
        delete_id = _insert_instance(conn, name="delete-me")
        _insert_scheduled_command(conn, keep_id, chat_id="1")
        _insert_scheduled_command(conn, delete_id, chat_id="2")

        from bot import bot_instances
        bot_instances.delete_instance(delete_id, actor="test")

        assert conn.execute("SELECT COUNT(*) c FROM scheduled_commands WHERE instance_id=?", (keep_id,)).fetchone()["c"] == 1


class TestPruneOldData:
    def test_prune_removes_only_rows_older_than_the_cutoff(self, temp_db):
        conn = temp_db
        old = (datetime.now(timezone.utc) - timedelta(days=200)).isoformat(timespec="seconds")
        recent = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat(timespec="seconds")
        conn.execute("INSERT INTO telemetry_events (component, metric, ts) VALUES ('x', 'old', ?)", (old,))
        conn.execute("INSERT INTO telemetry_events (component, metric, ts) VALUES ('x', 'recent', ?)", (recent,))
        conn.commit()

        removed = db.prune_old_data(days=90)

        assert removed["telemetry_events"] == 1
        remaining = [r["metric"] for r in conn.execute("SELECT metric FROM telemetry_events")]
        assert remaining == ["recent"]

    def test_prune_never_deletes_an_unfinished_job_regardless_of_age(self, temp_db):
        # This is the exact edge case that would make automatic pruning
        # dangerous: an old-but-still-running job must survive.
        conn = temp_db
        old = (datetime.now(timezone.utc) - timedelta(days=200)).isoformat(timespec="seconds")
        conn.execute(
            "INSERT INTO jobs (action_type, backend, status, created_at) VALUES ('ask', 'cli', 'running', ?)",
            (old,),
        )
        conn.execute(
            "INSERT INTO jobs (action_type, backend, status, created_at) VALUES ('ask', 'cli', 'success', ?)",
            (old,),
        )
        conn.commit()

        removed = db.prune_old_data(days=90)

        assert removed["jobs"] == 1
        statuses = [r["status"] for r in conn.execute("SELECT status FROM jobs")]
        assert statuses == ["running"]


class TestVacuumBookkeeping:
    def test_days_since_last_vacuum_is_none_before_any_vacuum(self, temp_db):
        assert db.days_since_last_vacuum() is None

    def test_days_since_last_vacuum_reads_back_near_zero_right_after(self, temp_db):
        db.vacuum(actor="test", detail="unit test")
        since = db.days_since_last_vacuum()
        assert since is not None
        assert since < 0.01
