"""The ABP Agents page is in both UIs, the same in each, and wired into the rest of the app."""
from __future__ import annotations

import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
DASH = (ROOT / "bot/dashboard/static/dashboard.html").read_text(encoding="utf-8")
DESK = (ROOT / "desktop-app/ui/index.html").read_text(encoding="utf-8")
PAGES = {"dashboard": DASH, "desktop": DESK}


def test_the_panel_script_is_identical_in_both_apps():
    dash = (ROOT / "bot/dashboard/static/agents-panel.js").read_text(encoding="utf-8")
    desk = (ROOT / "desktop-app/ui/agents-panel.js").read_text(encoding="utf-8")
    assert dash == desk
    assert "/api/agent/config" in dash and "/api/agent/overview" in dash


@pytest.mark.parametrize("name", ["dashboard", "desktop"])
def test_each_ui_has_the_page_its_nav_item_and_its_script(name):
    text = PAGES[name]
    assert '<section id="agents">' in text and 'id="agents-root"' in text
    assert 'href="#agents"' in text and "ABP Agents" in text
    assert re.search(r'<script src="(/static/)?agents-panel\.js"></script>', text)
    for element in ("agents-tabs", "agents-body", "agents-savebar", "agents-static-subagents"):
        assert f'id="{element}"' in text
    # the page comes before Swarms in the nav, so agents are found first
    assert text.index('href="#agents"') < text.index('href="#swarms"')


@pytest.mark.parametrize("name", ["dashboard", "desktop"])
def test_the_per_bot_agent_settings_card_lives_on_the_agents_page_not_in_automation(name):
    text = PAGES[name]
    start = text.index('<section id="agents">')
    end = text.index('<section id="swarms">')
    agents = text[start:end]
    assert 'id="btn-agent-settings-save"' in agents and 'id="agent-settings-instance"' in agents
    assert text.count('id="btn-agent-settings-save"') == 1  # moved, not duplicated


def test_the_bot_form_script_is_identical_in_both_apps():
    dash = (ROOT / "bot/dashboard/static/bot-agent-form.js").read_text(encoding="utf-8")
    desk = (ROOT / "desktop-app/ui/bot-agent-form.js").read_text(encoding="utf-8")
    assert dash == desk
    assert "own=true" in dash and "/permissions" in dash


@pytest.mark.parametrize("name", ["dashboard", "desktop"])
def test_the_bot_form_offers_abp_agent_first_and_has_its_settings_panel(name):
    text = PAGES[name]
    select = text[text.index('<select id="bot-new-backend">'):]
    select = select[:select.index("</select>")]
    assert select.index("ABP Agent (recommended)") < select.index("Claude apps") < select.index("Hermes Agent")
    assert select.index('value="native_agent"') < select.index('value="cli"')
    assert "custom endpoint + spawn_subagent" not in text
    for element in ("bot-agent-panel", "bot-agent-permission", "bot-agent-max-children", "bot-agent-worker-model",
                    "bot-agent-fallback", "bot-agent-worker-effort", "bot-agent-manager-effort", "bot-agent-plan-approval"):
        assert f'id="{element}"' in text
    assert re.search(r'<script src="(/static/)?bot-agent-form\.js"></script>', text)


@pytest.mark.parametrize("rel", ["bot/dashboard/static/dashboard.html", "desktop-app/ui/main.js"])
def test_the_form_hooks_the_agent_panel_into_reset_load_and_save(rel):
    text = (ROOT / rel).read_text(encoding="utf-8")
    assert "document.getElementById('bot-new-backend').value = 'native_agent'" in text  # ABP Agent is the default
    assert "abpBotAgentForm.reset()" in text and "abpBotAgentForm.load(bot.id)" in text
    assert "abpBotAgentForm.save(savedId)" in text and "savedId = created.id" in text


@pytest.mark.parametrize("rel", ["bot/dashboard/static/dashboard.html", "desktop-app/ui/main.js"])
def test_agent_bots_get_an_agent_settings_button(rel):
    text = (ROOT / rel).read_text(encoding="utf-8")
    assert "data-bot-agent" in text and "abpAgents.openBot" in text
    assert "['native_agent', 'api', 'custom_model'].includes" in text
