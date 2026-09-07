"""Unit tests for bot/backends/ui_backend.py's pure logic — the parts that
don't require a real Windows/pywinauto window. Real end-to-end automation
against Claude Desktop was live-verified manually this session (see the
module's own docstrings for what was confirmed and why); these tests lock
in the specific bugs found and fixed during that verification so they
can't silently regress.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from bot.backends.ui_backend import UiBackend, UNTITLED_SESSION_KEY


def _button(text: str, enabled: bool = True) -> MagicMock:
    btn = MagicMock()
    btn.window_text.return_value = text
    btn.is_enabled.return_value = enabled
    return btn


def _win(buttons: list) -> MagicMock:
    win = MagicMock()
    win.descendants.side_effect = lambda control_type=None, **_: (
        buttons if control_type == "Button" else []
    )
    return win


class TestFindSendButton:
    def test_exact_match_wins_over_send_feedback(self):
        """Real bug found live: Claude Desktop also has a disabled 'Send
        feedback' button elsewhere in the window. The old substring match
        ("send" in name) could find and click that one first, silently
        doing nothing while the real message sat unsent."""
        backend = UiBackend()
        feedback_btn = _button("Send feedback", enabled=False)
        real_send_btn = _button("Send", enabled=True)
        win = _win([feedback_btn, real_send_btn])

        found = backend._find_send_button(win)

        assert found is real_send_btn

    def test_returns_none_when_no_send_button(self):
        backend = UiBackend()
        win = _win([_button("Send feedback", enabled=False)])
        assert backend._find_send_button(win) is None


class TestFindNewChatButton:
    def test_requires_a_project(self):
        """Real finding: the bare 'New' button opens a project/worktree
        picker screen, not a compose view — this backend deliberately
        refuses rather than guessing a worktree choice."""
        backend = UiBackend()
        win = _win([_button("New")])
        try:
            backend._find_new_chat_button(win, project=None)
            assert False, "expected BackendError"
        except Exception as exc:
            assert "desktop_project" in str(exc)

    def test_matches_new_session_in_project(self):
        backend = UiBackend()
        target = _button("New session in PrivateProject")
        win = _win([_button("New"), target, _button("New session in Other")])

        found = backend._find_new_chat_button(win, project="PrivateProject")

        assert found is target

    def test_missing_project_raises_clear_error(self):
        backend = UiBackend()
        win = _win([_button("New session in Other")])
        try:
            backend._find_new_chat_button(win, project="PrivateProject")
            assert False, "expected BackendError"
        except Exception as exc:
            assert "PrivateProject" in str(exc)


class TestSessionButtons:
    def test_strips_idle_and_running_status_prefixes(self):
        """Real finding: sidebar rows are Buttons named f"{status} {title}",
        not a distinct ListItem/TreeItem control type — and a session's
        status (Idle vs Running) shouldn't affect whether it's found by
        its bare title."""
        backend = UiBackend()
        win = _win([
            _button("Idle PrivateProject"),
            _button("Running BotServer"),
            _button("New"),  # not a session row — must be excluded
            _button("More options for PrivateProject"),  # not a session row either
        ])

        items = backend._session_buttons(win)

        assert set(items.keys()) == {"PrivateProject", "BotServer"}


class TestListProjects:
    def test_parses_new_session_in_buttons(self):
        backend = UiBackend()
        win = _win([
            _button("New"),
            _button("New session in PrivateProject"),
            _button("New session in Omnisystem"),
            _button("Search"),
        ])
        backend._connect = lambda: win  # type: ignore[method-assign]

        projects = backend._sync_list_projects()

        assert projects == ["PrivateProject", "Omnisystem"]


class TestCreateSessionSentinel:
    def test_returns_untitled_sentinel(self):
        """Real finding: a brand-new chat has no sidebar entry to read a
        label from until its first message names it — create_session()
        can't return a real key immediately, only this placeholder."""
        backend = UiBackend(poll_interval_s=0.01)
        win = _win([_button("New session in PrivateProject")])
        backend._connect = lambda: win  # type: ignore[method-assign]

        key = backend._sync_create_session(timeout_s=1, project="PrivateProject")

        assert key == UNTITLED_SESSION_KEY
