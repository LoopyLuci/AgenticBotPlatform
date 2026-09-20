"""bot/server_identity.py + /healthz's server_id — lets the Android app tell
"my server at a new address" from "some other ABP on the network" before it
adopts an address or sends its API key there."""
from __future__ import annotations

import re

from fastapi.testclient import TestClient

from bot import server_identity
from bot.dashboard.server import build_app


def test_an_id_is_created_once_and_is_stable(tmp_path):
    path = tmp_path / "data" / "server_id"
    first = server_identity.get_server_id(path)
    second = server_identity.get_server_id(path)
    assert first == second
    assert re.fullmatch(r"[0-9a-f]{32}", first)
    assert path.read_text(encoding="utf-8").strip() == first


def test_an_existing_id_survives_a_restart(tmp_path):
    path = tmp_path / "server_id"
    path.write_text("0123456789abcdef0123456789abcdef", encoding="utf-8")
    assert server_identity.get_server_id(path) == "0123456789abcdef0123456789abcdef"


def test_a_corrupt_id_file_is_replaced_not_trusted(tmp_path):
    path = tmp_path / "server_id"
    path.write_text("not-a-valid-id", encoding="utf-8")
    new_id = server_identity.get_server_id(path)
    assert re.fullmatch(r"[0-9a-f]{32}", new_id) and new_id != "not-a-valid-id"


def test_two_installs_get_different_ids(tmp_path):
    assert server_identity.get_server_id(tmp_path / "a") != server_identity.get_server_id(tmp_path / "b")


def test_an_unwritable_state_dir_does_not_break_the_server(tmp_path):
    blocker = tmp_path / "a-file"
    blocker.write_text("x", encoding="utf-8")
    result = server_identity.get_server_id(blocker / "sub" / "server_id")  # parent is a file
    assert re.fullmatch(r"[0-9a-f]{32}", result)


def test_healthz_reports_the_server_id_without_auth(temp_db, tmp_path, monkeypatch):
    monkeypatch.setattr(server_identity, "ID_PATH", tmp_path / "server_id")
    monkeypatch.setattr(server_identity, "_cached", None)
    body = TestClient(build_app()).get("/healthz").json()
    assert body["status"] == "ok"
    assert body["server_id"] == server_identity.get_server_id()
    assert re.fullmatch(r"[0-9a-f]{32}", body["server_id"])
