"""Import from Hermes Agent and OpenCode (bot/localchat/imports.py), against small fake sources: sessions and messages land in
Local Chat marked by source, a re-run adds only what is new (and refreshes a message the source changed), the sources are never
written, secrets never reach the library, and the API is audited."""
from __future__ import annotations

import json
import sqlite3
import time

import pytest
from fastapi.testclient import TestClient

from bot.localchat import imports, store


def _hermes(home):
    home.mkdir(parents=True)
    con = sqlite3.connect(str(home / "state.db"))
    con.executescript("""
        CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT NOT NULL, title TEXT, display_name TEXT, model TEXT,
            parent_session_id TEXT, started_at REAL NOT NULL, ended_at REAL, last_activity_at REAL, archived INTEGER DEFAULT 0);
        CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL, role TEXT NOT NULL, content TEXT,
            tool_calls TEXT, tool_name TEXT, timestamp REAL NOT NULL, token_count INTEGER, active INTEGER NOT NULL DEFAULT 1);
    """)
    con.execute("insert into sessions values ('h1','cli','Fix the build',NULL,'some-model',NULL,100,NULL,200,0)")
    con.execute("insert into sessions values ('h2','subagent',NULL,NULL,'some-model','h1',150,NULL,160,0)")
    rows = [("h1", "user", "Why does the build fail?", None, None, 101), ("h1", "assistant", "", json.dumps([{"function": {"name": "terminal", "arguments": "{}"}}]), None, 102),
            ("h1", "tool", "exit 1", None, "terminal", 103), ("h1", "assistant", "A missing import.", None, None, 104),
            ("h2", "user", "Look at the tests", None, None, 151)]
    for sid, role, content, calls, tool, ts in rows:
        con.execute("insert into messages (session_id, role, content, tool_calls, tool_name, timestamp, token_count) values (?,?,?,?,?,?,?)",
                    (sid, role, content, calls, tool, ts, 7))
    con.commit()
    con.close()
    (home / "memories").mkdir()
    (home / "memories" / "MEMORY.md").write_text("The owner prefers short answers.\n§\nTests run with pytest.\n", encoding="utf-8")
    (home / "skills" / "make-tea").mkdir(parents=True)
    (home / "skills" / "make-tea" / "SKILL.md").write_text("# Make tea\nBoil water.\n", encoding="utf-8")
    (home / "config.yaml").write_text("model:\n  default: some-model\n  provider: example\n  api_key: SECRET-VALUE-1\n"
                                      "providers:\n  example:\n    base_url: https://example.invalid/v1\n    token: SECRET-VALUE-2\n",
                                      encoding="utf-8")


def _opencode(data, conf):
    (data / "opencode").mkdir(parents=True)
    con = sqlite3.connect(str(data / "opencode" / "opencode.db"))
    con.executescript("""
        CREATE TABLE session (id TEXT PRIMARY KEY, parent_id TEXT, title TEXT NOT NULL, time_created INTEGER NOT NULL,
            time_updated INTEGER NOT NULL, time_archived INTEGER);
        CREATE TABLE message (id TEXT PRIMARY KEY, session_id TEXT NOT NULL, time_created INTEGER NOT NULL, time_updated INTEGER NOT NULL,
            data TEXT NOT NULL);
        CREATE TABLE part (id TEXT PRIMARY KEY, message_id TEXT NOT NULL, session_id TEXT NOT NULL, time_created INTEGER NOT NULL,
            time_updated INTEGER NOT NULL, data TEXT NOT NULL);
    """)
    con.execute("insert into session values ('o1', NULL, 'Rename a function', 1000, 5000, NULL)")
    con.execute("insert into message values ('m1','o1',1000,1000,?)", (json.dumps({"role": "user"}),))
    con.execute("insert into message values ('m2','o1',2000,2000,?)", (json.dumps({"role": "assistant", "modelID": "coder-x"}),))
    con.execute("insert into part values ('p1','m1','o1',1000,1000,?)", (json.dumps({"type": "text", "text": "Rename foo to bar"}),))
    con.execute("insert into part values ('p2','m2','o1',2000,2000,?)", (json.dumps({"type": "text", "text": "Renamed"}),))
    con.execute("insert into part values ('p3','m2','o1',2001,2001,?)",
                (json.dumps({"type": "tool", "tool": "edit", "state": {"status": "completed", "title": "a.py", "output": "ok"}}),))
    con.commit()
    con.close()
    (conf / "opencode").mkdir(parents=True)
    (conf / "opencode" / "opencode.jsonc").write_text('{\n // comment\n "model": "example/coder-x",\n'
                                                      ' "provider": {"example": {"options": {"apiKey": "SECRET-VALUE-3", "baseURL": "https://example.invalid"}}},\n}',
                                                      encoding="utf-8")


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("ABP_AGENT_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "conf"))
    _hermes(tmp_path / "hermes")
    _opencode(tmp_path / "data", tmp_path / "conf")
    cfg = {"include_tools": False, **{s: {"enabled": True, "path": "", **{k: True for k in imports.KINDS}} for s in imports.SOURCES}}
    cfg["hermes"]["path"] = str(tmp_path / "hermes")
    monkeypatch.setattr(imports, "settings", lambda: json.loads(json.dumps(cfg)))
    return {"cfg": cfg, "tmp": tmp_path}


def test_import_maps_sessions_and_is_idempotent(env):
    before = (env["tmp"] / "hermes" / "state.db").read_bytes()
    imports.run()
    hs = store.list_sessions(source="hermes")
    assert {s["title"] for s in hs} == {"Fix the build", "Look at the tests"}
    h1 = next(s for s in hs if s["title"] == "Fix the build")
    assert h1["id"] == imports.session_id("hermes", "h1") and h1["group_id"] == "imp-hermes" and h1["autonomy"] == 0
    child = next(s for s in hs if s["title"] == "Look at the tests")
    assert child["parent_id"] == h1["id"]
    msgs = store.messages(h1["id"])
    assert [(m["role"], m["text"]) for m in msgs] == [("user", "Why does the build fail?"), ("assistant", "A missing import.")]
    oc = store.list_sessions(source="opencode")
    assert [s["title"] for s in oc] == ["Rename a function"]
    assert [(m["role"], m["text"], m["model"]) for m in store.messages(oc[0]["id"])] == [("user", "Rename foo to bar", ""),
                                                                                        ("assistant", "Renamed", "coder-x")]
    assert store.list_sessions(source="") == []                           # nothing counted as made here

    imports.run()                                                          # a re-run adds nothing
    imports.run(full=True)                                                 # nor does a full re-scan
    assert imports.imported_counts() == {"hermes": {"sessions": 2, "messages": 3}, "opencode": {"sessions": 1, "messages": 2}}
    assert (env["tmp"] / "hermes" / "state.db").read_bytes() == before     # the source is never written


def test_reimport_adds_only_new_and_refreshes_changed(env):
    imports.run()
    con = sqlite3.connect(str(env["tmp"] / "hermes" / "state.db"))
    con.execute("insert into messages (session_id, role, content, timestamp) values ('h1','user','And the tests?',300)")
    con.commit()
    con.close()
    con = sqlite3.connect(str(env["tmp"] / "data" / "opencode" / "opencode.db"))
    con.execute("update part set data=?, time_updated=9000 where id='p2'", (json.dumps({"type": "text", "text": "Renamed foo to bar"}),))
    con.commit()
    con.close()
    imports.run()
    h1 = imports.session_id("hermes", "h1")
    assert [m["text"] for m in store.messages(h1)][-1] == "And the tests?"
    assert len(store.messages(h1)) == 3
    oc = imports.session_id("opencode", "o1")
    assert [m["text"] for m in store.messages(oc)] == ["Rename foo to bar", "Renamed foo to bar"]


def test_owner_edits_survive_a_reimport(env):
    imports.run()
    sid = imports.session_id("hermes", "h1")
    store.update(sid, title="My title", pinned=True)
    imports.run(full=True)
    s = store.get(sid)
    assert s["title"] == "My title" and s["pinned"]


def test_tools_only_when_switched_on(env, monkeypatch):
    env["cfg"]["include_tools"] = True
    imports.run()
    texts = [m["text"] for m in store.messages(imports.session_id("hermes", "h1"))]
    assert "Tool calls: terminal" in texts and any(t.startswith("[terminal result]") for t in texts)
    assert any(m["role"] == "system" and m["text"].startswith("[edit]") for m in store.messages(imports.session_id("opencode", "o1")))


def test_library_has_no_secrets(env):
    imports.run()
    lib = imports.library()
    assert [m["text"] for m in lib["hermes"]["memories"]] == ["The owner prefers short answers.", "Tests run with pytest."]
    assert lib["hermes"]["skills"] == ["make-tea"]
    assert lib["hermes"]["config"]["model"] == {"default": "some-model", "provider": "example"}
    assert lib["opencode"]["config"]["model"] == "example/coder-x"
    dump = json.dumps(lib)
    assert "SECRET-VALUE" not in dump and "https://example.invalid" in dump
    imports.run()
    assert len(imports.library()["hermes"]["memories"]) == 2


def test_disabled_source_and_kind_are_skipped(env):
    env["cfg"]["opencode"]["enabled"] = False
    env["cfg"]["hermes"]["skills"] = False
    imports.run()
    assert store.list_sessions(source="opencode") == []
    assert imports.library()["hermes"]["skills"] == []


def test_preview_counts(env):
    p = imports.preview()["sources"]
    assert p["hermes"]["found"] and p["hermes"]["sessions"]["total"] == 2 and p["hermes"]["memories"] == {"total": 2, "new": 2}
    assert p["opencode"]["found"] and p["opencode"]["sessions"]["total"] == 1
    imports.run()
    p = imports.preview()["sources"]
    assert p["hermes"]["imported"]["sessions"] == 2 and p["hermes"]["memories"]["new"] == 0 and p["hermes"]["skills"]["new"] == 0


def test_setting_changes_validate():
    assert imports.setting_changes({"hermes": {"enabled": False, "path": " /home/u/.hermes "}, "include_tools": True}) == {
        ("local_chat", "import", "hermes", "enabled"): False, ("local_chat", "import", "hermes", "path"): "/home/u/.hermes",
        ("local_chat", "import", "include_tools"): True}
    for bad in ({}, {"nope": 1}, {"hermes": {"enabled": "yes"}}, {"hermes": {"colour": True}}, {"include_tools": 1}):
        with pytest.raises(ValueError):
            imports.setting_changes(bad)


def test_api_runs_and_audits(env, temp_db, monkeypatch):
    monkeypatch.setenv("DASHBOARD_TOKEN", "test-token")
    from bot import db
    from bot.config import config
    from bot.dashboard.server import build_app

    written = []
    monkeypatch.setattr(config, "set_values", lambda changes, actor="dashboard": written.append(dict(changes)))
    c, h = TestClient(build_app()), {"X-Dashboard-Token": "test-token"}
    d = c.get("/api/local-chat/import", headers=h).json()
    assert d["preview"]["sources"]["hermes"]["found"]
    assert c.post("/api/local-chat/import/run", json={}).status_code in (401, 403)      # starting work needs the token
    r = c.post("/api/local-chat/import/run", json={"sources": ["hermes"]}, headers=h)
    assert r.status_code == 200
    for _ in range(200):
        if not imports.status()["running"]:
            break
        time.sleep(0.05)
    assert imports.status()["last"]["phase"] == "done"
    assert len(c.get("/api/local-chat/sessions?source=hermes", headers=h).json()["sessions"]) == 2
    assert c.post("/api/local-chat/import/run", json={"sources": ["nope"]}, headers=h).status_code == 400
    assert c.put("/api/local-chat/import", json={"bad": 1}, headers=h).status_code == 400
    assert c.put("/api/local-chat/import", json={"opencode": {"enabled": False}}, headers=h).status_code == 200
    assert written == [{("local_chat", "import", "opencode", "enabled"): False}]
    actions = [r["action"] for r in db.list_audit_log(limit=50)]
    assert "import_run" in actions and "import_settings_set" in actions
