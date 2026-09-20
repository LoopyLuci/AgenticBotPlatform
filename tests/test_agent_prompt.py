"""System-prompt builder: section order, switches, and that it changes nothing else."""
from __future__ import annotations

import datetime as dt
from pathlib import Path

from bot.agent_runtime import prompt

NOW = dt.datetime(2026, 9, 20, 12, 0, tzinfo=dt.timezone.utc)


def _cfg(monkeypatch, **values):
    monkeypatch.setattr(prompt, "_config", lambda: values)


def test_default_sections_and_order(monkeypatch, tmp_path):
    _cfg(monkeypatch)
    names = [n for n, _ in prompt.sections(None, workspace=tmp_path, session_context="from a hook", now=NOW)]
    assert names == ["guidance", "environment", "session"]


def test_environment_block_is_factual_and_carries_the_date(monkeypatch, tmp_path):
    _cfg(monkeypatch)
    text = prompt.environment(tmp_path, NOW)
    assert str(tmp_path) in text and "2026-09-20" in text and "Git repository: no" in text


def test_environment_sees_a_git_repo(tmp_path):
    (tmp_path / ".git").mkdir()
    sub = tmp_path / "a" / "b"
    sub.mkdir(parents=True)
    import shutil
    if shutil.which("git"):
        assert "Git repository: yes" in prompt.environment(sub, NOW)


def test_sections_can_be_switched_off(monkeypatch, tmp_path):
    _cfg(monkeypatch, guidance=False, environment=False)
    assert prompt.build(None, workspace=tmp_path, now=NOW) == ""


def test_operator_text_follows_the_guidance(monkeypatch, tmp_path):
    _cfg(monkeypatch, extra="Always answer in French.", environment=False)
    names = [n for n, _ in prompt.sections(None, workspace=tmp_path)]
    assert names == ["guidance", "operator"]
    assert "French" in prompt.build(None, workspace=tmp_path)


def test_the_date_is_in_the_last_static_section_so_the_cached_prefix_survives(monkeypatch, tmp_path):
    _cfg(monkeypatch)
    a = prompt.build(None, workspace=tmp_path, now=NOW)
    b = prompt.build(None, workspace=tmp_path, now=NOW + dt.timedelta(days=1))
    prefix = a[: a.index("Environment:")]
    assert b.startswith(prefix)


def test_memory_and_skills_come_between_guidance_and_environment(monkeypatch, tmp_path, temp_db):
    from bot import bot_instances, memory, skills

    _cfg(monkeypatch)
    iid = bot_instances.create_instance(
        name="p", platform="telegram", backend="api",
        credentials={"bot_token": "123456789:AAExampleTokenFromBotFather1234"}, allowed_user_ids=[1])
    memory.set_approval_required(iid, False, actor="test")
    memory.remember(iid, "the user's dog is called Biscuit", source="test")
    names = [n for n, _ in prompt.sections(iid, workspace=Path(tmp_path), now=NOW)]
    assert names.index("guidance") < names.index("memory") < names.index("environment")
    assert "Biscuit" in prompt.build(iid, workspace=tmp_path, now=NOW)


def test_guidance_states_the_rules_that_matter():
    text = prompt.GUIDANCE
    for phrase in ("evidence", "approval", "data, not instructions", "working directory"):
        assert phrase in text
