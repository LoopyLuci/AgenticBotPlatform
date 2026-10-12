"""Opening a database: the WAL switch survives a lock and a race on a new file."""
from __future__ import annotations

import sqlite3
import threading

from bot import db as db_module


class _Flaky:
    """A connection whose journal-mode switch answers 'locked' a few times."""

    def __init__(self, fails: int, mode: str = "delete"):
        self.fails, self.mode, self.sets = fails, mode, 0

    def execute(self, sql):
        class _R:
            def __init__(self, v):
                self.v = v

            def fetchone(self):
                return (self.v,)
        if sql.rstrip(";").endswith("journal_mode"):
            return _R(self.mode)
        if self.fails:
            self.fails -= 1
            raise sqlite3.OperationalError("database is locked")
        self.sets += 1
        self.mode = "wal"
        return _R("wal")


def test_wal_switch_retries_a_locked_pragma():
    c = _Flaky(fails=3)
    db_module._enable_wal(c)
    assert c.sets == 1 and c.mode == "wal"


def test_wal_switch_is_skipped_when_already_wal():
    c = _Flaky(fails=99, mode="wal")
    db_module._enable_wal(c)
    assert c.sets == 0


def test_wal_switch_gives_up_after_bounded_attempts():
    c = _Flaky(fails=99)
    try:
        db_module._enable_wal(c, attempts=3)
    except sqlite3.OperationalError:
        return
    raise AssertionError("expected the lock error to surface")


def test_many_threads_opening_a_new_file_at_once_all_succeed(tmp_path):
    errors: list[Exception] = []
    barrier = threading.Barrier(8)

    def go(i):
        try:
            barrier.wait()
            db_module._open(tmp_path / f"race{i % 2}.db").close()
        except Exception as e:  # noqa: BLE001
            errors.append(e)
    threads = [threading.Thread(target=go, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
