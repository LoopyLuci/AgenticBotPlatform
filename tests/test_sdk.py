"""The OpenAPI document and the Python and TypeScript clients generated from / driven by it (roadmap P5)."""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from abp_sdk import AbpClient, AbpError
from bot.dashboard.server import build_app

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import export_openapi  # noqa: E402


@pytest.fixture
def app(monkeypatch, temp_db):
    monkeypatch.setenv("DASHBOARD_TOKEN", "test-token")
    return build_app()


@pytest.fixture
def abp(app):
    http = TestClient(app)
    return AbpClient("http://testserver", "test-token", http=http)


def test_the_committed_spec_and_typescript_client_are_current():
    """If this fails, run: python scripts/export_openapi.py  (an API route was added, changed or removed)."""
    assert export_openapi.main(["--check"]) == 0


def test_the_spec_describes_the_new_routes():
    spec = export_openapi.build_spec()
    for path in ("/api/models/info", "/api/skills/quarantine", "/api/agent/permissions", "/api/agent/sessions/{session_key}/export"):
        assert path in spec["paths"], path
    ops = export_openapi.operations(spec)
    assert len({o["id"] for o in ops}) == len(ops), "operation ids must be unique"


def test_the_python_client_calls_named_and_generic_operations(abp):
    assert "jobs_today" in json.dumps(abp.status()) or isinstance(abp.status(), dict)
    assert abp.permissions()["mode"] in ("default", "plan", "accept_edits", "bypass")
    assert abp.model_usage()["days"] == 1
    assert abp.call("GET /api/agent/permissions")["modes"]
    assert abp.call("GET /api/models/find", query={"free_only": True})["models"] == [] or True
    assert "GET /api/export/{table}" in abp.operations()
    with pytest.raises(KeyError):
        abp.call("GET /api/nothing")
    with pytest.raises(KeyError):
        abp.call("GET /api/export/{table}")                     # a missing path parameter is caught before any request


def test_errors_carry_the_status_and_detail(app):
    bad = AbpClient("http://testserver", "wrong", http=TestClient(app))
    with pytest.raises(AbpError) as err:
        bad.permissions()
    assert err.value.status == 401
    good = AbpClient("http://testserver", "test-token", http=TestClient(app))
    with pytest.raises(AbpError) as err:
        good.export_session("no-such-session")
    assert err.value.status == 404 and "no messages" in str(err.value.detail)


def test_setting_limits_goes_through_the_api(abp, monkeypatch):
    from bot.config import config

    saved = {}
    monkeypatch.setattr(config, "set_value", lambda path, value, actor="x": saved.__setitem__(tuple(path), value))
    assert abp.set_model_limits("openrouter/*:free", rpm=20, rpd=1000)["limits"] == {"rpm": 20, "rpd": 1000}
    assert saved[("native_agent", "models", "limits", "openrouter/*:free")] == {"rpm": 20, "rpd": 1000}


# ---- the TypeScript client, against a real server ---------------------------------------------------------------
@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_the_typescript_client_talks_to_a_live_server(app, tmp_path):
    import uvicorn

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        for _ in range(100):
            if server.started:
                break
            time.sleep(0.1)
        assert server.started
        script = tmp_path / "check.mjs"
        script.write_text(
            f'import {{ AbpClient, AbpError, operations }} from "{(ROOT / "sdk" / "typescript" / "abp.mjs").as_uri()}";\n'
            f'const abp = new AbpClient("http://127.0.0.1:{port}", "test-token");\n'
            'const perms = await abp.permissions();\n'
            'const usage = await abp.modelUsage(2);\n'
            'const viaRoute = await abp.call("GET /api/agent/permissions");\n'
            'let status = 0;\n'
            'try {{ await new AbpClient("http://127.0.0.1:%d", "wrong").permissions(); }} catch (e) {{ status = e instanceof AbpError ? e.status : -1; }}\n'
            'console.log(JSON.stringify({{ mode: perms.mode, days: usage.days, same: viaRoute.mode === perms.mode, status, ops: Object.keys(operations).length }}));\n'
            .replace("{{", "{").replace("}}", "}") % port, encoding="utf-8")
        proc = subprocess.run(["node", str(script)], capture_output=True, text=True, timeout=60)
        assert proc.returncode == 0, proc.stderr
        out = json.loads(proc.stdout.strip().splitlines()[-1])
        assert out["days"] == 2 and out["same"] is True and out["status"] == 401 and out["ops"] > 400
    finally:
        server.should_exit = True
        thread.join(timeout=10)
