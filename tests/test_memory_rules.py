"""Tool-scoped rules and goals (bot/memoryfabric/rules.py): edicts become critical rules pinned in every system
prompt, repeated failures become observations, goals live in the vault and every prompt."""
from __future__ import annotations

import time

import pytest


@pytest.fixture
def r(temp_db):
    from bot.memoryfabric import rules
    rules._ready_for = None
    return rules


def test_rules_put_list_and_reach_the_prompt(r):
    a = r.put_rule("run_shell", "Never run rm -rf outside the workspace.", "critical", "user_explicit", ["safety"])
    b = r.put_rule("run_shell", "Prefer ripgrep to grep.", "normal")
    again = r.put_rule("run_shell", "never run rm -rf outside the workspace.", "critical")
    assert again["id"] == a["id"]                                         # the same rule, updated not duplicated
    assert [x["id"] for x in r.list_rules("run_shell")] == [a["id"], b["id"]]
    text = r.rules_for_prompt()
    assert "[critical] run_shell: never run rm -rf" in text and "ripgrep" not in text   # the newest wording; normal ones stay out
    with pytest.raises(ValueError, match="priority"):
        r.put_rule("x", "y", "urgent")
    assert r.delete_rule(b["id"]) and not r.delete_rule(b["id"])
    from bot.agent_runtime import prompt
    names = [n for n, _ in prompt.sections(None)]
    assert "tool_rules" in names


def test_edicts_land_on_the_tool_they_name(r):
    got = r.capture_edicts("Thanks! Never use the shell to delete files. Don't write_file into /etc please. "
                           "Can you never do that?", tools_used=["read_file"])
    assert [(x["tool"], x["priority"], x["source"]) for x in got] == [("run_shell", "critical", "user_explicit"),
                                                                      ("write_file", "critical", "user_explicit")]
    assert r.capture_edicts("Stop being so verbose.", tools_used=["list_dir"])[0]["tool"] == "list_dir"
    assert r.capture_edicts("Stop being so verbose.") == []                # no tool named, none used: nothing to attach it to


def test_repeated_failures_become_observations(r):
    assert r.note_failures({"web_fetch": ["HTTPError: 404"]}) == []
    got = r.note_failures({"web_fetch": ["HTTPError: 404", "HTTPError: 500"]})
    assert got[0]["priority"] == "normal" and "Failed 2 times" in got[0]["rule"] and "HTTPError" in got[0]["rule"]


def test_goals_in_the_prompt_and_the_vault(r, tmp_path, monkeypatch):
    g = r.put_goal("Ship ABP 1.0 by December")
    r.put_goal("Keep the NAS backups green", "paused")
    assert "Ship ABP 1.0" in r.goals_for_prompt() and "(paused)" in r.goals_for_prompt()
    r.write_goals_file()
    from bot.memoryfabric import vault
    path = vault.root() / "goals.md"
    assert f"<!-- {g['id']} -->" in path.read_text()
    assert r.read_goals_file() == {"changed": 0}
    time.sleep(0.05)
    text = path.read_text().replace(f"- [ ] Ship ABP 1.0 by December <!-- {g['id']} -->", f"- [x] Ship ABP 1.0 by December <!-- {g['id']} -->")
    path.write_text(text + "- [ ] Learn Rust properly\n")
    assert r.read_goals_file()["changed"] == 2
    assert {x["text"] for x in r.goals()} == {"Keep the NAS backups green", "Learn Rust properly"}
    assert next(x for x in r.goals(True) if x["id"] == g["id"])["status"] == "done"
    for i in range(10):
        r.put_goal(f"goal {i}")
    with pytest.raises(ValueError, match="at most"):
        r.put_goal("one too many")


def test_the_routes(r):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from bot.dashboard import memory_api
    app = FastAPI()
    memory_api.register(app, lambda: None)
    c = TestClient(app)
    rule = c.post("/api/memory/tool-rules", json={"tool": "send_email", "rule": "Never email Sarah.", "priority": "critical"}).json()
    assert c.get("/api/memory/tool-rules", params={"tool": "send_email"}).json()[0]["id"] == rule["id"]
    assert c.post("/api/memory/tool-rules", json={"tool": "", "rule": "x"}).status_code == 400
    assert c.delete(f"/api/memory/tool-rules/{rule['id']}").json() == {"deleted": rule["id"]}
    assert c.delete(f"/api/memory/tool-rules/{rule['id']}").status_code == 404
    goal = c.post("/api/memory/goals", json={"text": "Run a marathon"}).json()
    assert [x["text"] for x in c.get("/api/memory/goals").json()] == ["Run a marathon"]
    assert c.delete(f"/api/memory/goals/{goal['id']}").json() == {"deleted": goal["id"]}
