"""Unit tests for bot/backends/ui_backend.py's pure logic — the parts that
don't require a real Windows/pywinauto window. Real end-to-end automation
against Claude Desktop was live-verified manually this session (see the
module's own docstrings for what was confirmed and why); these tests lock
in the specific bugs found and fixed during that verification so they
can't silently regress.
"""

from __future__ import annotations

import sys
from unittest.mock import MagicMock

from bot.backends.ui_backend import UiBackend, UNTITLED_SESSION_KEY, _type_text_via_clipboard


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


class _FakeWin32Clipboard:
    """Stands in for the real win32clipboard module — enough surface for
    _type_text_via_clipboard's open/get/empty/set/close sequence."""

    CF_UNICODETEXT = 13

    def __init__(self, initial_contents=None):
        self._contents = initial_contents
        self.set_calls = []
        self.open_count = 0

    def OpenClipboard(self):
        self.open_count += 1

    def CloseClipboard(self):
        pass

    def GetClipboardData(self, fmt):
        if self._contents is None:
            raise RuntimeError("nothing on the clipboard")
        return self._contents

    def EmptyClipboard(self):
        self._contents = None

    def SetClipboardData(self, fmt, value):
        self.set_calls.append(value)
        self._contents = value


class TestTypeTextViaClipboard:
    def test_pastes_via_ctrl_v_not_keystrokes(self, monkeypatch):
        """Real bug this replaces: field.type_keys(kaomoji_text) failed
        outright with "[Errno 22] Invalid argument" — confirmed live
        against the real Claude Desktop window the moment a
        custom_instructions value containing kaomoji was sent through
        it. Pasting hands the OS the whole string at once instead of
        simulating it keystroke-by-keystroke."""
        fake_clipboard = _FakeWin32Clipboard(initial_contents="previous clipboard contents")
        monkeypatch.setitem(sys.modules, "win32clipboard", fake_clipboard)
        field = MagicMock()

        _type_text_via_clipboard(field, "hello (づ｡◕‿‿◕｡)づ")

        assert fake_clipboard.set_calls[0] == "hello (づ｡◕‿‿◕｡)づ"
        field.type_keys.assert_called_once_with("^v")

    def test_restores_the_original_clipboard_contents_afterward(self, monkeypatch):
        fake_clipboard = _FakeWin32Clipboard(initial_contents="what the user actually had copied")
        monkeypatch.setitem(sys.modules, "win32clipboard", fake_clipboard)
        field = MagicMock()

        _type_text_via_clipboard(field, "some prompt text")

        assert fake_clipboard.set_calls[-1] == "what the user actually had copied"

    def test_restores_empty_clipboard_when_there_was_nothing_before(self, monkeypatch):
        fake_clipboard = _FakeWin32Clipboard(initial_contents=None)
        monkeypatch.setitem(sys.modules, "win32clipboard", fake_clipboard)
        field = MagicMock()

        _type_text_via_clipboard(field, "some prompt text")

        # Only the prompt itself was ever set — nothing to restore since
        # there was nothing real on the clipboard beforehand.
        assert fake_clipboard.set_calls == ["some prompt text"]
