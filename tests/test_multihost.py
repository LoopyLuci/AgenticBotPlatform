"""Multi-host management: ?host=<linked server id> sends container/VM/Tailscale/terminal requests to a linked server.
A real uvicorn server stands in for the remote machine (it points its own peer row at itself), so the proxy, the
peer-key auth tier and the opt-in switch all run for real over HTTP and WebSocket."""
from __future__ import annotations

import asyncio
import json
import os
import socket
import sys
import threading
import time

import httpx
import pytest
import uvicorn

from bot import db, envfile
from bot.dashboard.server import build_app

TOKEN = "multi-host-token"


@pytest.fixture
def remote(temp_db, monkeypatch):
    """One live server (acts as both machines) + an in-memory stand-in for .env."""
    env: dict[str, str] = {}
    monkeypatch.setattr(envfile, "get_var", lambda k, *a, **kw: env.get(k, ""))
    monkeypatch.setattr(envfile, "set_var", lambda k, v, actor="dashboard": env.__setitem__(k, v))
    monkeypatch.setenv("DASHBOARD_TOKEN", TOKEN)
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(build_app(), host="127.0.0.1", port=port, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
            break
        except OSError:
            time.sleep(0.1)
    key_id, peer_key = db.create_api_key("peer: A", kind="peer_server")
    peer_id = db.create_peer_server("Server B", f"http://127.0.0.1:{port}", peer_key, key_id)
    yield {"base": f"http://127.0.0.1:{port}", "peer_id": peer_id, "peer_key": peer_key, "env": env, "port": port}
    server.should_exit = True
    thread.join(timeout=5)


H = {"X-Dashboard-Token": TOKEN}


def test_remote_management_is_off_until_the_owner_turns_it_on(remote):
    base, pid = remote["base"], remote["peer_id"]
    direct = httpx.get(f"{base}/api/docker/templates", headers={"X-Dashboard-Token": remote["peer_key"]})
    assert direct.status_code == 401                                    # a peer key alone gets nothing by default
    via = httpx.get(f"{base}/api/docker/templates?host={pid}", headers=H)
    assert via.status_code == 403 and "Allow linked servers" in via.json()["detail"]

    assert httpx.post(f"{base}/api/infra/peer-access", json={"enabled": True}, headers=H).json() == {"enabled": True}
    assert remote["env"]["PEER_INFRA_ACCESS"] == "1"
    via = httpx.get(f"{base}/api/docker/templates?host={pid}", headers=H)
    assert via.status_code == 200 and any(t["id"] == "nginx" for t in via.json())
    assert httpx.get(f"{base}/api/docker/templates", headers={"X-Dashboard-Token": remote["peer_key"]}).status_code == 200


def test_a_peer_or_phone_can_never_grant_itself_access_or_relay_through_this_server(remote):
    base, pid = remote["base"], remote["peer_id"]
    _, phone = db.create_api_key("phone", kind="device")
    httpx.post(f"{base}/api/infra/peer-access", json={"enabled": True}, headers=H)
    for key in (remote["peer_key"], phone):
        h = {"X-Dashboard-Token": key}
        assert httpx.post(f"{base}/api/infra/peer-access", json={"enabled": False}, headers=h).status_code == 401
        assert httpx.get(f"{base}/api/docker/templates?host={pid}", headers=h).status_code == 401   # no relaying
    assert remote["env"]["PEER_INFRA_ACCESS"] == "1"
    # even with access on, a peer key stays out of everything that is not infra
    assert httpx.get(f"{base}/api/config", headers={"X-Dashboard-Token": remote["peer_key"]}).status_code in (401, 403)
    assert httpx.post(f"{base}/api/env/set", json={"key": "X", "value": "1"},
                      headers={"X-Dashboard-Token": remote["peer_key"]}).status_code in (401, 403)


def test_turning_access_off_again_cuts_the_peer_off(remote):
    base, pid = remote["base"], remote["peer_id"]
    httpx.post(f"{base}/api/infra/peer-access", json={"enabled": True}, headers=H)
    assert httpx.get(f"{base}/api/docker/templates?host={pid}", headers=H).status_code == 200
    httpx.post(f"{base}/api/infra/peer-access", json={"enabled": False}, headers=H)
    assert httpx.get(f"{base}/api/docker/templates?host={pid}", headers=H).status_code == 403


def test_hosts_list_unknown_hosts_and_forwarded_writes(remote):
    base, pid = remote["base"], remote["peer_id"]
    hosts = httpx.get(f"{base}/api/infra/hosts", headers=H).json()["hosts"]
    assert hosts[0]["id"] == "local" and hosts[1]["name"] == "Server B" and "outbound_api_key" not in json.dumps(hosts)
    assert httpx.get(f"{base}/api/docker/templates?host=999", headers=H).status_code == 404
    assert httpx.get(f"{base}/api/docker/templates?host=local", headers=H).status_code == 200
    httpx.post(f"{base}/api/infra/peer-access", json={"enabled": True}, headers=H)
    made = httpx.post(f"{base}/api/infra/rules?host={pid}", headers=H, json={
        "name": "remote rule", "trigger": {"type": "interval", "every": "1h"}, "action": {"type": "notify"}})
    assert made.status_code == 200 and made.json()["name"] == "remote rule"
    assert httpx.post(f"{base}/api/docker/containers/--x/action?host={pid}", headers=H,
                      json={"action": "start"}).status_code == 400        # the remote's own validation still applies


def _fake_docker(tmp_path, monkeypatch):
    prog = tmp_path / "echo_shell.py"
    prog.write_text("import sys\nprint('READY', flush=True)\nfor line in sys.stdin:\n    print('got:'+line.strip(), flush=True)\n",
                    encoding="utf-8")
    if os.name == "nt":
        (tmp_path / "docker.cmd").write_text(f'@echo off\r\n"{sys.executable}" "{prog}"\r\n')
    else:
        exe = tmp_path / "docker"
        exe.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{prog}"\n')
        exe.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ["PATH"])


def test_a_terminal_can_be_opened_on_a_linked_server(remote, tmp_path, monkeypatch):
    import websockets
    _fake_docker(tmp_path, monkeypatch)
    base, pid, port = remote["base"], remote["peer_id"], remote["port"]

    async def session(host_query):
        url = f"ws://127.0.0.1:{port}/api/terminals/ws?kind=container&target=web&token={TOKEN}{host_query}"
        async with websockets.connect(url) as ws:
            first = json.loads(await asyncio.wait_for(ws.recv(), 10))
            if first["type"] != "ready":
                return first
            await ws.send(json.dumps({"type": "input", "data": "ping\r"}))
            seen = ""
            end = time.time() + 15
            while "got:ping" not in seen and time.time() < end:
                m = json.loads(await asyncio.wait_for(ws.recv(), 10))
                seen += m.get("data", "") if m["type"] == "output" else ""
            return {"type": "ok" if "got:ping" in seen else "missing", "seen": seen}

    refused = asyncio.run(session(f"&host={pid}"))                # access still off on the remote
    assert refused["type"] == "error" and "Allow linked servers" in refused["message"]
    httpx.post(f"{base}/api/infra/peer-access", json={"enabled": True}, headers=H)
    assert asyncio.run(session(f"&host={pid}"))["type"] == "ok"   # relayed through to the remote's PTY
