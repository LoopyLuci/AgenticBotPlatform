"""bot/shared_context.py — small, named cross-instance markdown docs any
registered agent (any backend) can read/write, independent of any one
instance's own conversation history.
"""

from __future__ import annotations

import pytest

from bot import shared_context


def test_write_then_read_round_trips(temp_db):
    shared_context.write_doc("status", "# Project Status\n\nAll green.", actor="manager")

    doc = shared_context.read_doc("status")

    assert doc["content"] == "# Project Status\n\nAll green."
    assert doc["updated_by"] == "manager"
    assert doc["name"] == "status"


def test_read_missing_doc_returns_none(temp_db):
    assert shared_context.read_doc("does-not-exist") is None


def test_write_overwrites_existing_doc(temp_db):
    shared_context.write_doc("status", "first version", actor="a")
    shared_context.write_doc("status", "second version", actor="b")

    doc = shared_context.read_doc("status")
    assert doc["content"] == "second version"
    assert doc["updated_by"] == "b"


def test_list_docs_reports_size_and_metadata(temp_db):
    shared_context.write_doc("status", "hello", actor="a")
    shared_context.write_doc("notes", "world!!", actor="b")

    docs = {d["name"]: d for d in shared_context.list_docs()}

    assert docs["status"]["size"] == 5
    assert docs["notes"]["size"] == 7
    assert docs["status"]["updated_by"] == "a"


def test_delete_doc_removes_it(temp_db):
    shared_context.write_doc("status", "hello", actor="a")
    removed = shared_context.delete_doc("status")

    assert removed is True
    assert shared_context.read_doc("status") is None


def test_delete_missing_doc_returns_false(temp_db):
    assert shared_context.delete_doc("nope") is False


def test_rejects_empty_name(temp_db):
    with pytest.raises(shared_context.SharedContextError):
        shared_context.write_doc("", "content", actor="a")


def test_rejects_invalid_characters_in_name(temp_db):
    with pytest.raises(shared_context.SharedContextError):
        shared_context.write_doc("has spaces", "content", actor="a")
    with pytest.raises(shared_context.SharedContextError):
        shared_context.write_doc("has/slash", "content", actor="a")


def test_rejects_content_over_the_size_cap(temp_db):
    with pytest.raises(shared_context.SharedContextError):
        shared_context.write_doc("status", "x" * (shared_context.MAX_DOC_CHARS + 1), actor="a")


def test_rejects_new_doc_past_the_doc_count_cap(temp_db, monkeypatch):
    shared_context.delete_doc(shared_context.SEED_DOC_NAME)  # isolate this test's own doc-count math
    monkeypatch.setattr(shared_context, "MAX_DOCS", 2)
    shared_context.write_doc("a", "1", actor="x")
    shared_context.write_doc("b", "2", actor="x")

    with pytest.raises(shared_context.SharedContextError):
        shared_context.write_doc("c", "3", actor="x")

    # Overwriting an EXISTING doc at the cap is still fine — only new docs are capped.
    shared_context.write_doc("a", "updated", actor="x")
    assert shared_context.read_doc("a")["content"] == "updated"


# --------------------------------------------------------------- seed doc


def test_seed_doc_exists_on_a_fresh_db(temp_db):
    # temp_db's fixture calls db.init_db(), which calls seed_default_docs()
    # — a fresh install should always have a working example an agent can
    # discover via list_project_context with zero manual setup.
    doc = shared_context.read_doc(shared_context.SEED_DOC_NAME)
    assert doc is not None
    assert "write_project_context" in doc["content"]
    assert "read_project_context" in doc["content"]
    assert doc["updated_by"] == "system"


def test_seed_doc_is_within_the_size_cap():
    assert len(shared_context.SEED_DOC_CONTENT) <= shared_context.MAX_DOC_CHARS


def test_seed_never_overwrites_a_customized_or_deleted_seed_doc(temp_db):
    # An operator/agent edited the seed doc — re-seeding must never clobber that.
    shared_context.write_doc(shared_context.SEED_DOC_NAME, "my own customized notes", actor="operator")

    shared_context.seed_default_docs()

    assert shared_context.read_doc(shared_context.SEED_DOC_NAME)["content"] == "my own customized notes"


def test_seed_is_idempotent_when_called_again(temp_db):
    shared_context.seed_default_docs()  # already seeded once by temp_db's init_db()
    shared_context.seed_default_docs()  # calling again must not raise or duplicate

    docs = [d for d in shared_context.list_docs() if d["name"] == shared_context.SEED_DOC_NAME]
    assert len(docs) == 1
