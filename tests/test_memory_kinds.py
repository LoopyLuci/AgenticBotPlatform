"""Typed long-term memory: kinds, de-duplication, fading, and the commands (roadmap P3)."""
from __future__ import annotations

import asyncio

import pytest

from bot import bot_instances, commands, db, memory
from bot.agent_runtime import prompt, toolspec, tools


@pytest.fixture
def iid(temp_db):
    i = bot_instances.create_instance(
        name="m", platform="telegram", backend="api",
        credentials={"bot_token": "123456789:AAExampleTokenFromBotFather1234"}, allowed_user_ids=[1])
    memory.set_approval_required(i, False, actor="test")
    return i


def texts(iid):
    return [r["content"] for r in memory.listing(iid)]


def test_memories_have_a_kind_and_unknown_kinds_become_facts(iid):
    a = memory.remember_full(iid, "prefers tabs to spaces", kind="feedback")
    b = memory.remember_full(iid, "the deploy script is scripts/ship.sh", kind="reference")
    c = memory.remember_full(iid, "something else entirely", kind="nonsense")
    assert (a["kind"], b["kind"], c["kind"]) == ("feedback", "reference", "fact")
    assert {r["kind"] for r in memory.listing(iid)} == {"feedback", "reference", "fact"}
    assert [r["content"] for r in memory.listing(iid, "reference")] == ["the deploy script is scripts/ship.sh"]


def test_saying_the_same_thing_again_refreshes_instead_of_duplicating(iid):
    first = memory.remember_full(iid, "The user's dog is called Biscuit.", kind="user")
    again = memory.remember_full(iid, "the users dog is called biscuit", kind="user")
    near = memory.remember_full(iid, "The user's dog is called Biscuit!!", kind="user")
    assert not first["duplicate"] and again["duplicate"] and near["duplicate"]
    assert again["id"] == first["id"] == near["id"] and len(memory.listing(iid)) == 1
    assert dict(db.get_memory_entry(first["id"]))["uses"] == 2
    assert not memory.remember_full(iid, "The user's cat is called Mittens.", kind="user")["duplicate"]


def test_duplicates_are_found_among_pending_memories_too(iid):
    memory.set_approval_required(iid, True, actor="test")
    a = memory.remember_full(iid, "pending fact about deploys")
    b = memory.remember_full(iid, "Pending fact about deploys.")
    assert not a["approved"] and b["duplicate"] and len(memory.pending(iid)) == 1


def test_similar_wording_with_different_numbers_is_a_different_memory(iid):
    a = memory.remember_full(iid, "the staging server listens on port 8080")
    b = memory.remember_full(iid, "the staging server listens on port 8081")
    assert not a["duplicate"] and not b["duplicate"] and len(memory.listing(iid)) == 2


def test_rejected_memories_do_not_block_a_fresh_save(iid):
    memory.set_approval_required(iid, True, actor="test")
    a = memory.remember_full(iid, "something a person rejected")
    memory.reject(a["id"])
    assert not memory.remember_full(iid, "something a person rejected")["duplicate"]


def test_the_summary_groups_by_kind(iid):
    memory.remember_full(iid, "is a nurse who works nights", kind="user")
    memory.remember_full(iid, "wants short answers", kind="feedback")
    memory.remember_full(iid, "the project ships monthly", kind="project")
    text = memory.approved_summary(iid)
    assert text.index("About the user") < text.index("How to work with them") < text.index("About the work")
    assert "- wants short answers" in text


def test_old_unconfirmed_memories_fade_from_the_prompt_but_reconfirming_brings_them_back(iid):
    old = memory.remember_full(iid, "an old preference nobody has mentioned lately", kind="feedback")
    topics = ["deploys", "billing", "invoices", "shipping", "returns", "support", "onboarding", "pricing", "hiring", "legal",
              "security", "backups", "logging", "metrics", "alerts", "release", "testing", "design", "copy", "sales",
              "travel", "events", "vendors", "budget", "roadmap", "research", "training", "quality", "compliance", "growth",
              "partners", "hosting", "domains", "email", "analytics"]
    fresh = [memory.remember_full(iid, f"the team's notes about {t} live in the shared handbook", kind="project")["id"] for t in topics]
    conn = db.get_conn()
    conn.execute("UPDATE memory_entries SET created_at = '2020-01-01T00:00:00+00:00' WHERE id = ?", (old["id"],))
    conn.commit()
    assert "old preference" not in memory.approved_summary(iid)
    assert "older memories are not shown" in memory.approved_summary(iid)
    memory.remember_full(iid, "an old preference nobody has mentioned lately", kind="feedback")          # said again
    assert "old preference" in memory.approved_summary(iid)
    assert len(memory.listing(iid)) == 36, "fading never deletes anything"


def test_the_summary_respects_its_size_budget(iid):
    for i in range(40):
        memory.remember_full(iid, f"memory about {['red','blue','green','pink','grey','teal','gold','navy','plum','ruby'][i % 10]} item {i * 7 + 11} " + " ".join(f"w{j}x{i}" for j in range(60)))
    assert len(memory.approved_summary(iid)) <= memory.MAX_SUMMARY_CHARS + 200


def test_only_a_person_can_forget_and_only_within_their_instance(iid, temp_db):
    other = bot_instances.create_instance(
        name="o", platform="telegram", backend="api",
        credentials={"bot_token": "123456789:AAExampleTokenFromBotFather1234"}, allowed_user_ids=[1])
    m = memory.remember_full(iid, "keep this one")
    assert memory.forget(other, m["id"]) is False and texts(iid) == ["keep this one"]
    assert memory.forget(iid, m["id"]) is True and texts(iid) == []
    assert "forget_memory" not in {s["name"] for s in tools.all_tool_schemas()}


def test_save_memory_tool_takes_a_kind_and_reports_duplicates(iid, tmp_path):
    async def call(**inp):
        return await tools.execute_tool("save_memory", inp, workspace=tmp_path, instance_id=iid)

    first = asyncio.run(call(content="prefers dark mode", kind="feedback"))
    second = asyncio.run(call(content="Prefers dark mode.", kind="feedback"))
    assert "feedback" in first and "approved" in first and "Already remembered" in second
    assert len(memory.listing(iid)) == 1
    schema = next(s for s in tools.TOOL_SCHEMAS if s["name"] == "save_memory")
    assert "kind" in schema["input_schema"]["properties"]


def test_the_prompt_includes_typed_memory(iid, tmp_path, monkeypatch):
    memory.remember_full(iid, "the user is called Sam", kind="user")
    monkeypatch.setattr(prompt, "_config", lambda: {"environment": False})
    text = prompt.build(iid, workspace=tmp_path)
    assert "About the user" in text and "the user is called Sam" in text


class Ctx:
    def __init__(self, instance_id):
        self.instance_id, self.user_id = instance_id, 1


def cmd(iid, raw):
    return asyncio.run(commands.cmd_memory(Ctx(iid), raw))


def test_memory_commands_list_add_with_kind_and_forget(iid):
    assert cmd(iid, "list") == "No memories yet."
    assert "(user)" in cmd(iid, "add user: is a vegetarian")
    assert "Already remembered" in cmd(iid, "add user: Is a vegetarian.")
    assert "(fact)" in cmd(iid, "add plain text with no kind")
    assert "(fact)" in cmd(iid, "add note: colons in text are fine")
    listing = cmd(iid, "list")
    assert "[user]" in listing and "[fact]" in listing and "is a vegetarian" in listing
    assert "is a vegetarian" in cmd(iid, "list user") and "plain text" not in cmd(iid, "list user")
    assert cmd(iid, "list reference") == "No reference memories."
    entry = memory.listing(iid, "user")[0]["id"]
    assert cmd(iid, f"forget {entry}") == f"Forgot #{entry}." and cmd(iid, f"forget {entry}") == "Not found."
    assert "forget" in cmd(iid, "bogus")


def test_existing_databases_gain_the_new_columns(temp_db):
    cols = {r["name"] for r in db.get_conn().execute("PRAGMA table_info(memory_entries)")}
    assert {"kind", "uses", "last_used"} <= cols
