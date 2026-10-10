"""A module hub that accepts a connection and never answers must not stall the app: the cluster heartbeat probes
module hubs (bot/modules/client.find, a blocking HTTP call) and has to do it off the event loop."""
from __future__ import annotations

import dataclasses
import json
import shutil
import socket
import sys
import threading
import time
from pathlib import Path

import httpx
import uvicorn

from bot.cluster import inventory, membership
from bot.dashboard.server import build_app
from bot.modules import registry
from bot.runtime import legacy

ROOT = Path(__file__).resolve().parent.parent
FIXTURE = ROOT / "tests" / "fixtures" / "fake_module"
FAKE = {"module": {"id": "fake-mod", "name": "FakeMod", "repo": "https://example.invalid/fake-mod.git"},
        "checkout": {"marker": ["hub.py"]}}
TOKEN = "loop-test-token"


class BlackHole:
    """Accepts connections and never answers them."""

    def __init__(self) -> None:
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(64)
        self.port = self.sock.getsockname()[1]
        self.held: list = []
        self.accepted = 0
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self) -> None:
        while True:
            try:
                c, _ = self.sock.accept()
            except OSError:
                return
            self.accepted += 1
            self.held.append(c)

    def close(self) -> None:
        self.sock.close()
        for c in self.held:
            c.close()


def test_a_hung_module_hub_does_not_delay_requests_while_the_heartbeat_runs(temp_db, tmp_path, monkeypatch):
    repo = tmp_path / "FakeMod"
    shutil.copytree(FIXTURE, repo)
    toml = (repo / "abp-module.toml").read_text(encoding="utf-8")
    exe = Path(sys.executable).as_posix()
    (repo / "abp-module.toml").write_text(toml.replace('start = ["python"', f'start = ["{exe}"'), encoding="utf-8")
    monkeypatch.setattr(registry, "BUILTIN", [*registry.BUILTIN, FAKE])
    monkeypatch.setenv("ABP_MODULE_FAKE_MOD_DIR", str(repo))
    monkeypatch.setattr(registry, "data_dir", lambda m: tmp_path / "data" / m.id)
    registry.modules(refresh=True)
    # this machine's software inventory is cached for 2 minutes; a test before this one may have filled it
    monkeypatch.setattr(inventory, "_static", {"at": 0.0, "data": None})
    hole = BlackHole()
    (tmp_path / "data" / "fake-mod").mkdir(parents=True)
    (tmp_path / "data" / "fake-mod" / "control.json").write_text(
        json.dumps({"url": f"http://127.0.0.1:{hole.port}", "token": "x", "pid": 1}), encoding="utf-8")

    monkeypatch.setenv("DASHBOARD_TOKEN", TOKEN)
    monkeypatch.setattr(membership, "HEARTBEAT_S", 0.2)
    monkeypatch.setattr(legacy, "TASKS_ENABLED", True)
    # only the cluster heartbeat runs; the other areas' tasks (hosting, DNS ...) have no business in this test
    for uid, area in list(legacy.AREAS.items()):
        keep = tuple(t for t in area.tasks if t.startswith("bot.cluster.membership:"))
        if area.tasks and keep != area.tasks:
            monkeypatch.setitem(legacy.AREAS, uid, dataclasses.replace(area, tasks=keep))
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    srv = uvicorn.Server(uvicorn.Config(build_app(), host="127.0.0.1", port=port, log_level="error"))
    t = threading.Thread(target=srv.run, daemon=True)
    t.start()
    try:
        for _ in range(100):
            try:
                socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
                break
            except OSError:
                time.sleep(0.1)
        deadline = time.monotonic() + 20
        while hole.accepted == 0 and time.monotonic() < deadline:
            time.sleep(0.05)
        assert hole.accepted, "the heartbeat never probed the module hub"
        worst = 0.0
        for _ in range(10):
            t0 = time.monotonic()
            r = httpx.get(f"http://127.0.0.1:{port}/api/auth/status", headers={"X-Dashboard-Token": TOKEN}, timeout=10)
            worst = max(worst, time.monotonic() - t0)
            assert r.status_code < 500, r.text
            time.sleep(0.1)
        assert worst < 1.0, f"a request waited {worst:.1f}s behind the hung module hub probe"
    finally:
        srv.should_exit = True
        t.join(timeout=10)
        hole.close()
