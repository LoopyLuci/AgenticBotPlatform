"""The reference node against the real server half, over real HTTP (roadmap P7)."""
from __future__ import annotations

import asyncio
import base64
import socket
import threading
import time

import httpx
import pytest

from abp_node.__main__ import ReferenceNode
from bot import db, nodes
from bot.dashboard.server import build_app

pytestmark = pytest.mark.usefixtures("temp_db")


@pytest.fixture
def live(monkeypatch, temp_db):
    import uvicorn

    monkeypatch.setenv("DASHBOARD_TOKEN", "owner-token")
    device_id, key = db.create_api_key("phone", permission_tier="standard")
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(build_app(), host="127.0.0.1", port=port, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.1)
    loop = server.servers[0].get_loop()

    def agent_invoke(cap, args=None, timeout=15):
        return asyncio.run_coroutine_threadsafe(nodes.invoke(device_id, cap, args or {}, timeout_s=timeout), loop).result(timeout=timeout + 5)

    yield f"http://127.0.0.1:{port}", key, device_id, agent_invoke
    server.should_exit = True
    thread.join(timeout=10)


def consent(base, device_id, cap, mode):
    r = httpx.put(f"{base}/api/nodes/{device_id}/consent", json={"capability": cap, "mode": mode}, headers={"X-Dashboard-Token": "owner-token"})
    assert r.status_code == 200, r.text


def serve(node, times=1):
    def loop():
        for _ in range(times):
            node.run_once(wait=10)

    t = threading.Thread(target=loop)
    t.start()
    return t


def test_the_agent_gets_the_location_a_node_reports(live):
    base, key, device_id, invoke = live
    node = ReferenceNode(base, key, name="Sim")
    assert set(node.register()) >= {"location.get", "camera.snap"}
    consent(base, device_id, "location.get", "allow")
    t = serve(node)
    time.sleep(0.5)
    result = invoke("location.get")
    t.join()
    assert result["lat"] == 40.7128 and result["provider"] == "reference-node"


def test_a_photo_comes_back_as_an_image(live):
    base, key, device_id, invoke = live
    node = ReferenceNode(base, key)
    node.register()
    consent(base, device_id, "camera.snap", "allow")
    t = serve(node)
    time.sleep(0.5)
    result = invoke("camera.snap")
    t.join()
    text, info = nodes.store_result(result)
    assert "saved at" in text and base64.b64decode(result["image_b64"])[:4] == b"\x89PNG"


def test_a_node_whose_user_says_no_makes_the_command_fail_cleanly(live):
    base, key, device_id, invoke = live
    asked = []
    node = ReferenceNode(base, key, ask=lambda cap, args: asked.append(cap) or False)
    node.register()
    consent(base, device_id, "clipboard.read", "device")
    t = serve(node)
    time.sleep(0.5)
    with pytest.raises(nodes.NodeError, match="the user declined"):
        invoke("clipboard.read")
    t.join()
    assert asked == ["clipboard.read"]


def test_a_capability_the_node_does_not_offer_cannot_be_used(live):
    base, key, device_id, invoke = live
    node = ReferenceNode(base, key, capabilities=["location.get"])
    node.register()
    with pytest.raises(nodes.NodeError, match="does not offer"):
        invoke("camera.snap")
