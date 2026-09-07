"""UI automation backend — drives the actual Claude Desktop window.

This is the one backend that can read or continue whatever conversation is
already open in Desktop. It is also the least stable of the three: there is
no official automation API, Claude Desktop is Electron (its accessibility
tree is not guaranteed to be complete or stable across versions), and any
UI update can silently break the selectors below.

Treat this as opt-in and narrow, exactly as scoped in the router config —
only route to it what genuinely needs "whatever's open in the window right
now". If your Claude Desktop version exposes different control names, set
`input_automation_id` / `send_button_automation_id` in config/backends.yaml
under backends.ui rather than editing this file; leave them unset to fall
back to a best-effort heuristic search (first editable control, first
button whose name contains "Send").

Windows + pywinauto only.

Session isolation
------------------
Claude Desktop has one window with many chats in its sidebar; without
tracking which sidebar chat belongs to which bot instance, two instances
routed to "ui" would both type into whatever chat happens to be selected —
possibly each other's. To prevent that:

  - Every ask() requires a `context["desktop_session_key"]` (the sidebar
    chat's label, captured at creation time — see create_session()) unless
    the caller explicitly wants a brand-new chat
    (`context["force_new_session"]`). No key and no force -> BackendError,
    never "whatever's open".
  - Before typing, `_select_session()` re-selects that exact sidebar item
    by label so a message can never land in an unlinked/wrong chat. If the
    labeled chat can't be found (renamed/deleted in Desktop itself), this
    fails loudly rather than silently falling back to whatever's focused.
  - A single `asyncio.Lock` serializes every ask()/create_session() call
    against this shared window — there is only one real OS window, so two
    instances' calls must never interleave chat-switch-then-type sequences.
"""

from __future__ import annotations

import asyncio
import logging
import platform
import re
import time
from typing import Optional

from bot.backends.base import Backend, BackendError, BackendResult

logger = logging.getLogger("bot.backends.ui")


def _type_text_via_clipboard(field, text: str) -> None:
    """Enters text into a focused control via the clipboard (set text,
    Ctrl+V, restore whatever was on the clipboard before) instead of
    field.type_keys(text). Confirmed live and real: type_keys() sends
    Windows SendInput keystrokes one at a time, which reliably fails
    with "[Errno 22] Invalid argument" on kaomoji and similar Unicode
    outside plain ASCII — a real production instance's prompt started
    failing outright the moment its custom_instructions gained kaomoji.
    Clipboard paste hands the whole string to the OS in one piece and
    has no such character-set limitation, matching how a human would
    paste non-ASCII text in practice. The clipboard's prior contents are
    restored afterward so this doesn't clobber whatever the user
    actually had copied."""
    import win32clipboard

    def _open_clipboard_with_retry(attempts: int = 10, delay_s: float = 0.05) -> bool:
        # Real bug found live: OpenClipboard() can transiently fail
        # (Windows clipboard access is exclusive — another process, or
        # even Explorer's own clipboard history, can be holding it for a
        # moment) — confirmed by an actual "Thread does not have a
        # clipboard open" CloseClipboard error, which happened because
        # the original code unconditionally closed in a bare `finally`
        # even when open itself had failed. Retrying open a few times is
        # the standard, expected way to handle this on Windows; only a
        # *successful* open is ever paired with a close below.
        for attempt in range(attempts):
            try:
                win32clipboard.OpenClipboard()
                return True
            except Exception:
                if attempt == attempts - 1:
                    return False
                time.sleep(delay_s)
        return False

    previous = None
    if _open_clipboard_with_retry():
        try:
            try:
                previous = win32clipboard.GetClipboardData(win32clipboard.CF_UNICODETEXT)
            except Exception:
                previous = None
            win32clipboard.EmptyClipboard()
            win32clipboard.SetClipboardData(win32clipboard.CF_UNICODETEXT, text)
        finally:
            win32clipboard.CloseClipboard()
    else:
        raise BackendError("could not open the clipboard to paste the prompt (another process was holding it)")

    try:
        field.type_keys("^v")
    finally:
        if _open_clipboard_with_retry():
            try:
                win32clipboard.EmptyClipboard()
                if previous is not None:
                    win32clipboard.SetClipboardData(win32clipboard.CF_UNICODETEXT, previous)
            except Exception:
                pass
            finally:
                win32clipboard.CloseClipboard()

# Confirmed live against a real running Claude Desktop install: a brand-new
# chat has NO sidebar entry at all until its first message exchange gives it
# an auto-generated title (Desktop names it from the conversation content,
# e.g. sending "PONG?" produced a session titled "PONG response"). There is
# nothing to read a real key from immediately after clicking the new-chat
# button. This sentinel stands in for "opened, but not yet named" — ask()
# recognizes it and skips trying to re-select a session that doesn't exist
# in the sidebar yet, then discovers and persists the real title once the
# first reply lands.
UNTITLED_SESSION_KEY = "__untitled__"

# How long the visible text must stay completely unchanged before a reply is
# considered finished streaming. Real bug found live: the old fixed "2 polls"
# threshold (1s at the default 0.5s poll interval) was short enough that a
# normal mid-generation pause (e.g. between an opening sentence and the rest
# of a longer answer) got mistaken for the model being done — a real reply
# was cut down to its first couple of words as a result.
REPLY_STABLE_SECONDS = 4.0

# Confirmed live (with a real, longer, multi-part reply): the "Claude
# responded: <preview>" accessibility announcement extraction below used to
# be preferred as the authoritative reply source — but it is itself only a
# short PREVIEW of the reply (a one-sentence summary, apparently for
# screen-reader brevity), not the full text. A real test reply consisting of
# an opening sentence, three bulleted list items, and a closing sentence
# announced as just the opening sentence, while the visible message
# contained everything — every earlier "truncated to the first couple of
# words" report traces back to this. Desktop also emits a distinct, exact
# marker text once a reply is genuinely done streaming, which is a far more
# reliable completion signal than guessing from a stability timeout.
REPLY_COMPLETE_MARKER = "Claude finished the response"

# Known UI chrome / echo strings that can appear as "new" accessibility text
# around a reply but are never part of the reply's own content. Bulleted
# list items render as a distinct "ListItem" control type, not "Text" —
# _collect_texts() must gather both or a reply's list items go missing
# entirely, exactly as confirmed live.
_UI_CHROME_TEXTS = {
    REPLY_COMPLETE_MARKER,
    "Use the up and down arrow keys to move between messages.",
    "Chat mode",
    "Claude is responding",
}

# Confirmed live: Desktop's own compose toolbar has a real "Effort" slider
# (a UIA RangeValue control, name "Effort") with these six exact levels —
# found by sweeping its full 0.0-5.0 range and reading back the resulting
# legacy-accessibility "Value" string at each step. It defaults to "High"
# and is a genuine per-turn cost multiplier, not a cosmetic — every
# automated send forces it down to a configured level (Low by default) so
# routine bot traffic never silently burns Extra/Max/Ultracode-tier
# reasoning budget the way a human would have to remember to avoid by hand.
EFFORT_LEVELS: dict[str, float] = {
    "low": 0.0,
    "medium": 1.0,
    "high": 2.0,
    "extra": 3.0,
    "max": 4.0,
    "ultracode": 5.0,
}
DEFAULT_EFFORT = "low"


def _is_chrome_or_echo(text: str, prompt_stripped: str) -> bool:
    if text in _UI_CHROME_TEXTS:
        return True
    if text.startswith("You said: ") or text.startswith("Claude responded: "):
        return True
    if text == prompt_stripped:
        return True
    return False


class UiBackend(Backend):
    name = "ui"

    def __init__(
        self,
        window_title_re: str = "Claude.*",
        poll_interval_s: float = 0.5,
        input_automation_id: Optional[str] = None,
        send_button_automation_id: Optional[str] = None,
        new_chat_button_automation_id: Optional[str] = None,
        sidebar_item_control_type: str = "Button",
    ):
        self.window_title_re = window_title_re
        self.poll_interval_s = poll_interval_s
        self.input_automation_id = input_automation_id
        self.send_button_automation_id = send_button_automation_id
        self.new_chat_button_automation_id = new_chat_button_automation_id
        self.sidebar_item_control_type = sidebar_item_control_type
        self._lock = asyncio.Lock()

        if platform.system() != "Windows":
            logger.warning("UiBackend initialized on non-Windows platform — will fail at call time")

    def _connect(self):
        from pywinauto import Desktop

        try:
            win = Desktop(backend="uia").window(title_re=self.window_title_re)
            win.wait("exists enabled visible ready", timeout=5)
            return win
        except Exception as exc:
            raise BackendError(
                f"could not find/focus a window matching {self.window_title_re!r} — "
                "is Claude Desktop running? (use /start_desktop)"
            ) from exc

    def _find_input(self, win):
        if self.input_automation_id:
            return win.child_window(auto_id=self.input_automation_id, control_type="Edit")
        candidates = win.descendants(control_type="Edit")
        if not candidates:
            candidates = win.descendants(control_type="Document")
        if not candidates:
            raise BackendError(
                "no editable text control found in the Claude Desktop window — "
                "set backends.ui.input_automation_id in config/backends.yaml"
            )
        return candidates[0]

    def _find_send_button(self, win):
        if self.send_button_automation_id:
            return win.child_window(auto_id=self.send_button_automation_id, control_type="Button")
        # Confirmed live, and the actual root cause of a whole run of
        # apparent "reply extraction" failures earlier: Claude Desktop
        # also has a "Send feedback" button elsewhere in the window,
        # which a bare substring match ("send" in name) can find and
        # click FIRST — it's disabled, so the click silently does
        # nothing and the real message is left sitting unsent in the
        # input field. An exact (case-insensitive) match to "send" only
        # matches the real button.
        for btn in win.descendants(control_type="Button"):
            try:
                if (btn.window_text() or "").strip().lower() == "send":
                    return btn
            except Exception:
                continue
        return None

    def _find_new_chat_button(self, win, project: Optional[str] = None):
        """Current Claude Desktop (confirmed live against a real running
        install) organizes chats per-project in the sidebar — there is no
        single global "New chat" button anymore, only per-project "New
        session in <project>" buttons plus one bare "New" button (labeled
        just "New", not "New chat") for a session outside any project. A
        [project] name routes to that project's own button; without one,
        the bare "New" button is used. The old "new chat"/"+" heuristic
        below this comment's history matched neither and always raised —
        this is a real, confirmed fix, not a guess."""
        if self.new_chat_button_automation_id:
            return win.child_window(auto_id=self.new_chat_button_automation_id, control_type="Button")
        if not project:
            # Confirmed live: the bare "New" button (no project) opens a
            # picker screen (pick a project/worktree) before landing on a
            # compose view — a real, meaningful choice this backend
            # deliberately does not guess at. Only the per-project "New
            # session in <project>" buttons drop straight into compose,
            # which is the one path the rest of this backend automates.
            raise BackendError(
                "the ui backend needs a project to start a session reliably — set desktop_project on this "
                "bot instance to one of list_desktop_projects()'s names (the bare \"New\" button opens a "
                "project/worktree picker screen this backend doesn't automate)"
            )
        target = f"new session in {project}".strip().lower()
        for btn in win.descendants(control_type="Button"):
            try:
                name = (btn.window_text() or "").strip().lower()
            except Exception:
                continue
            if name == target:
                return btn
        raise BackendError(
            f"no \"New session in {project}\" button found — is that project still open in "
            "Claude Desktop's sidebar? Use list_projects() to see what's currently available."
        )

    def _find_default_workspace_chip(self, win):
        """The folder chip(s) Desktop's own Ctrl+N ("New") landing screen
        pre-attaches to a brand-new chat, shown next to a constant "Local"
        button just above the compose box. Confirmed live this does NOT
        track the currently-active sidebar project — switching which
        project was active in the sidebar and then pressing Ctrl+N again
        left the exact same default chip in place — so a bot instance
        dedicated to its own isolated workspace cannot assume a fresh chat
        starts with no folder context; it must explicitly replace whatever
        default is already attached (see _sync_open_isolated_workspace).
        Returns the chip button to click to open its replace menu, or None
        if only the constant "Local" option is present."""
        names_and_buttons: list[tuple[str, object]] = []
        for btn in win.descendants(control_type="Button"):
            try:
                name = (btn.window_text() or "").strip()
            except Exception:
                continue
            if name:
                names_and_buttons.append((name, btn))
        try:
            add_idx = next(i for i, (name, _) in enumerate(names_and_buttons) if name == "Add another folder")
        except StopIteration:
            return None
        try:
            start_idx = next(i for i, (name, _) in enumerate(names_and_buttons) if name == "Send feedback") + 1
        except StopIteration:
            start_idx = 0
        for name, btn in names_and_buttons[start_idx:add_idx]:
            if name != "Local":
                return btn
        return None

    @staticmethod
    def _sync_pick_folder_in_dialog(workspace_dir: str, timeout_s: float = 8.0) -> None:
        """Automates whichever native Windows folder-picker dialog Desktop
        just opened (title differs depending on which button triggered it
        — confirmed live both "Select folder for local session" from the
        chip's "Open folder..." replace menu, and "Add folder to session"
        from the plain "Add another folder" button) by pasting the target
        path into its address bar (Ctrl+L, matching how a human would jump
        straight to a path in Windows Explorer) rather than clicking
        through the tree view."""
        import win32gui
        from pywinauto import Application
        from pywinauto.keyboard import send_keys

        dialog_titles = ("Select folder for local session", "Add folder to session")
        deadline = time.monotonic() + timeout_s
        hwnd = 0
        title = None
        while time.monotonic() < deadline and not hwnd:
            for candidate in dialog_titles:
                hwnd = win32gui.FindWindow(None, candidate)
                if hwnd:
                    title = candidate
                    break
            if not hwnd:
                time.sleep(0.2)
        if not hwnd:
            raise BackendError(
                "ui backend: expected a folder-picker dialog to open but none appeared — "
                "Claude Desktop's new-chat screen may have changed"
            )

        win32gui.SetForegroundWindow(hwnd)
        time.sleep(0.3)
        send_keys("^l")
        time.sleep(0.2)
        # SendKeys' mini-language treats {}, +, ^, %, ~, (, ) specially —
        # a real workspace path (e.g. under "Program Files (x86)") must
        # have these escaped or the keystrokes sent are simply wrong.
        escaped = re.sub(r"([{}+^%~()])", r"{\1}", workspace_dir)
        send_keys(escaped, pause=0.01, with_spaces=True)
        time.sleep(0.2)
        send_keys("{ENTER}")
        time.sleep(0.8)

        app = Application(backend="win32").connect(title=title)
        dlg = app.window(title=title)
        dlg.child_window(title="Select Folder", class_name="Button").click()

    def _sync_open_isolated_workspace(self, win, workspace_dir: str) -> None:
        """Opens a brand-new chat scoped to exactly [workspace_dir] and
        nothing else. Confirmed live that "Add another folder" only ADDS
        a folder alongside whatever Desktop already pre-attached — a
        session created that way ended up scoped to BOTH the intended
        scratch folder AND the leftover default project, and a real
        message sent into it came back referencing the wrong project.
        Clicking the existing default chip (see
        _find_default_workspace_chip) opens a small menu containing
        "Open folder...", which genuinely REPLACES it — confirmed live by
        inspecting the resulting session's own folder-chip toolbar and
        seeing only the new folder, and by the model's own reply
        confirming "a clean, isolated workspace (not a git repo, no prior
        state loaded)"."""
        win.type_keys("^n")
        time.sleep(2.0)

        default_chip = self._find_default_workspace_chip(win)
        if default_chip is not None:
            default_chip.click_input()
            time.sleep(0.6)
            opened = False
            for item in win.descendants(control_type="MenuItem"):
                try:
                    name = (item.window_text() or "").strip().lower()
                except Exception:
                    continue
                if name.startswith("open folder"):
                    item.click_input()
                    opened = True
                    break
            if not opened:
                win.type_keys("{ESC}")
                raise BackendError(
                    "ui backend could not find \"Open folder...\" to replace the default workspace "
                    "folder on Claude Desktop's new-chat screen — it may have changed"
                )
        else:
            for btn in win.descendants(control_type="Button"):
                try:
                    name = (btn.window_text() or "").strip()
                except Exception:
                    continue
                if name == "Add another folder":
                    btn.click_input()
                    break

        self._sync_pick_folder_in_dialog(workspace_dir)
        time.sleep(1.0)

        # A folder Desktop has never opened before needs a one-time trust
        # confirmation — confirmed live; absent on every later reuse of
        # the same folder.
        for btn in win.descendants(control_type="Button"):
            try:
                name = (btn.window_text() or "").strip()
            except Exception:
                continue
            if name == "Trust workspace":
                btn.click_input()
                time.sleep(0.8)
                break

    def _sync_ensure_effort(self, win, level: str) -> None:
        """Forces Desktop's own "Effort" slider to [level] before a
        message is sent — see EFFORT_LEVELS' module docstring for how
        this control was confirmed live. A no-op once the toolbar button
        already reads the target level, so this costs nothing on the
        common case."""
        target_label = f"Effort: {level.capitalize()}" if level != "ultracode" else "Effort: Ultracode"
        for btn in win.descendants(control_type="Button"):
            try:
                text = (btn.window_text() or "").strip()
            except Exception:
                continue
            if not text.startswith("Effort:"):
                continue
            if text == target_label:
                return
            btn.click_input()
            time.sleep(0.4)
            for slider in win.descendants(control_type="Slider"):
                if slider.window_text() == "Effort":
                    try:
                        slider.set_value(EFFORT_LEVELS.get(level, EFFORT_LEVELS[DEFAULT_EFFORT]))
                    except Exception:
                        logger.warning("ui backend: could not set the Effort slider to %r", level)
                    break
            win.type_keys("{ESC}")
            return

    def _sync_list_projects(self) -> list[str]:
        win = self._connect()
        projects: list[str] = []
        prefix = "new session in "
        for btn in win.descendants(control_type="Button"):
            try:
                name = (btn.window_text() or "").strip()
            except Exception:
                continue
            if name.lower().startswith(prefix):
                projects.append(name[len(prefix):])
        return projects

    NO_PROJECT_BUCKET = "(no project)"

    def _sync_list_projects_with_sessions(self) -> dict[str, list[str]]:
        """Every project's real, currently-visible sidebar sessions,
        grouped by project — confirmed live that a project's own session
        buttons ("Idle <title>"/"Running <title>") are listed immediately
        after that project's own "New session in <project>" button, before
        the next project's, so a project can be inferred from button
        order alone without any explicit parent/child UIA relationship to
        rely on. Sessions appearing before the first project marker at all
        (e.g. a pinned session outside any project) are grouped under
        NO_PROJECT_BUCKET rather than dropped."""
        win = self._connect()
        new_session_prefix = "new session in "
        result: dict[str, list[str]] = {}
        current_project = self.NO_PROJECT_BUCKET
        for btn in win.descendants(control_type="Button"):
            try:
                name = (btn.window_text() or "").strip()
            except Exception:
                continue
            if not name:
                continue
            lowered = name.lower()
            if lowered.startswith(new_session_prefix):
                current_project = name[len(new_session_prefix):]
                result.setdefault(current_project, [])
                continue
            for status_prefix in ("Idle ", "Running "):
                if name.startswith(status_prefix):
                    result.setdefault(current_project, []).append(name[len(status_prefix):])
                    break
        if not result.get(self.NO_PROJECT_BUCKET):
            result.pop(self.NO_PROJECT_BUCKET, None)
        return result

    async def list_projects(self, timeout_s: float = 10) -> list[str]:
        """Every project currently visible in Claude Desktop's sidebar —
        lets a caller (dashboard/MCP) show real, live project names before
        picking one to pin a bot instance to, or to start a session in."""
        if platform.system() != "Windows":
            raise BackendError("ui backend is only available on Windows")
        async with self._lock:
            try:
                return await asyncio.to_thread(self._sync_list_projects)
            except BackendError:
                raise
            except Exception as exc:
                raise BackendError(f"ui backend error listing projects: {exc}") from exc

    async def list_projects_with_sessions(self, timeout_s: float = 10) -> dict[str, list[str]]:
        """Every project's real, currently-visible sessions, grouped by
        project name — the live data behind letting a user browse "what
        projects and chats already exist in Claude Desktop" and pick one
        to continue, rather than only ever creating brand-new sessions."""
        if platform.system() != "Windows":
            raise BackendError("ui backend is only available on Windows")
        async with self._lock:
            try:
                return await asyncio.to_thread(self._sync_list_projects_with_sessions)
            except BackendError:
                raise
            except Exception as exc:
                raise BackendError(f"ui backend error listing projects and sessions: {exc}") from exc

    def _session_buttons(self, win) -> dict[str, object]:
        """Every sidebar row that is an actual chat session, keyed by its
        bare title with the "Idle "/"Running " status prefix stripped —
        confirmed live that real Claude Desktop sidebar rows are plain
        Buttons named exactly f"{status} {title}", not a distinct
        ListItem/TreeItem control type the old code assumed. Buttons that
        aren't session rows (New, Search, per-project "New session in X",
        etc.) never match this prefix and are correctly excluded."""
        items: dict[str, object] = {}
        for btn in win.descendants(control_type=self.sidebar_item_control_type):
            try:
                text = (btn.window_text() or "").strip()
            except Exception:
                continue
            for prefix in ("Idle ", "Running "):
                if text.startswith(prefix):
                    items[text[len(prefix):]] = btn
                    break
        return items

    def _sync_create_session(
        self, timeout_s: float, project: Optional[str] = None, workspace_dir: Optional[str] = None
    ) -> str:
        """Opens a brand-new chat and returns immediately with
        UNTITLED_SESSION_KEY — see that constant's docstring for why no
        real key can be read yet. The caller (ask(), or a later ask() for
        a chat whose only prior create_session() call returned this same
        placeholder) is responsible for discovering and persisting the
        real title once a first message actually names the session.

        [workspace_dir], when given, takes priority over [project]: it
        opens a chat scoped to exactly that folder and nothing else (see
        _sync_open_isolated_workspace) rather than a new session nested
        inside an existing project — the reliable way to guarantee a bot
        instance's own chats never land inside someone else's real
        project. Without it, falls back to the existing per-project
        "New session in <project>" button."""
        win = self._connect()
        win.set_focus()
        if workspace_dir:
            self._sync_open_isolated_workspace(win, workspace_dir)
        else:
            btn = self._find_new_chat_button(win, project=project)
            btn.click_input()
        # Confirmed live: poll_interval_s (0.5s) alone isn't a long enough
        # pause here — the new compose view is still transitioning in
        # right after the click, so typing too soon lands in whatever
        # Edit control was already on screen before the switch finished
        # (confirmed by reproducing exactly this: an automated attempt at
        # 0.5s typed into the wrong place and echoed the prompt back
        # instead of getting a real reply, while the identical sequence
        # done by hand with a ~1.5s pause worked correctly). A fixed,
        # slightly generous pause here is simpler and no less reliable
        # than guessing a readiness signal from an already-undocumented,
        # unstable accessibility tree.
        time.sleep(max(self.poll_interval_s, 2.0))
        return UNTITLED_SESSION_KEY

    def _select_session(self, win, session_key: str) -> None:
        items = self._session_buttons(win)
        btn = items.get(session_key)
        if btn is None:
            raise BackendError(
                f"linked chat {session_key!r} is no longer in the Claude Desktop sidebar (renamed or deleted there) — "
                "create a new session for this bot instance to relink it"
            )
        btn.click_input()

    def _sync_ask(
        self, prompt: str, timeout_s: float, session_key: Optional[str], effort: str = DEFAULT_EFFORT
    ) -> tuple[str, Optional[str]]:
        """Returns (reply_text, discovered_session_key). discovered_session_key
        is only ever non-None when [session_key] was None/UNTITLED_SESSION_KEY
        going in — the real title Desktop assigned this session from this
        very message, to be persisted by the caller."""
        win = self._connect()
        win.set_focus()

        untitled = session_key is None or session_key == UNTITLED_SESSION_KEY
        before_labels: set[str] = set()
        if untitled:
            # Nothing to select — the session this ask() is for was either
            # just created (still the focused/active chat) or was created
            # by an earlier create_session() call and never touched since,
            # so it's still sitting there focused with nothing else having
            # happened in this single-window, lock-serialized backend
            # since. Snapshot which titled sessions already exist so the
            # one that appears after this message is unambiguous.
            before_labels = set(self._session_buttons(win).keys())
        else:
            self._select_session(win, session_key)

        # Force the Effort level before every single send (not just once
        # per session) — it's a global toolbar control, not per-chat state,
        # so nothing guarantees it stays put between messages.
        self._sync_ensure_effort(win, effort)

        before_texts = {t.strip() for t in self._collect_texts(win) if t and t.strip()}

        field = self._find_input(win)
        field.set_focus()
        _type_text_via_clipboard(field, prompt)

        send_btn = self._find_send_button(win)
        if send_btn is not None:
            send_btn.click_input()
        else:
            field.type_keys("{ENTER}")

        prompt_stripped = prompt.strip()
        deadline = time.monotonic() + timeout_s
        last_texts: set[str] = before_texts
        stable_reads = 0
        # REPLY_COMPLETE_MARKER (checked first, below) is the fast, reliable
        # completion signal. This stable-reads counter is only a fallback
        # for when that exact marker text is ever absent (older/different
        # Desktop build) — real bug found live: requiring just 2 stable
        # polls (1s at the default 0.5s interval) declared the reply
        # "finished" the moment generation paused for a beat mid-reply.
        stable_reads_required = max(2, round(REPLY_STABLE_SECONDS / self.poll_interval_s))
        while time.monotonic() < deadline:
            time.sleep(self.poll_interval_s)
            current_list = [t.strip() for t in self._collect_texts(win) if t and t.strip()]
            current = set(current_list)
            new_text = current - before_texts
            if new_text and current == last_texts:
                stable_reads += 1
            else:
                stable_reads = 0
            if new_text and (REPLY_COMPLETE_MARKER in new_text or stable_reads >= stable_reads_required):
                # Confirmed live: the "Claude responded: <preview>"
                # announcement is only a short, one-sentence PREVIEW of the
                # reply, not the full text — a real reply containing an
                # opening sentence, a bulleted list, and a closing sentence
                # announced only the opening sentence, while the actual
                # on-screen content had everything. Prefer reassembling the
                # real visible content (preserving on-screen order, since a
                # set loses it) and only fall back to the truncated
                # announcement if nothing else survives filtering.
                new_ordered = list(dict.fromkeys(t for t in current_list if t in new_text))
                content = [t for t in new_ordered if not _is_chrome_or_echo(t, prompt_stripped)]
                if content:
                    reply = "\n".join(content)
                else:
                    announced = next(
                        (t[len("Claude responded: "):] for t in new_text if t.startswith("Claude responded: ")),
                        None,
                    )
                    reply = announced if announced is not None else "\n".join(sorted(new_text, key=len, reverse=True)[:1] or new_text)
                discovered = None
                if untitled:
                    # Confirmed live: the sidebar's auto-generated title
                    # lags a couple of seconds behind the reply text itself
                    # becoming stable — polling only once, right here,
                    # missed a title that reliably showed up moments later.
                    # A short bounded retry (not the full timeout_s budget)
                    # covers that lag without risking a slow permanent hang.
                    # Confirmed live: re-using the same WindowSpecification
                    # object across repeated descendants() calls in this
                    # loop never saw the new title appear no matter how long
                    # the wait, while a freshly reconnected window handle
                    # found it within a fraction of a second — a real
                    # pywinauto/UIA element-tree caching gotcha on a
                    # long-lived handle, not an actual Desktop delay.
                    # Reconnecting fresh each attempt avoids it.
                    title_deadline = time.monotonic() + 20
                    new_labels: set[str] = set()
                    while time.monotonic() < title_deadline:
                        fresh_win = self._connect()
                        new_labels = set(self._session_buttons(fresh_win).keys()) - before_labels
                        if new_labels:
                            break
                        time.sleep(self.poll_interval_s)
                    if len(new_labels) == 1:
                        discovered = next(iter(new_labels))
                    elif new_labels:
                        # Ambiguous (more than one new title appeared, e.g.
                        # another instance's ask() also just finished) —
                        # better to leave this session unlinked than link it
                        # to the wrong title.
                        logger.warning("ui backend: %d new session titles appeared, could not disambiguate", len(new_labels))
                    else:
                        logger.warning("ui backend: sent a message into an untitled session but no new sidebar title appeared yet")
                return reply, discovered
            last_texts = current

        raise BackendError(f"ui backend timed out after {timeout_s}s waiting for a reply")

    @staticmethod
    def _collect_texts(win) -> list[str]:
        # Confirmed live: a reply's own markdown list items (e.g. bullet
        # points) render as "ListItem" controls, not "Text" — a reply
        # consisting of a lead sentence, a bulleted list, and a closing
        # sentence had its bullets silently vanish from extraction entirely
        # when only "Text" was collected here.
        texts = []
        for el in win.descendants(control_type="Text"):
            try:
                texts.append(el.window_text())
            except Exception:
                continue
        for el in win.descendants(control_type="ListItem"):
            try:
                texts.append(el.window_text())
            except Exception:
                continue
        return texts

    async def create_session(
        self, timeout_s: float = 45, project: Optional[str] = None, workspace_dir: Optional[str] = None
    ) -> str:
        """Explicitly opens a brand-new chat in the real Claude Desktop
        window and returns UNTITLED_SESSION_KEY — see that constant's
        docstring. [workspace_dir] (an absolute folder path) takes
        priority over [project] (an existing project's display name) when
        both are given — see _sync_open_isolated_workspace for why a
        workspace directory is the reliable way to guarantee a brand-new
        chat is never created inside an unrelated existing project.
        Persist the returned placeholder exactly like a real key; the
        first ask() that consumes it discovers and reports back the real
        title, which Router.ask() already re-persists over this
        placeholder the same way it does for its own lazy-create path."""
        if platform.system() != "Windows":
            raise BackendError("ui backend is only available on Windows")
        async with self._lock:
            try:
                return await asyncio.to_thread(self._sync_create_session, timeout_s, project, workspace_dir)
            except BackendError:
                raise
            except Exception as exc:
                raise BackendError(f"ui backend error creating session: {exc}") from exc

    async def ask(self, prompt: str, *, context=None, timeout_s: float = 45) -> BackendResult:
        if platform.system() != "Windows":
            raise BackendError("ui backend is only available on Windows")

        context = context or {}
        session_key = context.get("desktop_session_key")
        force_new = bool(context.get("force_new_session"))
        instance_id = context.get("instance_id")
        project = context.get("desktop_project")
        workspace_dir = context.get("desktop_workspace_dir")
        effort = context.get("desktop_effort") or DEFAULT_EFFORT

        async with self._lock:
            try:
                if force_new or not session_key:
                    if instance_id is None:
                        raise BackendError(
                            "ui backend requires a bot instance with a linked session — "
                            "this call has neither instance_id nor an existing desktop_session_key"
                        )
                    session_key = await asyncio.to_thread(
                        self._sync_create_session, timeout_s, project, workspace_dir
                    )

                text, discovered_key = await asyncio.to_thread(self._sync_ask, prompt, timeout_s, session_key, effort)
            except BackendError:
                raise
            except Exception as exc:
                raise BackendError(f"ui backend error: {exc}") from exc

        raw = {"desktop_session_key": discovered_key} if discovered_key else None
        return BackendResult(text=text, tokens=None, raw=raw)
