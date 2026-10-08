"""bot/poller_claim.py - two copies of ABP with different data directories but the
same bot credentials must never both poll that bot (2026-10-05: a second poller
of the live Telegram token turned the user's bot off with a 409)."""
from __future__ import annotations

import asyncio
import subprocess
import sys
import textwrap
from pathlib import Path

from bot import platform_supervisor, poller_claim

ROOT = Path(platform_supervisor.__file__).resolve().parents[1]
ROW = {"id": 7, "name": "tg", "platform": "telegram", "credentials": {"bot_token": "unused-value"}}


def test_key_names_the_bot_not_the_token():
    key = poller_claim.claim_key(ROW)
    assert key.startswith("telegram-") and "unused-value" not in key
    assert poller_claim.claim_key(dict(ROW, id=8, name="other")) == key  # same bot, whatever the row
    assert poller_claim.claim_key(dict(ROW, credentials={"bot_token": "x"})) != key
    assert poller_claim.claim_key({"platform": "app", "credentials": {}}) is None


def test_claim_dir_follows_override(tmp_path, monkeypatch):
    monkeypatch.setenv(poller_claim.CLAIM_DIR_ENV, str(tmp_path / "c"))
    assert poller_claim.claim_dir() == tmp_path / "c"


def test_claim_dir_ignores_a_faked_home(monkeypatch, tmp_path):
    monkeypatch.delenv(poller_claim.CLAIM_DIR_ENV, raising=False)
    real = poller_claim.claim_dir()
    for var in ("HOME", "USERPROFILE", "LOCALAPPDATA"):
        monkeypatch.setenv(var, str(tmp_path / "fake-home"))
    assert poller_claim.claim_dir() == real


def test_claim_held_by_another_process_parks_the_runner(tmp_path, monkeypatch):
    """A real second process holds the claim: our runner must not start until it lets go."""
    monkeypatch.setattr(platform_supervisor, "CLAIM_RECHECK_S", 0.05)
    holder = subprocess.Popen(
        [sys.executable, "-c", textwrap.dedent(f"""
            import sys, time
            sys.path.insert(0, {str(ROOT)!r})
            from bot import poller_claim
            row = {ROW!r}
            claim = poller_claim.for_instance(row)
            assert claim.try_acquire({{"instance": "elsewhere", "state_root": "other-copy"}})
            print("held", flush=True)
            sys.stdin.readline()
        """)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
    )
    try:
        assert holder.stdout.readline().strip() == "held"
        started = []

        async def runner(row):
            started.append(row["id"])

        async def scenario():
            task = asyncio.create_task(platform_supervisor._run_claimed(ROW, runner))
            await asyncio.sleep(0.3)
            assert started == []  # parked behind the other copy
            holder.stdin.write("\n")
            holder.stdin.flush()
            await asyncio.wait_for(task, timeout=10)

        asyncio.run(scenario())
        assert started == [7]  # took over once the other copy was gone
    finally:
        holder.kill()
        holder.wait()


def test_parked_instance_says_who_has_it(monkeypatch):
    monkeypatch.setattr(platform_supervisor, "CLAIM_RECHECK_S", 0.05)
    other = poller_claim.for_instance(ROW)
    assert other.try_acquire({"instance": "elsewhere", "state_root": "other-copy"})
    seen = []
    monkeypatch.setattr(platform_supervisor, "_set_status", lambda iid, text, **kw: seen.append(text))

    async def runner(row):
        pass

    async def scenario():
        task = asyncio.create_task(platform_supervisor._run_claimed(ROW, runner))
        await asyncio.sleep(0.2)
        other.release()
        await asyncio.wait_for(task, timeout=5)

    try:
        asyncio.run(scenario())
    finally:
        other.release()
    assert "other-copy" in seen[0] and "'elsewhere'" in seen[0]
    assert seen[-1] == ""  # cleared once it started here


def test_claim_is_released_when_the_runner_ends():
    async def runner(row):
        raise RuntimeError("boom")

    try:
        asyncio.run(platform_supervisor._run_claimed(ROW, runner))
    except RuntimeError:
        pass
    probe = poller_claim.for_instance(ROW)
    assert probe.try_acquire()
    probe.release()


def test_instance_without_credentials_runs_unclaimed():
    ran = []

    async def runner(row):
        ran.append(row)

    asyncio.run(platform_supervisor._run_claimed({"id": 1, "name": "a", "platform": "email", "credentials": {}}, runner))
    assert len(ran) == 1
