"""/api/cicd/* — the control-plane routes over the event store, plus the parity
guarantee: the HTTP transport, the local transport and the CLI all expose the
same capabilities and return identical data."""
from __future__ import annotations

import io
import json
import os
import urllib.error
import urllib.request

import pytest
from fastapi.testclient import TestClient

from abp_cicd import cli, recorder, service, transport
from abp_cicd.store import EventStore
from bot.dashboard.server import build_app

TOKEN = "test-token"
H = {"X-Dashboard-Token": TOKEN}


@pytest.fixture
def store():
    return EventStore(os.environ["ABP_CICD_DB"])          # the per-test store from conftest


@pytest.fixture
def client(monkeypatch, temp_db):
    monkeypatch.setenv("DASHBOARD_TOKEN", TOKEN)
    return TestClient(build_app())


@pytest.fixture
def seeded(store):
    with recorder.start_run("release", store=store, version="1.2.3", title="t") as run:
        run.record_step("preflight", "ok", 800)
        run.record_step("built_desktop", "ok", 240_000)
        run.record_step("smoke", "skipped", 0, skipped_reason="test")
        run.decision(actor="rules", decision="run the gate", reason="release", confidence=1.0)
    store.append("worker.heartbeat", {"worker": "flake_detector", "state": "serving", "model": "v1", "queue": 0})
    return run


PATHS = ["/api/cicd/summary", "/api/cicd/runs", "/api/cicd/steps/stats", "/api/cicd/decisions",
         "/api/cicd/workers", "/api/cicd/events", "/api/cicd/chain"]


@pytest.mark.parametrize("path", PATHS)
def test_every_route_requires_authentication(client, path):
    assert client.get(path).status_code == 401
    assert client.get(path, headers={"X-Dashboard-Token": "wrong"}).status_code == 401


def test_routes_return_the_recorded_data(client, seeded):
    runs = client.get("/api/cicd/runs", headers=H).json()["runs"]
    assert [r["id"] for r in runs] == [seeded.id] and runs[0]["kind"] == "release"
    run = client.get(f"/api/cicd/runs/{seeded.id}", headers=H).json()
    assert [s["name"] for s in run["steps"]] == ["preflight", "built_desktop", "smoke"]
    text = client.get(f"/api/cicd/runs/{seeded.id}/explain", headers=H).json()["text"]
    assert "release 1.2.3" in text and "slowest was 'built_desktop'" in text
    assert client.get("/api/cicd/workers", headers=H).json()["workers"][0]["worker"] == "flake_detector"
    assert client.get("/api/cicd/chain", headers=H).json()["ok"] is True
    stats = client.get("/api/cicd/steps/stats", params={"name": "built_desktop"}, headers=H).json()["steps"]
    assert stats["built_desktop"]["p50_ms"] == 240_000
    decisions = client.get("/api/cicd/decisions", headers=H).json()["decisions"]
    assert decisions[0]["decision"] == "run the gate"


def test_a_missing_run_is_a_404_not_a_500(client, seeded):
    assert client.get("/api/cicd/runs/nope", headers=H).status_code == 404
    assert client.get("/api/cicd/runs/nope/explain", headers=H).status_code == 404


def test_query_parameters_are_validated(client):
    assert client.get("/api/cicd/runs", params={"limit": 0}, headers=H).status_code == 422
    assert client.get("/api/cicd/events", params={"limit": 999999}, headers=H).status_code == 422
    assert client.get("/api/cicd/events", params={"since": -1}, headers=H).status_code == 422


def test_an_empty_store_answers_cleanly(client):
    assert client.get("/api/cicd/summary", headers=H).json()["events"] == 0
    assert client.get("/api/cicd/runs", headers=H).json() == {"runs": []}


def test_the_event_stream_sends_events_then_closes_when_not_following(client, seeded):
    resp = client.get("/api/cicd/events/stream", params={"follow": "false"}, headers=H)
    assert resp.status_code == 200 and resp.headers["content-type"].startswith("text/event-stream")
    frames = [f for f in resp.text.split("\n\n") if f.startswith("id:")]
    assert len(frames) >= 5
    first = json.loads(frames[0].split("data: ", 1)[1])
    assert first["kind"] == "run.start"
    # resuming from a cursor skips what was already delivered
    last_id = int(frames[-1].split("\n", 1)[0].removeprefix("id: "))
    again = client.get("/api/cicd/events/stream", params={"follow": "false", "since": last_id}, headers=H)
    assert "id:" not in again.text


def test_the_api_never_serves_secrets_that_were_recorded(client, store):
    fake = "sk" + "-" + "abcdefgh12345678"
    store.append("note", {"message": f"key {fake} password=hunter2"}, run_id="r")
    body = client.get("/api/cicd/events", headers=H).text
    assert fake not in body and "hunter2" not in body


# ---- parity ------------------------------------------------------------------ #
class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.fixture
def http(client, monkeypatch):
    """An HttpTransport whose network calls are served by the in-process app."""
    def fake_urlopen(req, timeout=None):
        path = req.full_url.split("://", 1)[1].split("/", 1)[1]
        r = client.get("/" + path, headers={k: v for k, v in req.header_items()})
        if r.status_code >= 400:
            raise urllib.error.HTTPError(req.full_url, r.status_code, "err", {}, io.BytesIO(r.content))
        return _Resp(r.content)
    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    return transport.HttpTransport("http://testserver", TOKEN)


def test_every_capability_exists_on_every_transport(store):
    local = transport.LocalTransport(store.path)
    for name in service.CAPABILITIES:
        assert callable(getattr(service.Service, name)), name
        assert callable(getattr(local, name)), name
        assert callable(getattr(transport.HttpTransport, name)), f"HttpTransport is missing {name}"


def test_every_capability_has_a_cli_command():
    commands = set(cli.build_parser()._subparsers._group_actions[0].choices)
    for name in service.CAPABILITIES:
        assert cli.COMMAND_FOR[name] in commands, f"the CLI has no command for {name}"
    assert set(cli.COMMAND_FOR) == set(service.CAPABILITIES)


def _strip_ages(payload):
    """Worker ages depend on 'now', which differs by microseconds between two calls."""
    if isinstance(payload, dict):
        return {k: _strip_ages(v) for k, v in payload.items() if k != "age_s"}
    if isinstance(payload, list):
        return [_strip_ages(v) for v in payload]
    return payload


def test_http_and_local_transports_return_identical_data(http, store, seeded):
    local = transport.LocalTransport(store.path)
    rid = seeded.id
    calls = [("summary", (), {}), ("runs", (), {"limit": 10}), ("run", (rid,), {}), ("explain", (rid,), {}),
             ("step_stats", (), {}), ("decisions", (), {}), ("workers", (), {}),
             ("events", (), {"since": 0, "limit": 50}), ("chain", (), {})]
    assert {c[0] for c in calls} == set(service.CAPABILITIES)
    for name, a, kw in calls:
        assert _strip_ages(getattr(http, name)(*a, **kw)) == _strip_ages(getattr(local, name)(*a, **kw)), name


def test_a_missing_run_is_none_on_both_transports(http, store):
    assert http.run("nope") is None and transport.LocalTransport(store.path).run("nope") is None
    assert http.explain("nope") is None


def test_the_http_transport_reports_auth_failures_clearly(http):
    bad = transport.HttpTransport("http://testserver", "wrong")
    with pytest.raises(transport.TransportError, match="HTTP 401"):
        bad.summary()
