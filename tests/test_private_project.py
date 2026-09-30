"""ABP's side of the PrivateProject link (bot/private_project.py, the "private_project" backend, /api/private_project, the "private_project" integration
preset), against a real HTTP + WebSocket server that speaks PrivateProject's remote session API as its ADR-0091/0097
document it: a paired-device Bearer token, messages only for the active agent type (409 otherwise), a POST that
returns when the turn is done, and the reply published as chat_message events (streaming, then final)."""
from __future__ import annotations

import asyncio
import shutil
import socket
import threading
import time

import pytest
import uvicorn
import yaml
from fastapi import FastAPI, Header, HTTPException, WebSocket
from fastapi.testclient import TestClient

from bot import envfile, integrations, private_project
from bot.backends.base import BackendError
from bot.backends.private_project_backend import PrivateProjectBackend
from bot.config import config

DEVICE_TOKEN = "device-token-for-abp"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Desk:
    """PrivateProject's desktop, as far as its session API shows it."""

    def __init__(self) -> None:
        self.active = "build"
        self.messages: dict[str, list[dict]] = {"build": [], "plan": []}
        self.sockets: dict[str, list[WebSocket]] = {}
        self.n = 0

    def app(self) -> FastAPI:
        app = FastAPI()

        def auth(authorization: str = "") -> None:
            if authorization != f"Bearer {DEVICE_TOKEN}":
                raise HTTPException(status_code=401, detail="missing or invalid paired-device token")

        async def publish(agent_type: str, record: dict) -> None:
            lst = self.messages[agent_type]
            for i, m in enumerate(lst):
                if m["id"] == record["id"]:
                    lst[i] = record
                    break
            else:
                lst.append(record)
            for ws in list(self.sockets.get(agent_type, [])):
                await ws.send_json({"type": "chat_message", "data": record})

        @app.get("/api/v1/sessions")
        async def sessions(authorization: str = Header(default="")):
            auth(authorization)
            return [{"agentType": k, "messageCount": len(v)} for k, v in self.messages.items()]

        @app.get("/api/v1/models")
        async def models(authorization: str = Header(default="")):
            auth(authorization)
            return [{"id": "local-gguf"}]

        @app.get("/api/v1/sessions/{agent_type}/messages")
        async def get_messages(agent_type: str, authorization: str = Header(default="")):
            auth(authorization)
            return self.messages.get(agent_type, [])

        @app.post("/api/v1/sessions/{agent_type}/messages")
        async def send(agent_type: str, body: dict, authorization: str = Header(default="")):
            auth(authorization)
            if agent_type != self.active:
                raise HTTPException(status_code=409, detail="x")
            self.n += 1
            await publish(agent_type, {"id": f"u{self.n}", "agent_type": agent_type, "role": "user",
                                       "content": body["content"], "streaming": None})
            reply = f"private_project heard: {body['content']}"
            await publish(agent_type, {"id": f"a{self.n}", "agent_type": agent_type, "role": "assistant",
                                       "content": reply[:5], "streaming": True})
            await asyncio.sleep(0.05)
            await publish(agent_type, {"id": f"a{self.n}", "agent_type": agent_type, "role": "assistant",
                                       "content": reply, "streaming": False, "token_count": 7, "model_id": "local-gguf"})
            return {"ok": True}

        @app.exception_handler(HTTPException)
        async def as_private_project(request, exc):
            from fastapi.responses import JSONResponse
            msg = (f'agent type "{request.path_params.get("agent_type")}" is not the currently active session '
                   f'(active: "{self.active}")') if exc.status_code == 409 else exc.detail
            return JSONResponse({"error": msg}, status_code=exc.status_code)

        @app.websocket("/api/v1/sessions/{agent_type}/events")
        async def events(ws: WebSocket, agent_type: str):
            if ws.headers.get("authorization") != f"Bearer {DEVICE_TOKEN}":
                await ws.close(code=4401)
                return
            await ws.accept()
            self.sockets.setdefault(agent_type, []).append(ws)
            try:
                while True:
                    await ws.receive_text()
            except Exception:  # noqa: BLE001 - the client left
                self.sockets[agent_type].remove(ws)
        return app


@pytest.fixture
def desk():
    d = Desk()
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(d.app(), host="127.0.0.1", port=port, log_level="error"))
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    d.url = f"http://127.0.0.1:{port}"
    yield d
    server.should_exit = True
    t.join(5)


@pytest.fixture
def home(tmp_path, monkeypatch, temp_db):
    """A real config file and a real .env, both temporary."""
    cfg = tmp_path / "backends.yaml"
    shutil.copy(config.path, cfg)
    monkeypatch.setattr(config, "path", cfg)
    monkeypatch.setattr(config, "_data", dict(config._data))
    env = tmp_path / ".env"
    env.write_text("DASHBOARD_TOKEN=test-token\n", encoding="utf-8")
    monkeypatch.setattr(envfile, "PROJECT_ENV", env)
    monkeypatch.setattr(envfile, "GLOBAL_ENV", env)
    monkeypatch.setattr(envfile, "configured_override", lambda: None)
    monkeypatch.setenv("DASHBOARD_TOKEN", "test-token")
    config.reload(actor="test")
    return {"cfg": cfg, "env": env}


def test_linking_checks_the_address_and_token_then_keeps_them(desk, home):
    assert private_project.status()["linked"] is False and private_project.ready()[0] is False
    with pytest.raises(private_project.PrivateProjectError) as e:
        private_project.link(desk.url, "not-the-token")
    assert e.value.status == 401 and "PRIVATE_PROJECT_DEVICE_TOKEN" not in home["env"].read_text(encoding="utf-8")
    with pytest.raises(private_project.PrivateProjectError, match="session API address"):
        private_project.link("ftp://x", DEVICE_TOKEN)
    st = private_project.link(desk.url, DEVICE_TOKEN, agent_type="build", label="PrivateProject on this PC")
    assert st["linked"] and st["reachable"] and st["base_url"] == desk.url and DEVICE_TOKEN not in str(st)
    assert f"PRIVATE_PROJECT_DEVICE_TOKEN={DEVICE_TOKEN}" in home["env"].read_text(encoding="utf-8")
    assert yaml.safe_load(home["cfg"].read_text(encoding="utf-8"))["backends"]["private_project"]["base_url"] == desk.url
    assert private_project.ready() == (True, "")
    assert private_project.models() == [{"id": "local-gguf"}]
    # PrivateProject restarts on another port and announces it; the token it was given stays
    assert private_project.link(desk.url + "/", "")["base_url"] == desk.url
    assert private_project.unlink()["linked"] is False


def test_the_backend_returns_the_finished_reply_and_says_why_it_cannot(desk, home):
    b = PrivateProjectBackend(desk.url, DEVICE_TOKEN, "build")
    r = asyncio.run(b.ask("what is 2+2?"))
    assert r.text == "private_project heard: what is 2+2?" and r.tokens == 7 and r.raw["model"] == "local-gguf"
    r2 = asyncio.run(b.ask("and again"))                                # an earlier turn's reply is never reused
    assert r2.text == "private_project heard: and again"
    with pytest.raises(BackendError, match='not on the "plan" agent right now'):
        asyncio.run(PrivateProjectBackend(desk.url, DEVICE_TOKEN, "plan").ask("x"))
    with pytest.raises(BackendError, match="refused ABP's device token"):
        asyncio.run(PrivateProjectBackend(desk.url, "wrong", "build").ask("x"))
    with pytest.raises(BackendError, match="does not answer"):
        asyncio.run(PrivateProjectBackend(f"http://127.0.0.1:{_free_port()}", DEVICE_TOKEN, "build").ask("x"))
    with pytest.raises(BackendError, match="isn't linked"):
        asyncio.run(PrivateProjectBackend("", "", "build").ask("x"))


def test_the_router_builds_it_from_the_link_and_the_bots_model_is_the_agent_type(desk, home):
    from bot import router as router_mod, setup_wizard
    from bot.models import BACKEND_FAMILY
    assert "private_project" in router_mod.VALID_BACKENDS and BACKEND_FAMILY["private_project"] == "private_project"
    ok, why = setup_wizard.check_backend_ready("private_project")
    assert not ok and "ABP Connector" in why
    private_project.link(desk.url, DEVICE_TOKEN)
    assert setup_wizard.check_backend_ready("private_project") == (True, "")
    r = router_mod.Router.__new__(router_mod.Router)
    b = r._build_backend("private_project", config.current, model_override="plan")
    assert isinstance(b, PrivateProjectBackend) and b.agent_type == "plan" and b.base_url == desk.url
    assert r._build_backend("private_project", config.current).agent_type == "build"
    desk.active = "plan"
    assert asyncio.run(b.ask("hi")).text == "private_project heard: hi"


def test_private_projects_own_key_links_and_asks_but_is_not_the_dashboard_token(desk, home):
    from bot.dashboard.server import build_app
    c = TestClient(build_app())
    D = {"X-Dashboard-Token": "test-token"}
    r = c.post("/api/integrations/keys", headers=D, json={"preset": "private_project", "allow_framing": False})
    assert r.status_code == 200 and {"agents:ask", "private_project:link", "mcp:register"} <= set(r.json()["scopes"])
    K = {"X-Dashboard-Token": r.json()["key"]}
    # PrivateProject announces itself with its own key
    r = c.post("/api/private_project/link", headers=K, json={"base_url": desk.url, "device_token": DEVICE_TOKEN})
    assert r.status_code == 200 and r.json()["reachable"] and DEVICE_TOKEN not in r.text
    assert c.get("/api/private_project", headers=K).json()["linked"] is True
    assert c.post("/api/private_project/link", headers=K, json={"base_url": desk.url, "device_token": "bad"}).status_code == 401
    # what its connector calls is reachable with that key (no such bot here: a 4xx from the route, not from auth)
    ask = c.post("/api/agent/ask", headers=K, json={"prompt": "x", "source_instance": "nope", "target_instance": "nope"})
    assert ask.status_code not in (401, 403)
    assert c.get("/api/swarms", headers=K).status_code == 200
    assert c.get("/api/mcp-external", headers=K).status_code == 200
    # and nothing else is
    for method, path in (("GET", "/api/config"), ("DELETE", "/api/private_project/link"), ("POST", "/api/private_project/ask"),
                         ("POST", "/api/bots/1/stop"), ("POST", "/api/integrations/keys")):
        assert c.request(method, path, headers=K, json={}).status_code == 403, path
    # the owner asks through the backend, from ABP
    out = c.post("/api/private_project/ask", headers=D, json={"prompt": "hello from ABP"})
    assert out.status_code == 200 and out.json()["text"] == "private_project heard: hello from ABP"
    assert c.get("/api/private_project/sessions/build/messages", headers=D).json()["messages"][-1]["role"] == "assistant"
    assert c.delete("/api/private_project/link", headers=D).json()["linked"] is False
    assert c.get("/api/private_project", headers={"X-Dashboard-Token": "nope"}).status_code == 401
    assert integrations.PRESETS["private_project"]["scopes"]
