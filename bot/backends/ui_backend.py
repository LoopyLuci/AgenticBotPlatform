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


class UiBackend(Backend):
    name = "ui"

    def __init__(
        self,
        window_title_re: str = "Claude.*",
        poll_interval_s: float = 0.5,
        input_automation_id: Optional[str] = None,
        send_button_automation_id: Optional[str] = None,
        new_chat_button_automation_id: Optional[str] = None,
        sidebar_item_control_type: str = "ListItem",
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
        for btn in win.descendants(control_type="Button"):
            try:
                if "send" in (btn.window_text() or "").lower():
                    return btn
            except Exception:
                continue
        return None

    def _find_new_chat_button(self, win):
        if self.new_chat_button_automation_id:
            return win.child_window(auto_id=self.new_chat_button_automation_id, control_type="Button")
        for btn in win.descendants(control_type="Button"):
            try:
                name = (btn.window_text() or "").lower()
                if "new chat" in name or name.strip() == "+":
                    return btn
            except Exception:
                continue
        raise BackendError(
            "no \"New chat\" button found in the Claude Desktop window — "
            "set backends.ui.new_chat_button_automation_id in config/backends.yaml"
        )

    def _sidebar_items(self, win) -> list:
        items = win.descendants(control_type=self.sidebar_item_control_type)
        if not items:
            raise BackendError(
                f"no sidebar items (control_type={self.sidebar_item_control_type!r}) found in the "
                "Claude Desktop window — set backends.ui.sidebar_item_control_type in config/backends.yaml"
            )
        return items

    def _sync_create_session(self, timeout_s: float) -> str:
        win = self._connect()
        win.set_focus()
        btn = self._find_new_chat_button(win)
        btn.click_input()
        time.sleep(self.poll_interval_s)

        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            items = self._sidebar_items(win)
            for item in items:
                try:
                    label = (item.window_text() or "").strip()
                except Exception:
                    continue
                if label:
                    return label
            time.sleep(self.poll_interval_s)
        raise BackendError("created a new chat but could not read its sidebar label to link it to this bot instance")

    def _select_session(self, win, session_key: str) -> None:
        for item in self._sidebar_items(win):
            try:
                label = (item.window_text() or "").strip()
            except Exception:
                continue
            if label == session_key:
                item.click_input()
                return
        raise BackendError(
            f"linked chat {session_key!r} is no longer in the Claude Desktop sidebar (renamed or deleted there) — "
            "create a new session for this bot instance to relink it"
        )

    def _sync_ask(self, prompt: str, timeout_s: float, session_key: str) -> str:
        win = self._connect()
        win.set_focus()
        self._select_session(win, session_key)

        before_texts = {t.strip() for t in self._collect_texts(win) if t and t.strip()}

        field = self._find_input(win)
        field.set_focus()
        field.type_keys(prompt, with_spaces=True, with_tabs=True, with_newlines=False)

        send_btn = self._find_send_button(win)
        if send_btn is not None:
            send_btn.click_input()
        else:
            field.type_keys("{ENTER}")

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
                    return "\n".join(sorted(new_text, key=len, reverse=True)[:1] or new_text)
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

    async def create_session(self, timeout_s: float = 45) -> str:
        """Explicitly opens a brand-new chat in the real Claude Desktop
        window and returns its sidebar label as the session key the caller
        (Router.create_session) should persist against the bot instance."""
        if platform.system() != "Windows":
            raise BackendError("ui backend is only available on Windows")
        async with self._lock:
            try:
                return await asyncio.to_thread(self._sync_create_session, timeout_s)
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

        async with self._lock:
            try:
                created_key: Optional[str] = None
                if force_new or not session_key:
                    if instance_id is None:
                        raise BackendError(
                            "ui backend requires a bot instance with a linked session — "
                            "this call has neither instance_id nor an existing desktop_session_key"
                        )
                    created_key = await asyncio.to_thread(self._sync_create_session, timeout_s)
                    session_key = created_key

                text = await asyncio.to_thread(self._sync_ask, prompt, timeout_s, session_key)
            except BackendError:
                raise
            except Exception as exc:
                raise BackendError(f"ui backend error: {exc}") from exc

        raw = {"desktop_session_key": created_key} if created_key else None
        return BackendResult(text=text, tokens=None, raw=raw)
