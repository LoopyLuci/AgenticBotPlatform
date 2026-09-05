"""GET/POST/DELETE /api/files* — the routes backing "grab a file that
lives outside BotServer's own data (e.g. a freshly built Android APK)
from anywhere," built on bot/file_share.py's allowlisted-root model.
Exercised against the real FastAPI app via TestClient, with the registry
isolated to a temp config/file_share.yaml so no test touches this
machine's real one.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from bot import db, file_share
from bot.config import ConfigManager
from bot.dashboard.server import build_app


def _client(monkeypatch):
    monkeypatch.setenv("DASHBOARD_TOKEN", "test-token")
    return TestClient(build_app())


def _headers():
    return {"X-Dashboard-Token": "test-token"}


def _isolate_registry(tmp_path, monkeypatch):
    path = tmp_path / "file_share.yaml"
    path.write_text("roots: {}\n", encoding="utf-8")
    monkeypatch.setattr(file_share, "_manager", ConfigManager(path=path))


def _shared_dir(tmp_path):
    d = tmp_path / "shared"
    d.mkdir()
    (d / "app-debug.apk").write_bytes(b"fake apk bytes")
    return d


def test_list_roots_requires_auth(temp_db, monkeypatch, tmp_path):
    _isolate_registry(tmp_path, monkeypatch)
    client = _client(monkeypatch)

    resp = client.get("/api/files")

    assert resp.status_code == 401


def test_add_list_and_download_a_root_end_to_end(temp_db, monkeypatch, tmp_path):
    _isolate_registry(tmp_path, monkeypatch)
    client = _client(monkeypatch)
    shared = _shared_dir(tmp_path)

    add_resp = client.post("/api/files", headers=_headers(), json={"name": "shared", "path": str(shared)})
    assert add_resp.status_code == 200

    roots_resp = client.get("/api/files", headers=_headers())
    assert roots_resp.status_code == 200
    assert roots_resp.json()["shared"]["exists"] is True

    list_resp = client.get("/api/files/shared", headers=_headers())
    assert list_resp.status_code == 200
    names = [e["name"] for e in list_resp.json()["entries"]]
    assert "app-debug.apk" in names

    download_resp = client.get("/api/files/shared/download", headers=_headers(), params={"path": "app-debug.apk"})
    assert download_resp.status_code == 200
    assert download_resp.content == b"fake apk bytes"


def test_add_root_requires_the_desktop_token_not_a_mobile_key(temp_db, monkeypatch, tmp_path):
    _isolate_registry(tmp_path, monkeypatch)
    client = _client(monkeypatch)
    shared = _shared_dir(tmp_path)
    _, plaintext = db.create_api_key("test-phone", kind="device")

    resp = client.post("/api/files", headers={"X-Dashboard-Token": plaintext}, json={"name": "shared", "path": str(shared)})

    assert resp.status_code == 401


def test_a_paired_mobile_device_can_list_and_download_from_an_existing_root(temp_db, monkeypatch, tmp_path):
    _isolate_registry(tmp_path, monkeypatch)
    client = _client(monkeypatch)
    shared = _shared_dir(tmp_path)
    client.post("/api/files", headers=_headers(), json={"name": "shared", "path": str(shared)})
    _, plaintext = db.create_api_key("test-phone", kind="device")

    resp = client.get("/api/files/shared/download", headers={"X-Dashboard-Token": plaintext}, params={"path": "app-debug.apk"})

    assert resp.status_code == 200
    assert resp.content == b"fake apk bytes"


def test_download_rejects_a_traversal_attempt(temp_db, monkeypatch, tmp_path):
    _isolate_registry(tmp_path, monkeypatch)
    client = _client(monkeypatch)
    shared = _shared_dir(tmp_path)
    client.post("/api/files", headers=_headers(), json={"name": "shared", "path": str(shared)})

    resp = client.get("/api/files/shared/download", headers=_headers(), params={"path": "../../../../etc/passwd"})

    assert resp.status_code == 400


def test_list_404s_for_an_unknown_root(temp_db, monkeypatch, tmp_path):
    _isolate_registry(tmp_path, monkeypatch)
    client = _client(monkeypatch)

    resp = client.get("/api/files/nope", headers=_headers())

    assert resp.status_code == 404


def test_download_404s_for_a_missing_file(temp_db, monkeypatch, tmp_path):
    _isolate_registry(tmp_path, monkeypatch)
    client = _client(monkeypatch)
    shared = _shared_dir(tmp_path)
    client.post("/api/files", headers=_headers(), json={"name": "shared", "path": str(shared)})

    resp = client.get("/api/files/shared/download", headers=_headers(), params={"path": "does-not-exist.apk"})

    assert resp.status_code == 404


def test_add_root_400s_for_a_nonexistent_path(temp_db, monkeypatch, tmp_path):
    _isolate_registry(tmp_path, monkeypatch)
    client = _client(monkeypatch)

    resp = client.post("/api/files", headers=_headers(), json={"name": "ghost", "path": str(tmp_path / "nope")})

    assert resp.status_code == 400


def test_remove_root(temp_db, monkeypatch, tmp_path):
    _isolate_registry(tmp_path, monkeypatch)
    client = _client(monkeypatch)
    shared = _shared_dir(tmp_path)
    client.post("/api/files", headers=_headers(), json={"name": "shared", "path": str(shared)})

    resp = client.delete("/api/files/shared", headers=_headers())

    assert resp.status_code == 200
    assert client.get("/api/files", headers=_headers()).json() == {}


def test_remove_root_404s_when_not_found(temp_db, monkeypatch, tmp_path):
    _isolate_registry(tmp_path, monkeypatch)
    client = _client(monkeypatch)

    resp = client.delete("/api/files/nope", headers=_headers())

    assert resp.status_code == 404
