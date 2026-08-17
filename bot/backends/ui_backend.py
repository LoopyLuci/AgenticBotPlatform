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
    ):
        self.window_title_re = window_title_re
        self.poll_interval_s = poll_interval_s
        self.input_automation_id = input_automation_id
        self.send_button_automation_id = send_button_automation_id

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

    def _sync_ask(self, prompt: str, timeout_s: float) -> str:
        win = self._connect()
        win.set_focus()

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

    async def ask(self, prompt: str, *, context=None, timeout_s: float = 45) -> BackendResult:
        if platform.system() != "Windows":
            raise BackendError("ui backend is only available on Windows")
        try:
            text = await asyncio.to_thread(self._sync_ask, prompt, timeout_s)
        except BackendError:
            raise
        except Exception as exc:
            raise BackendError(f"ui backend error: {exc}") from exc
        return BackendResult(text=text, tokens=None, raw=None)
