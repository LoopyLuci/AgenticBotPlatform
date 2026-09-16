"""/desktop_projects (bot.commands.cmd_desktop_projects) and
Router.link_existing_desktop_session — browsing and continuing REAL,
already-existing Claude Desktop projects/chats, as opposed to /sessions
and /resume which only know about sessions AgenticBotPlatform itself created via
/new. See bot/backends/ui_backend.py's list_projects_with_sessions() for
how project/session grouping is inferred from the live sidebar.
"""
from __future__ import annotations

import asyncio

import pytest

from bot import bot_instances, commands, db
from bot.backends.base import BackendError
from bot.commands import CmdContext
from bot.router import router


def _run(coro):
    return asyncio.run(coro)


def _ctx(instance_id):
    return CmdContext(instance_id=instance_id, instance_name="test", user_id=1, chat_id=42, actor="test")


def _make_ui_instance(temp_db):
    return bot_instances.create_instance(
        name="ui-bot", platform="telegram", backend="ui",
        credentials={"bot_token": "123456789:AAExampleTokenFromBotFatherPadding123"},
        allowed_user_ids=[1],
    )


class TestCmdDesktopProjectsListing:
    def test_lists_projects_with_chat_counts(self, temp_db, monkeypatch):
        iid = _make_ui_instance(temp_db)

        async def fake_grouped():
            return {"Kestrion": ["Fix the login bug", "Refactor the parser"], "TridentDroid": []}

        monkeypatch.setattr(router, "list_desktop_projects_with_sessions", fake_grouped)

        reply = _run(commands.cmd_desktop_projects(_ctx(iid), []))

        assert "Kestrion (2 chats)" in reply
        assert "TridentDroid (0 chats)" in reply

    def test_lists_chats_within_one_project(self, temp_db, monkeypatch):
        iid = _make_ui_instance(temp_db)

        async def fake_grouped():
            return {"Kestrion": ["Fix the login bug", "Refactor the parser"]}

        monkeypatch.setattr(router, "list_desktop_projects_with_sessions", fake_grouped)

        reply = _run(commands.cmd_desktop_projects(_ctx(iid), ["Kestrion"]))

        assert "1. Fix the login bug" in reply
        assert "2. Refactor the parser" in reply

    def test_unknown_project_is_a_clear_error(self, temp_db, monkeypatch):
        iid = _make_ui_instance(temp_db)

        async def fake_grouped():
            return {"Kestrion": []}

        monkeypatch.setattr(router, "list_desktop_projects_with_sessions", fake_grouped)

        reply = _run(commands.cmd_desktop_projects(_ctx(iid), ["NoSuchProject"]))

        assert "no project named" in reply.lower()

    def test_non_ui_backend_is_rejected(self, temp_db):
        iid = bot_instances.create_instance(
            name="api-bot", platform="telegram", backend="api",
            credentials={"bot_token": "123456789:AAExampleTokenFromBotFatherPadding123"},
            allowed_user_ids=[1],
        )
        reply = _run(commands.cmd_desktop_projects(_ctx(iid), []))
        assert "only applies to" in reply.lower()

    def test_backend_error_is_surfaced_not_raised(self, temp_db, monkeypatch):
        iid = _make_ui_instance(temp_db)

        async def fake_grouped():
            raise BackendError("Claude Desktop isn't running")

        monkeypatch.setattr(router, "list_desktop_projects_with_sessions", fake_grouped)

        reply = _run(commands.cmd_desktop_projects(_ctx(iid), []))

        assert "Claude Desktop isn't running" in reply


class TestCmdDesktopProjectsContinue:
    def test_continuing_a_chat_by_index_links_it(self, temp_db, monkeypatch):
        iid = _make_ui_instance(temp_db)

        async def fake_grouped():
            return {"Kestrion": ["Fix the login bug", "Refactor the parser"]}

        linked = {}

        async def fake_link(instance_id, chat_id, session_title, thread_id=None):
            linked["args"] = (instance_id, chat_id, session_title, thread_id)

        monkeypatch.setattr(router, "list_desktop_projects_with_sessions", fake_grouped)
        monkeypatch.setattr(router, "link_existing_desktop_session", fake_link)

        reply = _run(commands.cmd_desktop_projects(_ctx(iid), ["Kestrion", "2"]))

        assert "Refactor the parser" in reply
        assert linked["args"] == (iid, 42, "Refactor the parser", None)

    def test_out_of_range_index_is_a_clear_error(self, temp_db, monkeypatch):
        iid = _make_ui_instance(temp_db)

        async def fake_grouped():
            return {"Kestrion": ["Fix the login bug"]}

        monkeypatch.setattr(router, "list_desktop_projects_with_sessions", fake_grouped)

        reply = _run(commands.cmd_desktop_projects(_ctx(iid), ["Kestrion", "5"]))

        assert "only has 1 chat" in reply.lower()


class TestLinkExistingDesktopSession:
    def test_rejects_a_title_not_in_the_live_listing(self, temp_db, monkeypatch):
        iid = _make_ui_instance(temp_db)

        async def fake_grouped():
            return {"Kestrion": ["Fix the login bug"]}

        monkeypatch.setattr(router, "list_desktop_projects_with_sessions", fake_grouped)

        with pytest.raises(BackendError, match="isn't a currently-visible"):
            _run(router.link_existing_desktop_session(iid, 42, "Some Made Up Title"))

    def test_rejects_a_non_ui_instance(self, temp_db):
        iid = bot_instances.create_instance(
            name="api-bot", platform="telegram", backend="api",
            credentials={"bot_token": "123456789:AAExampleTokenFromBotFatherPadding123"},
            allowed_user_ids=[1],
        )
        with pytest.raises(BackendError, match="ui"):
            _run(router.link_existing_desktop_session(iid, 42, "Fix the login bug"))

    def test_links_a_real_title(self, temp_db, monkeypatch):
        iid = _make_ui_instance(temp_db)

        async def fake_grouped():
            return {"Kestrion": ["Fix the login bug"]}

        monkeypatch.setattr(router, "list_desktop_projects_with_sessions", fake_grouped)

        _run(router.link_existing_desktop_session(iid, 42, "Fix the login bug"))

        session = db.get_active_chat_session(iid, 42)
        assert session["desktop_session_key"] == "Fix the login bug"
