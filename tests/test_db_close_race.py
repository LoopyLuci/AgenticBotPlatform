"""close_conn() runs on one thread while others hold per-thread connections.
Closing a sqlite3 connection another thread is stepping on is an access
violation (process death on Windows), so the close must wait for the statement."""
import threading
import time

from bot import db as db_module


def test_close_conn_never_kills_a_thread_mid_query(temp_db):
    stop = threading.Event()
    errors = []
    iterations = [0]

    def reader():
        while not stop.is_set():
            try:
                conn = db_module.get_conn()
                conn.execute("SELECT 1").fetchall()
                for _ in conn.execute("SELECT name FROM sqlite_master"):
                    pass
                iterations[0] += 1
            except Exception as e:  # a closed handle between calls is fine
                if "closed" not in str(e).lower():
                    errors.append(repr(e))

    t = threading.Thread(target=reader, daemon=True)
    t.start()
    try:
        for _ in range(300):
            db_module.close_conn()
            db_module.get_conn()  # reopen the primary like the next test would
            time.sleep(0.001)
    finally:
        stop.set()
        t.join(10)
    assert not t.is_alive()
    assert not errors
    assert iterations[0] > 0


def test_close_conn_from_a_worker_thread_closes_its_own_connection(temp_db):
    done = []

    def work():
        conn = db_module.get_conn()
        cur = conn.execute("SELECT 1")
        db_module.close_conn()  # closes this thread's own connection too
        try:
            conn.execute("SELECT 1")
        except Exception as e:
            done.append(type(e).__name__)
        done.append(cur)

    t = threading.Thread(target=work, daemon=True)
    t.start()
    t.join(10)
    assert not t.is_alive()
    assert done[0] == "ProgrammingError"
