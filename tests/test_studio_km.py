"""bot/studio/km: Knowledge Modules from applied Studio changes. The hub is a fake of bot/modelmistress/client.py
(find, call); nothing here reaches a real hub."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from bot.modelmistress import client
from bot.studio import km, log
from bot.studio.variants import StudioError

TOKEN = "sk-" + "A1b2C3d4E5f6G7h8I9j0"
WIN_PATH = "C:" + "\\Users\\alice9\\secret-project\\notes.txt"
F = "bot/dashboard/static/vision-panel.js"


@pytest.fixture
def studio(tmp_path, monkeypatch):
    monkeypatch.setenv("ABP_STUDIO_DIR", str(tmp_path / "studio"))
    return tmp_path


class FakeHub:
    def __init__(self, monkeypatch, state="awaiting_approval"):
        self.calls: list[tuple[str, dict]] = []
        self.state = state
        self.reviews: list = []
        self.hub = client.Hub(url="http://127.0.0.1:1", token="t")
        monkeypatch.setattr(client, "find", lambda peer="", *, refresh=False: self.hub)
        monkeypatch.setattr(client, "call", self.call)

    def call(self, op, args=None, *, hub=None, timeout=120.0):
        self.calls.append((op, dict(args or {})))
        if op == "factory.task.create":
            return {"task": "t-1", "job": "j-1", "state": "running"}
        if op == "factory.task.get":
            return {"id": args["id"], "state": self.state, "steps": {}}
        if op == "factory.review.list":
            return {"items": self.reviews}
        raise AssertionError(op)


def apply_one(vid="aaaa-1111", reverted=False, rating=5):
    log.event("generate", vid=vid, group="g", instruction=f"Make the buttons bigger, token {TOKEN}", files=[F], model="prov/m")
    log.event("generate-edit", vid=vid, file=F, diff=f"--- {F} (live)\n+++ {F} (variant)\n-a\n+b = '{WIN_PATH}'\n")
    log.event("edit", vid=vid, file="bot/other/secret.py", diff="--- bot/other/secret.py\n+PASSWORD_LEAK_ME\n")
    log.event("rate", vid=vid, group="g", rating=rating, why="looks good")
    log.event("gate", vid=vid, passed=True, failed=[], seconds=1)
    log.event("apply", vid=vid, group="g", files=[F, "bot/api/not_ui.py"], backup="b1")
    if reverted:
        log.event("revert", vid=vid, backup="b1", files=[F])


def test_export_documents_and_scrubbing(studio):
    apply_one()
    log.event("generate", vid="bbbb-2222", group="g", instruction="never applied", files=[F])
    res = km.export()
    assert res["documents"] == 1 and res["variants"] == ["aaaa-1111"]
    doc = (Path(res["dir"]) / "aaaa-1111.md").read_text(encoding="utf-8")
    assert "Make the buttons bigger" in doc and "Outcome: kept" in doc and "Rating: 5 of 5 (looks good)" in doc
    assert "Checks: passed" in doc and F in doc and "+b = " in doc
    assert TOKEN not in doc and "alice9" not in doc and "secret-project" not in doc and "C:\\" not in doc
    assert "PASSWORD_LEAK_ME" not in doc and "not_ui.py" not in doc and "bot/other/secret.py" not in doc


def test_export_marks_reverted_and_skips_a_leaking_document(studio, monkeypatch):
    apply_one(reverted=True, rating=1)
    doc = (Path(km.export()["dir"]) / "aaaa-1111.md").read_text(encoding="utf-8")
    assert "reverted afterwards" in doc and "Rating: 1 of 5" in doc
    monkeypatch.setattr(km, "scrub", lambda t: t)           # a scrubber that missed it: the leak guard still stops it
    res = km.export()
    assert res["documents"] == 0 and res["skipped"] == ["aaaa-1111"] and not list(Path(res["dir"]).glob("*.md"))


def test_build_sends_the_task_and_records_it(studio, monkeypatch):
    hub = FakeHub(monkeypatch)
    apply_one()
    res = km.build()
    assert res["task"] == "t-1" and res["approval"] == "propose" and res["plan_approval"] is True
    (op, task), = hub.calls
    assert op == "factory.task.create" and task["start"] is True
    assert task["plan_approval"] is True and task["approval"] == "propose"
    assert task["kinds"] == ["data", "memory"] and task["domain"] == "abp/studio-ui"
    assert task["sources"][0]["kind"] == "dir" and task["sources"][0]["path"] == str(km.source_dir())
    assert task["budget"]["wall_s"] <= 7200 and task["size"]["records"] <= 200
    assert json.loads((km.km_dir() / "tasks.json").read_text(encoding="utf-8"))[0]["task"] == "t-1"


def test_build_refuses_without_a_hub_or_documents(studio, monkeypatch):
    apply_one()
    monkeypatch.setattr(client, "find", lambda peer="", *, refresh=False: None)
    with pytest.raises(StudioError, match="not running"):
        km.build()
    hub = FakeHub(monkeypatch)
    for p in km.source_dir().glob("*.md"):
        p.unlink()
    log_file = studio / "studio" / "log.jsonl"
    log_file.unlink()                                       # nothing applied
    with pytest.raises(StudioError, match="nothing to build"):
        km.build()
    assert hub.calls == []


def test_build_reports_a_refused_task(studio, monkeypatch):
    FakeHub(monkeypatch)
    apply_one()

    def boom(op, args=None, **kw):
        raise client.MMError("bad task", 400)
    monkeypatch.setattr(client, "call", boom)
    with pytest.raises(StudioError, match="refused"):
        km.build()
    assert km.status()["tasks"] == []


def test_status_maps_task_states(studio, monkeypatch):
    hub = FakeHub(monkeypatch)
    apply_one()
    km.build()
    s = km.status()["tasks"][0]
    assert s["state"] == "awaiting_approval" and "Approve the plan" in s["needs"] and s["review_url"] == hub.hub.url
    hub.state, hub.reviews = "done", [{"id": 1}, {"id": 2}]
    s = km.status("t-1")["tasks"][0]
    assert s["state"] == "done" and s["open_reviews"] == 2 and "promotion" in s["needs"]
    hub.state, hub.reviews = "done", []
    assert "candidate" in km.status()["tasks"][0]["needs"]
    hub.state = "budget"
    assert "budget" in km.status()["tasks"][0]["needs"]
    monkeypatch.setattr(client, "find", lambda peer="", *, refresh=False: None)
    s = km.status()
    assert s["hub"] is False and s["tasks"][0]["error"] == "ModelMistress is not running" and s["tasks"][0]["state"] == "budget"


def test_api_routes_and_both_ui_copies(studio, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from bot.dashboard import studio_api
    app = FastAPI()
    studio_api.register(app, lambda: None)
    c = TestClient(app)
    FakeHub(monkeypatch)
    apply_one()
    assert c.post("/api/studio/km/export", json={}).json()["documents"] == 1
    assert c.post("/api/studio/km/build", json={}).json()["task"] == "t-1"
    assert c.get("/api/studio/km").json()["tasks"][0]["task"] == "t-1"
    monkeypatch.setattr(client, "find", lambda peer="", *, refresh=False: None)
    assert c.post("/api/studio/km/build", json={}).status_code == 400
    root = Path(__file__).resolve().parent.parent
    a = (root / "bot/dashboard/static/studio-panel.js").read_bytes()
    assert a == (root / "desktop-app/ui/studio-panel.js").read_bytes() and b"std-km-build" in a
