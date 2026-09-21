"""The desktop boot panel always ends in a finished state, and Chat opens in "Chat with Bot"."""
from __future__ import annotations

import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
MAIN_JS = (ROOT / "desktop-app/ui/main.js").read_text(encoding="utf-8")
WEB = {
    "dashboard": (ROOT / "bot/dashboard/static/dashboard.html").read_text(encoding="utf-8"),
    "desktop": (ROOT / "desktop-app/ui/main.js").read_text(encoding="utf-8"),
}


def _function_body(text: str, signature: str) -> str:
    start = text.index(signature)
    depth, i = 0, text.index("{", start)
    begin = i
    while True:
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return text[begin:i + 1]
        i += 1


def test_the_finished_state_stops_the_spinner_and_says_ready_not_just_the_pill():
    body = _function_body(MAIN_JS, "function markBootReady()")
    assert "'Ready.'" in body
    assert "boot-spinner" in body and "hidden" in body
    assert "setBootPill('ok'" in body


def test_hiding_the_overlay_and_the_already_booted_copy_both_finish_the_panel():
    assert "markBootReady();" in _function_body(MAIN_JS, "function hideBootOverlay()")
    booted = MAIN_JS[MAIN_JS.index("if (alreadyBooted) {\n    markBootReady();"):][:120]
    assert "markBootReady();" in booted
    # the live-served copy must not pop the panel open with a stale "starting" state
    assert "if (backlog.length && !alreadyBooted) expandBoot();" in MAIN_JS


def test_the_readiness_poll_starts_fast_and_is_time_budgeted():
    body = _function_body(MAIN_JS, "async function waitForServerReady(")
    assert "budgetMs" in MAIN_JS[MAIN_JS.index("async function waitForServerReady("):][:80]
    assert re.search(r"let delay = 100;", body)
    assert "Math.min(500" in body
    assert "/healthz" in body


def test_a_failed_listener_cannot_stop_the_boot_from_polling():
    body = _function_body(MAIN_JS, "async function initTauriBoot()")
    assert "const safeListen" in body
    assert "await listen(" not in body.replace("await listen(name, handler)", "")


@pytest.mark.parametrize("name", ["dashboard", "desktop"])
def test_chat_opens_in_chat_with_bot(name):
    text = WEB[name]
    assert "mode: 'bot' }" in text
    assert "setChatMode('bot');" in text
    assert "setChatMode('server');" not in text
    assert "mode: 'server' }" not in text


@pytest.mark.parametrize("rel", ["bot/dashboard/static/dashboard.html", "desktop-app/ui/index.html"])
def test_the_chat_markup_starts_in_chat_with_bot(rel):
    text = (ROOT / rel).read_text(encoding="utf-8")
    assert 'aria-checked="true" aria-label="Chat mode: Chat with Bot"' in text
    assert 'id="chat-mode-switch-text">💬 Chat with Bot<' in text
    assert re.search(r'chat-card mode-bot" id="chat-card"', text)


def test_the_android_chat_defaults_to_chat_with_bot():
    kt = (ROOT / "android-app/app/src/main/kotlin/com/agenticbotplatform/mobile/ui/chat/ChatViewModel.kt").read_text(encoding="utf-8")
    assert "val mode: ChatMode = ChatMode.CHAT_WITH_BOT" in kt
