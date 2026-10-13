"""bot.storage_tiering against real files in pytest tmp dirs (state root and archive are both tmp)."""
from __future__ import annotations

import os
import time

import pytest

from bot import storage_tiering as st

NOW = time.time()
DAY = 86400


@pytest.fixture
def env(tmp_path):
    root = tmp_path / "state"
    (root / "data" / "attachments").mkdir(parents=True)
    (root / "logs").mkdir()
    arch = tmp_path / "archive"
    cfg = {"enabled": True, "archive_dir": str(arch)}
    return root, arch, cfg


def _file(path, data=b"hello", age_days=200):
    path.write_bytes(data)
    t = NOW - age_days * DAY
    os.utime(path, (t, t))
    return path


def test_refuses_without_archive_dir(env):
    root, _, cfg = env
    cfg["archive_dir"] = ""
    for fn in (st.scan, st.apply):
        with pytest.raises(st.TieringError, match="not set"):
            fn(root, cfg, NOW)


def test_refuses_when_disabled(env):
    root, _, cfg = env
    cfg["enabled"] = False
    with pytest.raises(st.TieringError, match="off"):
        st.apply(root, cfg, NOW)


def test_refuses_archive_inside_data_dir(env):
    root, _, cfg = env
    cfg["archive_dir"] = str(root / "data" / "archive")
    with pytest.raises(st.TieringError, match="outside"):
        st.apply(root, cfg, NOW)


def test_refuses_unwritable_archive(env, tmp_path):
    root, _, cfg = env
    blocker = tmp_path / "afile"
    blocker.write_text("x")
    cfg["archive_dir"] = str(blocker / "sub")
    with pytest.raises(st.TieringError, match="writable"):
        st.apply(root, cfg, NOW)


def test_dry_run_moves_nothing(env):
    root, arch, cfg = env
    f = _file(root / "data" / "attachments" / "a_old.png")
    res = st.scan(root, cfg, NOW)
    assert [c["path"] for c in res["candidates"]] == ["data/attachments/a_old.png"]
    assert f.exists() and not list(arch.rglob("a_old.png"))
    assert not (root / "data" / st.INDEX_NAME).exists()


def test_apply_moves_only_old_enough_files_of_enabled_categories(env):
    root, arch, cfg = env
    cfg["categories"] = {"logs": {"enabled": False}}
    old = _file(root / "data" / "attachments" / "a_old.png")
    new = _file(root / "data" / "attachments" / "a_new.png", age_days=1)
    log = _file(root / "logs" / "bot.log.1")
    res = st.apply(root, cfg, NOW)
    assert [m["path"] for m in res["moved"]] == ["data/attachments/a_old.png"]
    assert not old.exists() and new.exists() and log.exists()
    assert (arch / "attachments" / "a_old.png").read_bytes() == b"hello"
    stat = st.status(root, cfg)
    assert stat["archived"]["files"] == 1 and stat["by_category"]["attachments"]["files"] == 1


def test_never_touches_databases_config_secrets_live_log_or_subfolders(env):
    root, arch, cfg = env
    att = root / "data" / "attachments"
    keep = [_file(att / "x.db"), _file(att / "x.db-wal"), _file(att / "settings.yaml"), _file(att / ".env"),
            _file(att / "id.key"), _file(root / "logs" / "bot.log"), _file(root / "data" / "bot.db")]
    (att / "thumbs").mkdir()
    keep.append(_file(att / "thumbs" / "t.png"))
    (att / "_upload_sessions").mkdir()
    keep.append(_file(att / "_upload_sessions" / "00000000"))
    res = st.apply(root, cfg, NOW)
    assert res["moved"] == []
    assert all(p.exists() for p in keep)


def _die(_p):
    raise KeyboardInterrupt  # the process dies after the index row is written, before the hot copy goes


def test_crash_between_copy_and_delete_is_finished_next_run(env, monkeypatch):
    root, arch, cfg = env
    f = _file(root / "data" / "attachments" / "a_old.png")
    monkeypatch.setattr(st, "_remove_hot", _die)
    with pytest.raises(KeyboardInterrupt):
        st.apply(root, cfg, NOW)
    assert f.exists() and st.status(root, cfg)["pending"] == 1
    monkeypatch.undo()
    res = st.apply(root, cfg, NOW)
    assert res["recovered"] == [{"path": "data/attachments/a_old.png", "action": "finished"}]
    assert not f.exists() and st.status(root, cfg)["archived"]["files"] == 1


def test_crash_recovery_rolls_back_when_hot_file_changed(env, monkeypatch):
    root, arch, cfg = env
    f = _file(root / "data" / "attachments" / "a_old.png")
    monkeypatch.setattr(st, "_remove_hot", _die)
    with pytest.raises(KeyboardInterrupt):
        st.apply(root, cfg, NOW)
    monkeypatch.undo()
    f.write_bytes(b"edited after the copy")
    done = st.recover(root, cfg)
    assert done[0]["action"] == "rolled-back"
    assert f.read_bytes() == b"edited after the copy" and st.status(root, cfg)["archived"]["files"] == 0
    assert not (arch / "attachments" / "a_old.png").exists()


def test_hash_mismatch_aborts_that_file_only(env, monkeypatch):
    root, arch, cfg = env
    bad = _file(root / "data" / "attachments" / "a_bad.png", b"AAAA", age_days=300)
    good = _file(root / "data" / "attachments" / "b_good.png", b"BBBB", age_days=200)
    real = st._copy_file

    def corrupt(src, dst):
        real(src, dst)
        if src.name == "a_bad.png":
            dst.write_bytes(b"AAAX")

    monkeypatch.setattr(st, "_copy_file", corrupt)
    res = st.apply(root, cfg, NOW)
    assert bad.exists() and not good.exists()
    assert [s["path"] for s in res["skipped"]] == ["data/attachments/a_bad.png"]
    assert not list(arch.rglob("a_bad.png*"))
    assert st.status(root, cfg)["archived"]["files"] == 1


def test_recall_restores_and_verifies(env):
    root, arch, cfg = env
    f = _file(root / "data" / "attachments" / "a_old.png", b"payload")
    st.apply(root, cfg, NOW)
    assert not f.exists()
    out = st.recall("data/attachments/a_old.png", root, cfg)
    assert out["size"] == 7 and f.read_bytes() == b"payload"
    assert st.status(root, cfg)["archived"]["files"] == 0
    # a damaged archive copy is refused and nothing is restored
    os.utime(f, (NOW - 200 * DAY, NOW - 200 * DAY))
    st.apply(root, cfg, NOW)
    (arch / "attachments" / "a_old.png").write_bytes(b"damaged")
    with pytest.raises(st.TieringError, match="verification"):
        st.recall("data/attachments/a_old.png", root, cfg)
    assert not f.exists()


def test_ensure_hot_recalls_an_archived_file(env):
    root, arch, cfg = env
    f = _file(root / "data" / "attachments" / "a_old.png", b"payload")
    st.apply(root, cfg, NOW)
    assert st.is_archived(f, root)
    assert st.ensure_hot(f, root, cfg) is True and f.read_bytes() == b"payload"
    assert st.ensure_hot(root / "data" / "attachments" / "never.png", root, cfg) is False


def test_locked_file_is_skipped(env, monkeypatch):
    root, arch, cfg = env
    locked = _file(root / "data" / "attachments" / "a_locked.png")
    free = _file(root / "data" / "attachments" / "b_free.png", age_days=100)
    monkeypatch.setattr(st, "_is_locked", lambda p: p.name == "a_locked.png")
    res = st.apply(root, cfg, NOW)
    assert locked.exists() and not free.exists()
    assert res["skipped"] == [{"path": "data/attachments/a_locked.png", "reason": "in use"}]


@pytest.mark.skipif(os.name != "nt", reason="an open handle blocks a rename only on Windows")
def test_really_open_file_is_skipped_on_windows(env):
    root, arch, cfg = env
    f = _file(root / "data" / "attachments" / "a_open.png")
    with open(f, "rb"):
        res = st.apply(root, cfg, NOW)
    assert f.exists() and res["moved"] == [] and res["skipped"][0]["reason"] == "in use"


def test_budget_respected_oldest_first(env):
    root, arch, cfg = env
    for i in range(5):
        _file(root / "data" / "attachments" / f"f{i}.bin", b"x" * 10, age_days=100 + i)
    cfg["max_files"] = 2
    res = st.apply(root, cfg, NOW)
    assert [m["path"] for m in res["moved"]] == ["data/attachments/f4.bin", "data/attachments/f3.bin"]
    cfg["max_files"] = 50
    cfg["max_bytes"] = 25
    assert st.scan(root, cfg, NOW)["files"] == 2  # 3 left at 10 bytes each: two fit under 25


def test_cli_refuses_when_off_and_reports_status(env, monkeypatch):
    root, arch, cfg = env
    real = st.settings
    monkeypatch.setattr(st, "_state_root", lambda r=None: root)
    monkeypatch.setattr(st, "settings", lambda c=None: real({"enabled": False, "archive_dir": str(arch)}))
    lines: list[str] = []
    assert st.main(["scan"], lines.append) == 2 and "off" in lines[0]
    assert st.main(["status"], lines.append) == 0 and '"enabled": false' in lines[-1]


def test_api_routes_status_scan_and_operator_only_apply(env, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from bot.dashboard import fileserver_api

    root, arch, cfg = env
    _file(root / "data" / "attachments" / "a_old.png")
    monkeypatch.setattr(st, "_state_root", lambda r=None: root)
    monkeypatch.setattr(st, "settings", lambda c=None: _REAL_SETTINGS(cfg))
    monkeypatch.setenv("DASHBOARD_TOKEN", "t0ken")
    app = FastAPI()
    fileserver_api.register(app, lambda: None)  # the page's own auth is stubbed; operator routes check the token
    c = TestClient(app)
    assert c.get("/api/fileserver/tiering").json()["ready"] is True
    assert c.get("/api/fileserver/tiering/scan").json()["files"] == 1
    assert c.post("/api/fileserver/tiering/apply").status_code == 403
    assert c.post("/api/fileserver/tiering/apply", headers={"x-dashboard-token": "wrong"}).status_code == 403
    ok = c.post("/api/fileserver/tiering/apply", headers={"x-dashboard-token": "t0ken"})
    assert ok.status_code == 200 and ok.json()["files"] == 1
    assert c.get("/api/fileserver/tiering/index").json()[0]["path"] == "data/attachments/a_old.png"
    back = c.post("/api/fileserver/tiering/recall", json={"path": "data/attachments/a_old.png"},
                  headers={"x-dashboard-token": "t0ken"})
    assert back.status_code == 200 and (root / "data" / "attachments" / "a_old.png").exists()
    assert c.post("/api/fileserver/tiering/recall", json={"path": "nope"}, headers={"x-dashboard-token": "t0ken"}).status_code == 400


_REAL_SETTINGS = st.settings

def test_two_requests_recalling_one_file_at_once_both_get_it(env):
    import threading
    root, arch, cfg = env
    f = _file(root / "data" / "attachments" / "a_old.png", b"payload" * 50000)
    st.apply(root, cfg, NOW)
    assert not f.exists()
    results: list[bool] = []
    threads = [threading.Thread(target=lambda: results.append(st.ensure_hot(f, root, cfg))) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results == [True] * 4
    assert f.read_bytes() == b"payload" * 50000
    assert not list(f.parent.glob("*.recall.part"))


# ---- schedule -------------------------------------------------------------------------
def _at(hour, minute, day=15):
    return time.mktime((2026, 3, day, hour, minute, 0, 0, 0, -1))


def test_schedule_due_logic():
    cfg = st.settings({"schedule": {"daily_at": "03:30"}})
    assert not st.schedule_due(cfg, 0, _at(3, 29))
    assert st.schedule_due(cfg, 0, _at(3, 30))
    assert st.schedule_due(cfg, 0, _at(23, 0))                    # missed while down: runs when next up that day
    assert not st.schedule_due(cfg, _at(3, 31), _at(23, 0))      # already attempted since the slot
    assert st.schedule_due(cfg, _at(3, 31), _at(3, 30, 16))      # next day
    for bad in ("", "25:00", "3pm", "03:60"):
        assert not st.schedule_due(st.settings({"schedule": {"daily_at": bad}}), 0, _at(12, 0))
    assert not st.schedule_due(st.settings({}), 0, _at(12, 0))   # default: off


def test_scheduled_run_applies_once_a_day_and_status_shows_it(env):
    root, arch, cfg = env
    cfg["schedule"] = {"daily_at": "03:30"}
    f = _file(root / "data" / "attachments" / "old.bin", age_days=200)
    now = _at(4, 0)
    _file(f, age_days=200)
    os.utime(f, (now - 200 * DAY, now - 200 * DAY))
    assert st.run_scheduled(root, cfg, _at(3, 0)) is None and f.exists()
    rec = st.run_scheduled(root, cfg, now)
    assert rec["ok"] and rec["files"] == 1 and not f.exists()
    assert st.run_scheduled(root, cfg, _at(5, 0)) is None            # once per day
    s = st.status(root, cfg)
    assert s["last_scheduled_run"]["ok"] and s["last_scheduled_run"]["files"] == 1
    assert s["schedule"] == {"daily_at": "03:30"}
    assert st.run_scheduled(root, cfg, _at(3, 31, 16)) is not None   # the next day runs again


def test_scheduled_run_needs_enabled_and_a_valid_archive_and_never_raises(env):
    root, arch, cfg = env
    cfg["schedule"] = {"daily_at": "03:30"}
    f = _file(root / "data" / "attachments" / "old.bin")
    off = {**cfg, "enabled": False}
    assert st.run_scheduled(root, off, _at(4, 0)) is None and f.exists()
    bad = {**cfg, "archive_dir": str(root / "data" / "inside")}
    rec = st.run_scheduled(root, bad, _at(4, 0))                     # refused: recorded as a failure, file stays
    assert rec["ok"] is False and "outside" in rec["error"] and f.exists()
    assert st.run_scheduled(root, bad, _at(5, 0)) is None            # a failed run is not retried the same day
    assert st.status(root, bad)["last_scheduled_run"]["ok"] is False


def test_scheduled_run_is_one_at_a_time(env):
    root, arch, cfg = env
    cfg["schedule"] = {"daily_at": "03:30"}
    f = _file(root / "data" / "attachments" / "old.bin")
    assert st._run_lock.acquire(blocking=False)
    try:
        assert st.run_scheduled(root, cfg, _at(4, 0)) is None and f.exists()
    finally:
        st._run_lock.release()


def test_scheduled_tick_runs_in_a_worker_thread(env, monkeypatch):
    import asyncio
    import threading
    root, arch, cfg = env
    cfg["schedule"] = {"daily_at": "03:30"}
    _file(root / "data" / "attachments" / "old.bin")
    seen = {}
    real = st.apply

    def spy(*a, **k):
        seen["thread"] = threading.current_thread()
        return real(*a, **k)
    monkeypatch.setattr(st, "apply", spy)
    rec = asyncio.run(st.scheduled_tick(root, cfg, _at(4, 0)))
    assert rec["ok"] and seen["thread"] is not threading.main_thread()


def test_the_scheduler_poll_loop_drives_the_tick(monkeypatch):
    import asyncio
    from bot import scheduler
    calls = []

    async def fake_tick(*a, **k):
        calls.append(1)
    monkeypatch.setattr(scheduler.db, "list_due_scheduled_commands", lambda now: [])
    monkeypatch.setattr(st, "scheduled_tick", fake_tick)

    async def go(daily):
        monkeypatch.setattr(st, "settings", lambda cfg=None: {"schedule": {"daily_at": daily}})
        stop = asyncio.Event()
        t = asyncio.create_task(scheduler.run_forever(stop))
        await asyncio.sleep(0.2)
        stop.set()
        await t
    asyncio.run(go(""))
    assert calls == []                                    # no schedule configured: the tick is not even started
    asyncio.run(go("03:30"))
    assert calls == [1]
