"""Chat with one bot instance, real turns — the TUI's counterpart to the dashboard's
"Chat with Bot" mode. Sends through POST /api/chat/send-to-bot, the same route the Android
app uses and that works for any bot instance regardless of its platform (including "app",
which has no other way to be reached at all — see bot_instances.PLATFORMS)."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.screen import Screen
from textual.widgets import Footer, Input, RichLog, Static

from bot.tui.client import ApiError


class ChatScreen(Screen):
    BINDINGS = [("escape", "app.pop_screen", "Back")]

    def __init__(self, bot: dict):
        super().__init__()
        self._bot = bot

    def compose(self) -> ComposeResult:
        yield Static(f"Chat — {self._bot['name']} (#{self._bot['id']})", id="chat-title")
        with Vertical(id="chat-body"):
            yield RichLog(id="chat-log", wrap=True, markup=False)
            yield Input(placeholder="Type a message and press Enter…", id="chat-input")
        yield Footer()

    async def on_mount(self) -> None:
        self.query_one("#chat-input", Input).focus()

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id != "chat-input":
            return
        text = event.value.strip()
        if not text:
            return
        log = self.query_one("#chat-log", RichLog)
        log.write(f"you> {text}")
        event.input.value = ""
        event.input.disabled = True
        try:
            result = await self.app.client.send_to_bot(self._bot["id"], text)
            log.write(f"bot> {result['reply']}")
        except ApiError as exc:
            log.write(f"error: {exc}")
        finally:
            event.input.disabled = False
            event.input.focus()
