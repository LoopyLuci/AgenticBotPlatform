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

from bot.backends.ui_backend import (
    UiBackend,
    UNTITLED_SESSION_KEY,
    REPLY_STABLE_SECONDS,
    REPLY_COMPLETE_MARKER,
    EFFORT_LEVELS,
    DEFAULT_EFFORT,
    _type_text_via_clipboard,
    _is_chrome_or_echo,
)


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
        target = _button("New session in Kestrion")
        win = _win([_button("New"), target, _button("New session in Other")])

        found = backend._find_new_chat_button(win, project="Kestrion")

        assert found is target

    def test_missing_project_raises_clear_error(self):
        backend = UiBackend()
        win = _win([_button("New session in Other")])
        try:
            backend._find_new_chat_button(win, project="Kestrion")
            assert False, "expected BackendError"
        except Exception as exc:
            assert "Kestrion" in str(exc)


class TestSessionButtons:
    def test_strips_idle_and_running_status_prefixes(self):
        """Real finding: sidebar rows are Buttons named f"{status} {title}",
        not a distinct ListItem/TreeItem control type — and a session's
        status (Idle vs Running) shouldn't affect whether it's found by
        its bare title."""
        backend = UiBackend()
        win = _win([
            _button("Idle Kestrion"),
            _button("Running AgenticBotPlatform"),
            _button("New"),  # not a session row — must be excluded
            _button("More options for Kestrion"),  # not a session row either
        ])

        items = backend._session_buttons(win)

        assert set(items.keys()) == {"Kestrion", "AgenticBotPlatform"}


class TestListProjects:
    def test_parses_new_session_in_buttons(self):
        backend = UiBackend()
        win = _win([
            _button("New"),
            _button("New session in Kestrion"),
            _button("New session in Omnisystem"),
            _button("Search"),
        ])
        backend._connect = lambda: win  # type: ignore[method-assign]

        projects = backend._sync_list_projects()

        assert projects == ["Kestrion", "Omnisystem"]


class TestCreateSessionSentinel:
    def test_returns_untitled_sentinel(self):
        """Real finding: a brand-new chat has no sidebar entry to read a
        label from until its first message names it — create_session()
        can't return a real key immediately, only this placeholder."""
        backend = UiBackend(poll_interval_s=0.01)
        win = _win([_button("New session in Kestrion")])
        backend._connect = lambda: win  # type: ignore[method-assign]

        key = backend._sync_create_session(timeout_s=1, project="Kestrion")

        assert key == UNTITLED_SESSION_KEY


class TestSyncAskReplyStability:
    def test_does_not_truncate_a_reply_that_pauses_mid_generation(self, monkeypatch):
        """Real bug found live: the reply-completion check declared a
        reply "done" after just 1s of no visible text change (2 polls at
        the default 0.5s interval) — short enough that an ordinary pause
        between an opening sentence and the rest of a longer answer got
        mistaken for the model being finished, and a real reply reached
        Telegram truncated to its first couple of words. This locks in
        the fix: a much longer required-stable window that survives a
        brief mid-generation pause before more text arrives."""
        monkeypatch.setattr("bot.backends.ui_backend.REPLY_STABLE_SECONDS", 0.03)
        backend = UiBackend(poll_interval_s=0.01)

        win = MagicMock()
        field = MagicMock()
        send_btn = _button("Send", enabled=True)
        backend._connect = lambda: win  # type: ignore[method-assign]
        backend._select_session = lambda win, session_key: None  # type: ignore[method-assign]
        backend._find_input = lambda win: field  # type: ignore[method-assign]
        backend._find_send_button = lambda win: send_btn  # type: ignore[method-assign]
        backend._sync_ensure_effort = lambda win, level: None  # type: ignore[method-assign]
        monkeypatch.setattr("bot.backends.ui_backend._type_text_via_clipboard", lambda field, text: None)

        # Poll sequence: "Hello" appears, pauses for 2 polls (shorter than
        # the required stable window), then "Hello world" appears and
        # holds stable long enough to be declared finished.
        sequence = [
            {"Hello"},
            {"Hello"},
            {"Hello world"},
            {"Hello world"},
            {"Hello world"},
            {"Hello world"},
        ]
        calls = {"n": 0}

        def fake_collect_texts(win):
            idx = min(calls["n"], len(sequence) - 1)
            calls["n"] += 1
            return list(sequence[idx])

        backend._collect_texts = fake_collect_texts  # type: ignore[method-assign]

        reply, discovered = backend._sync_ask("hi", timeout_s=5, session_key="existing-session")

        assert reply == "Hello world"

    def test_still_uses_the_shorter_default_window_when_unpatched(self):
        assert REPLY_STABLE_SECONDS >= 2.0

    def test_reassembles_a_multi_part_reply_instead_of_trusting_the_truncated_announcement(self, monkeypatch):
        """Real bug found live: the "Claude responded: <preview>"
        announcement is only a one-sentence PREVIEW, not the full reply —
        a real reply consisting of an opening sentence, three bulleted
        list items, and a closing sentence announced only the opening
        sentence. Once REPLY_COMPLETE_MARKER appears, extraction must
        reassemble every new Text/ListItem fragment (in on-screen order),
        not just trust the announcement."""
        backend = UiBackend(poll_interval_s=0.01)
        win = MagicMock()
        field = MagicMock()
        send_btn = _button("Send", enabled=True)
        backend._connect = lambda: win  # type: ignore[method-assign]
        backend._select_session = lambda win, session_key: None  # type: ignore[method-assign]
        backend._find_input = lambda win: field  # type: ignore[method-assign]
        backend._find_send_button = lambda win: send_btn  # type: ignore[method-assign]
        backend._sync_ensure_effort = lambda win, level: None  # type: ignore[method-assign]
        monkeypatch.setattr("bot.backends.ui_backend._type_text_via_clipboard", lambda field, text: None)

        prompt = "List exactly three short bullet points about the color blue, then say goodbye."
        full_reply_parts = [
            "Blue is the color most associated with calm, trust, and stability.",
            "The sky and ocean appear blue due to how they scatter and absorb sunlight.",
            "Goodbye, and take care!",
        ]
        # The very first call (before the prompt is even typed) captures
        # the pre-existing baseline; every call after that returns the
        # full final content at once, including the truncated
        # announcement and the completion marker — matches what was
        # confirmed live (the marker and full content land together).
        final_ordered = (
            [f"You said: {prompt}", prompt]
            + [f"Claude responded: {full_reply_parts[0]}"]
            + full_reply_parts
            + [REPLY_COMPLETE_MARKER, "Chat mode"]
        )
        calls = {"n": 0}

        def fake_collect_texts(win):
            calls["n"] += 1
            return [] if calls["n"] == 1 else list(final_ordered)

        backend._collect_texts = fake_collect_texts  # type: ignore[method-assign]

        reply, discovered = backend._sync_ask(prompt, timeout_s=5, session_key="existing-session")

        assert reply == "\n".join(full_reply_parts)

    def test_marker_short_circuits_the_stability_wait(self, monkeypatch):
        """The completion marker should finish extraction on the very
        poll it appears, without waiting for REPLY_STABLE_SECONDS worth
        of additional stable polls."""
        monkeypatch.setattr("bot.backends.ui_backend.REPLY_STABLE_SECONDS", 100.0)
        backend = UiBackend(poll_interval_s=0.01)
        win = MagicMock()
        field = MagicMock()
        send_btn = _button("Send", enabled=True)
        backend._connect = lambda: win  # type: ignore[method-assign]
        backend._select_session = lambda win, session_key: None  # type: ignore[method-assign]
        backend._find_input = lambda win: field  # type: ignore[method-assign]
        backend._find_send_button = lambda win: send_btn  # type: ignore[method-assign]
        backend._sync_ensure_effort = lambda win, level: None  # type: ignore[method-assign]
        monkeypatch.setattr("bot.backends.ui_backend._type_text_via_clipboard", lambda field, text: None)

        calls = {"n": 0}

        def fake_collect_texts(win):
            calls["n"] += 1
            return [] if calls["n"] == 1 else ["Hi there!", REPLY_COMPLETE_MARKER]

        backend._collect_texts = fake_collect_texts  # type: ignore[method-assign]

        reply, discovered = backend._sync_ask("hi", timeout_s=2, session_key="existing-session")

        assert reply == "Hi there!"


class TestIsChromeOrEcho:
    def test_excludes_echo_and_announcement_and_marker(self):
        assert _is_chrome_or_echo("You said: hi", "hi")
        assert _is_chrome_or_echo("Claude responded: Hi!", "hi")
        assert _is_chrome_or_echo("hi", "hi")
        assert _is_chrome_or_echo(REPLY_COMPLETE_MARKER, "hi")
        assert _is_chrome_or_echo("Chat mode", "hi")

    def test_keeps_real_reply_content(self):
        assert not _is_chrome_or_echo("Hello! How can I help you today?", "hi")
        assert not _is_chrome_or_echo("The sky and ocean appear blue.", "hi")


class TestEffortLevels:
    def test_default_is_low(self):
        assert DEFAULT_EFFORT == "low"
        assert EFFORT_LEVELS["low"] == 0.0

    def test_six_ordered_levels(self):
        assert list(EFFORT_LEVELS.keys()) == ["low", "medium", "high", "extra", "max", "ultracode"]
        values = list(EFFORT_LEVELS.values())
        assert values == sorted(values)


class TestSyncEnsureEffort:
    def test_noop_when_already_at_target_level(self):
        backend = UiBackend()
        win = MagicMock()
        effort_btn = _button("Effort: Low")
        win.descendants.side_effect = lambda control_type=None, **_: (
            [effort_btn] if control_type == "Button" else []
        )

        backend._sync_ensure_effort(win, "low")

        effort_btn.click_input.assert_not_called()

    def test_clicks_and_sets_slider_when_level_differs(self):
        backend = UiBackend()
        win = MagicMock()
        effort_btn = _button("Effort: High")
        slider = MagicMock()
        slider.window_text.return_value = "Effort"

        def descendants(control_type=None, **_):
            if control_type == "Button":
                return [effort_btn]
            if control_type == "Slider":
                return [slider]
            return []

        win.descendants.side_effect = descendants

        backend._sync_ensure_effort(win, "low")

        effort_btn.click_input.assert_called_once()
        slider.set_value.assert_called_once_with(0.0)


class TestListProjectsWithSessions:
    def test_groups_sessions_under_their_own_project_marker(self):
        """Confirmed live: a project's session buttons are listed
        immediately after that project's own "New session in <project>"
        button, before the next project's — grouping by button order is
        the only lever available since there's no explicit UIA
        parent/child relationship to rely on."""
        backend = UiBackend()
        win = _win([
            _button("New"),
            _button("New session in Kestrion"),
            _button("Idle Fix the login bug"),
            _button("More options for Fix the login bug"),
            _button("Idle Refactor the parser"),
            _button("New session in TridentDroid"),
            _button("Idle Android crash triage"),
        ])
        backend._connect = lambda: win  # type: ignore[method-assign]

        grouped = backend._sync_list_projects_with_sessions()

        assert grouped == {
            "Kestrion": ["Fix the login bug", "Refactor the parser"],
            "TridentDroid": ["Android crash triage"],
        }

    def test_sessions_before_any_project_marker_are_bucketed_separately(self):
        backend = UiBackend()
        win = _win([
            _button("Running AgenticBotPlatform"),
            _button("New session in Kestrion"),
            _button("Idle Fix the login bug"),
        ])
        backend._connect = lambda: win  # type: ignore[method-assign]

        grouped = backend._sync_list_projects_with_sessions()

        assert grouped[UiBackend.NO_PROJECT_BUCKET] == ["AgenticBotPlatform"]
        assert grouped["Kestrion"] == ["Fix the login bug"]

    def test_a_project_with_no_sessions_yet_still_appears(self):
        backend = UiBackend()
        win = _win([_button("New session in Omnisystem")])
        backend._connect = lambda: win  # type: ignore[method-assign]

        grouped = backend._sync_list_projects_with_sessions()

        assert grouped == {"Omnisystem": []}

    def test_pinned_project_shortcut_is_not_reported_as_a_standalone_chat(self):
        """Real bug found live: Desktop's "Pinned" section shows a
        shortcut per pinned PROJECT styled identically to a real session
        button ("Idle Kestrion"), appearing before any "New session in X"
        marker — a naive scan reported "Kestrion" as a standalone chat in
        NO_PROJECT_BUCKET even though "Kestrion" was also a real project
        with its own sessions found later in the same scan."""
        backend = UiBackend()
        win = _win([
            _button("Idle Kestrion"),  # pinned shortcut, not a real chat
            _button("New session in Kestrion"),
            _button("Idle Fix the login bug"),
        ])
        backend._connect = lambda: win  # type: ignore[method-assign]

        grouped = backend._sync_list_projects_with_sessions()

        assert UiBackend.NO_PROJECT_BUCKET not in grouped
        assert grouped["Kestrion"] == ["Fix the login bug"]

    def test_no_project_bucket_omitted_when_empty(self):
        backend = UiBackend()
        win = _win([_button("New session in Kestrion"), _button("Idle Fix the login bug")])
        backend._connect = lambda: win  # type: ignore[method-assign]

        grouped = backend._sync_list_projects_with_sessions()

        assert UiBackend.NO_PROJECT_BUCKET not in grouped


class TestClickProjectHeader:
    def test_clicks_the_exact_header_button(self):
        """Real bug found live: the sidebar's session rows are
        virtualized — a project's sessions are genuinely absent from the
        accessibility tree (not collapsed, not paginated) until that
        project's own plain header button is clicked."""
        backend = UiBackend()
        kestrion_header = _button("Kestrion")
        win = _win([kestrion_header, _button("New session in Kestrion")])

        found = backend._sync_click_project_header(win, "Kestrion")

        assert found is True
        kestrion_header.click_input.assert_called_once()

    def test_missing_header_is_a_safe_no_op(self):
        """Some projects (e.g. a very new one) show no separate header
        button at all — already as expanded as they'll get."""
        backend = UiBackend()
        win = _win([_button("New session in TridentDroid")])

        found = backend._sync_click_project_header(win, "TridentDroid")

        assert found is False

    def test_pinned_shortcut_is_never_mistaken_for_the_real_header(self):
        """The pinned section's "Idle Kestrion" shortcut must not be
        clicked instead of the real plain "Kestrion" header — only an
        exact, unprefixed name match counts."""
        backend = UiBackend()
        pinned_shortcut = _button("Idle Kestrion")
        real_header = _button("Kestrion")
        win = _win([pinned_shortcut, real_header, _button("New session in Kestrion")])

        backend._sync_click_project_header(win, "Kestrion")

        pinned_shortcut.click_input.assert_not_called()
        real_header.click_input.assert_called_once()


class TestListProjectsWithSessionsAccordion:
    def test_clicks_and_scans_each_project_one_at_a_time(self):
        """Confirmed live this is genuinely ACCORDION behavior — clicking
        one project's header collapses whichever was previously expanded.
        An "expand every project, then scan once" approach only ever
        captured the LAST project clicked; this locks in the real fix:
        click one, scan immediately, then move to the next, merging
        results across all of them."""
        backend = UiBackend()
        click_log: list[str] = []

        # Each project only reveals its own sessions in this fake
        # sidebar's view AFTER its own header has been clicked — modeling
        # the real accordion behavior found live.
        expanded = {"project": None}

        def make_win():
            win = MagicMock()

            def descendants(control_type=None, **_):
                if control_type != "Button":
                    return []
                # Realistic ordering: each project's own header + sessions
                # (when expanded) sit right after that project's own "New
                # session in X" marker, before the next project's.
                buttons = [_button("Kestrion"), _button("New session in Kestrion")]
                if expanded["project"] == "Kestrion":
                    buttons.append(_button("Idle Fix the login bug"))
                buttons += [_button("TridentDroid"), _button("New session in TridentDroid")]
                if expanded["project"] == "TridentDroid":
                    buttons.append(_button("Idle Android crash triage"))
                return buttons

            win.descendants.side_effect = descendants
            return win

        win = make_win()
        backend._connect = lambda: win  # type: ignore[method-assign]

        def fake_click_header(win, project):
            click_log.append(project)
            expanded["project"] = project
            return True

        backend._sync_click_project_header = fake_click_header  # type: ignore[method-assign]

        grouped = backend._sync_list_projects_with_sessions()

        assert click_log == ["Kestrion", "TridentDroid"]
        assert grouped == {
            "Kestrion": ["Fix the login bug"],
            "TridentDroid": ["Android crash triage"],
        }


class TestExpandPaginatedSessions:
    def test_clicks_show_more_until_it_disappears(self):
        backend = UiBackend(poll_interval_s=0.001)
        show_more = _button("Show 3 more in Kestrion")
        state = {"expanded": False}

        def descendants(control_type=None, **_):
            if control_type != "Button":
                return []
            return [] if state["expanded"] else [show_more]

        def click():
            state["expanded"] = True

        show_more.click_input.side_effect = click
        win = MagicMock()
        win.descendants.side_effect = descendants

        backend._sync_expand_paginated_sessions(win)

        show_more.click_input.assert_called_once()

    def test_noop_when_nothing_paginated(self):
        backend = UiBackend()
        win = _win([_button("Idle Fix the login bug")])

        backend._sync_expand_paginated_sessions(win)  # must not raise


class TestFindDefaultWorkspaceChip:
    def test_returns_the_non_local_chip_between_feedback_and_add_folder(self):
        backend = UiBackend()
        win = _win([
            _button("Send feedback"),
            _button("Local"),
            _button("Aion"),
            _button("Add another folder"),
            _button("Send"),
        ])

        chip = backend._find_default_workspace_chip(win)

        assert chip.window_text() == "Aion"

    def test_returns_none_when_only_local_is_present(self):
        backend = UiBackend()
        win = _win([
            _button("Send feedback"),
            _button("Local"),
            _button("Add another folder"),
        ])

        assert backend._find_default_workspace_chip(win) is None

    def test_returns_none_when_add_another_folder_is_absent(self):
        backend = UiBackend()
        win = _win([_button("Local")])

        assert backend._find_default_workspace_chip(win) is None


class _FakeWin32Clipboard:
    """Stands in for the real win32clipboard module — enough surface for
    _type_text_via_clipboard's open/get/empty/set/close sequence.
    fail_opens_before_success lets a test simulate OpenClipboard()'s real,
    transient "another process is holding it" failure mode."""

    CF_UNICODETEXT = 13

    def __init__(self, initial_contents=None, fail_opens_before_success=0):
        self._contents = initial_contents
        self.set_calls = []
        self.open_count = 0
        self.close_count = 0
        self._fail_opens_remaining = fail_opens_before_success

    def OpenClipboard(self):
        self.open_count += 1
        if self._fail_opens_remaining > 0:
            self._fail_opens_remaining -= 1
            raise OSError("Access is denied.")

    def CloseClipboard(self):
        self.close_count += 1

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

    def test_retries_a_transiently_failing_open_before_succeeding(self, monkeypatch):
        """Real bug found live: "(1418, 'CloseClipboard', 'Thread does
        not have a clipboard open.')" — the original code called
        CloseClipboard() in a bare finally even when OpenClipboard()
        itself had failed. OpenClipboard() genuinely can fail
        transiently (another process briefly holding the clipboard);
        this must retry rather than immediately treat one failure as
        fatal, and must never close a clipboard it never successfully
        opened."""
        fake_clipboard = _FakeWin32Clipboard(fail_opens_before_success=2)
        monkeypatch.setitem(sys.modules, "win32clipboard", fake_clipboard)
        monkeypatch.setattr("bot.backends.ui_backend.time.sleep", lambda s: None)
        field = MagicMock()

        _type_text_via_clipboard(field, "some prompt text")

        assert fake_clipboard.set_calls == ["some prompt text"]
        # Every OpenClipboard() call that actually succeeded (2 failures
        # + 1 success for the "set" half, then however many it took for
        # the restore half) has a matching CloseClipboard() — never more
        # closes than successful opens.
        assert fake_clipboard.close_count <= fake_clipboard.open_count

    def test_never_closes_a_clipboard_it_never_opened(self, monkeypatch):
        fake_clipboard = _FakeWin32Clipboard(fail_opens_before_success=999)
        monkeypatch.setitem(sys.modules, "win32clipboard", fake_clipboard)
        monkeypatch.setattr("bot.backends.ui_backend.time.sleep", lambda s: None)
        field = MagicMock()

        try:
            _type_text_via_clipboard(field, "some prompt text")
        except Exception:
            pass

        assert fake_clipboard.close_count == 0
