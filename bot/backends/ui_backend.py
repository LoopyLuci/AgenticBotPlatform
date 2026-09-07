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

    previous = None
    try:
        win32clipboard.OpenClipboard()
        try:
            previous = win32clipboard.GetClipboardData(win32clipboard.CF_UNICODETEXT)
        except Exception:
            previous = None
        win32clipboard.EmptyClipboard()
        win32clipboard.SetClipboardData(win32clipboard.CF_UNICODETEXT, text)
    finally:
        win32clipboard.CloseClipboard()

    try:
        field.type_keys("^v")
    finally:
        try:
            win32clipboard.OpenClipboard()
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

    def _sync_create_session(self, timeout_s: float, project: Optional[str] = None) -> str:
        """Opens a brand-new chat and returns immediately with
        UNTITLED_SESSION_KEY — see that constant's docstring for why no
        real key can be read yet. The caller (ask(), or a later ask() for
        a chat whose only prior create_session() call returned this same
        placeholder) is responsible for discovering and persisting the
        real title once a first message actually names the session."""
        win = self._connect()
        win.set_focus()
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

    def _sync_ask(self, prompt: str, timeout_s: float, session_key: Optional[str]) -> tuple[str, Optional[str]]:
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
        while time.monotonic() < deadline:
            time.sleep(self.poll_interval_s)
            current = {t.strip() for t in self._collect_texts(win) if t and t.strip()}
            new_text = current - before_texts
            if new_text and current == last_texts:
                stable_reads += 1
                if stable_reads >= 2:  # unchanged across two polls = response finished streaming
                    # Confirmed live: Desktop emits an accessibility-style
                    # announcement text "Claude responded: <reply>" right
                    # alongside the actual visible reply text (and
                    # similarly "You said: <prompt>" for the echoed
                    # prompt) — stripping that prefix is a direct,
                    # positive source for the real reply, far more
                    # reliable than guessing from length: the earlier
                    # "pick the longest new string" heuristic picked the
                    # echoed PROMPT itself whenever it was longer than the
                    # actual reply.
                    announced = next(
                        (t[len("Claude responded: "):] for t in new_text if t.startswith("Claude responded: ")),
                        None,
                    )
                    if announced is not None:
                        reply = announced
                    else:
                        candidates = {
                            t for t in new_text
                            if not t.startswith("You said: ")
                            and not t.startswith("Claude responded: ")
                            and t != prompt_stripped
                        } or new_text
                        reply = "\n".join(sorted(candidates, key=len, reverse=True)[:1] or candidates)
                    discovered = None
                    if untitled:
                        # Confirmed live: the sidebar's auto-generated
                        # title lags a couple of seconds behind the reply
                        # text itself becoming stable — polling only once,
                        # right here, missed a title that reliably showed
                        # up moments later. A short bounded retry (not the
                        # full timeout_s budget) covers that lag without
                        # risking a slow permanent hang.
                        # Confirmed live: re-using the same WindowSpecification
                        # object across repeated descendants() calls in this
                        # loop never saw the new title appear no matter how
                        # long the wait, while a freshly reconnected window
                        # handle found it within a fraction of a second — a
                        # real pywinauto/UIA element-tree caching gotcha on
                        # a long-lived handle, not an actual Desktop delay.
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
                            # Ambiguous (more than one new title appeared,
                            # e.g. another instance's ask() also just
                            # finished) — better to leave this session
                            # unlinked than link it to the wrong title.
                            logger.warning("ui backend: %d new session titles appeared, could not disambiguate", len(new_labels))
                        else:
                            logger.warning("ui backend: sent a message into an untitled session but no new sidebar title appeared yet")
                    return reply, discovered
            else:
                stable_reads = 0
            last_texts = current

        raise BackendError(f"ui backend timed out after {timeout_s}s waiting for a reply")

    @staticmethod
    def _collect_texts(win) -> list[str]:
        texts = []
        for el in win.descendants(control_type="Text"):
            try:
                texts.append(el.window_text())
            except Exception:
                continue
        return texts

    async def create_session(self, timeout_s: float = 45, project: Optional[str] = None) -> str:
        """Explicitly opens a brand-new chat in the real Claude Desktop
        window (in [project] if given, matching one of list_projects()'s
        names, otherwise the bare "New" button outside any project) and
        returns UNTITLED_SESSION_KEY — see that constant's docstring.
        Persist this placeholder exactly like a real key; the first ask()
        that consumes it discovers and reports back the real title, which
        Router.ask() already re-persists over this placeholder the same
        way it does for its own lazy-create path."""
        if platform.system() != "Windows":
            raise BackendError("ui backend is only available on Windows")
        async with self._lock:
            try:
                return await asyncio.to_thread(self._sync_create_session, timeout_s, project)
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

        async with self._lock:
            try:
                if force_new or not session_key:
                    if instance_id is None:
                        raise BackendError(
                            "ui backend requires a bot instance with a linked session — "
                            "this call has neither instance_id nor an existing desktop_session_key"
                        )
                    session_key = await asyncio.to_thread(self._sync_create_session, timeout_s, project)

                text, discovered_key = await asyncio.to_thread(self._sync_ask, prompt, timeout_s, session_key)
            except BackendError:
                raise
            except Exception as exc:
                raise BackendError(f"ui backend error: {exc}") from exc

        raw = {"desktop_session_key": discovered_key} if discovered_key else None
        return BackendResult(text=text, tokens=None, raw=raw)
