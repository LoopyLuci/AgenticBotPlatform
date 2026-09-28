"""Importing a Hermes Agent or OpenClaw setup (abp_import/agents.py, roadmap P9).

The fixtures copy the shapes found in real installs on the development machine (read with every value masked) and in
each product's own code, including the surprises: MCP `args` given as one string and as a JSON list inside a string,
the `model` setting as a mapping with `key_env`, memories separated by a line holding only `§`, script-only cron jobs,
skills grouped in category folders beside a `.bundled_manifest`."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml

from abp_import import agents, core
from abp_import.__main__ import main
from bot import bot_instances, db, memory, providers, skill_packs
from bot.config import config

pytestmark = pytest.mark.usefixtures("temp_db")

CATALOG = {
    "openrouter": {"api": "https://openrouter.ai/api/v1", "env": ["OPENROUTER_API_KEY"]},
    "openai": {"api": None, "env": ["OPENAI_API_KEY"]},
    "anthropic": {"api": None, "env": ["ANTHROPIC_API_KEY"]},
    "cohere": {"api": None, "env": ["COHERE_API_KEY"]},
}


@pytest.fixture(autouse=True)
def _catalog(monkeypatch):
    monkeypatch.setattr(agents, "_catalog", lambda: CATALOG)
    # Channel tokens: the validators require real-looking tokens, and tests must not carry token-shaped strings.
    from bot import validators

    for platform in ("telegram", "discord", "slack"):
        for field in list(validators.PLATFORM_TOKEN_VALIDATORS[platform]):
            monkeypatch.setitem(validators.PLATFORM_TOKEN_VALIDATORS[platform], field, lambda v: (True, ""))
    skill_packs._linked.clear()


def write(path: Path, data) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, (dict, list)):
        data = yaml.safe_dump(data) if path.suffix in (".yaml", ".yml") else json.dumps(data)
    path.write_text(data, encoding="utf-8")
    return path


def skill(root: Path, rel: str, description: str = "does a thing") -> None:
    name = Path(rel).name
    write(root / rel / "SKILL.md", f"---\nname: {name}\ndescription: {description}\n---\nSteps for {name}.\n")


def hermes_home(tmp_path: Path) -> Path:
    home = tmp_path / "hermes"
    write(home / "config.yaml", {
        "model": {"provider": "nous", "default": "meituan/longcat-2.0:free", "base_url": "https://inference-api.example/v1",
                  "key_env": "NOUS_KEY", "api_mode": "chat_completions"},
        "providers": {"ollama": {"name": "ollama", "base_url": "http://127.0.0.1:11434/v1", "model": "qwen3:4b",
                                 "discover_models": True, "models": {"qwen3:4b": {}}},
                      "unsloth": {"base_url": "http://127.0.0.1:8888/v1", "key_env": "UNSLOTH_KEY"}},
        "fallback_providers": [{"provider": "ollama", "model": "llama3.1:8b"}, {"provider": "anthropic", "model": "claude-x"},
                               {"provider": "mystery", "model": "m"}],
        "mcp_servers": {
            "arduino": {"command": "python", "args": '["Z:/tools/arduino server.py", "--port", "8"]', "timeout": 180},
            "saver": {"command": "python", "args": "-m saver --flag", "env": {"SS_API_URL": "http://127.0.0.1:8125"}},
            "trader": {"url": "http://127.0.0.1:8128/mcp", "timeout": 120},
            "filtered": {"command": "srv", "tools": {"include": ["a"]}},
        },
        "approvals": {"mode": "off", "timeout": 1000},
        "command_allowlist": ["recursive delete", "chmod 777"],
        "security": {"website_blocklist": {"enabled": True, "domains": ["evil.example", "ads.example"]}},
        "terminal": {"backend": "docker", "docker_image": "python:3.11"},
        "personalities": {"pirate": "arr"},
        "skills": {"external_dirs": [str(tmp_path / "more-skills")]},
    })
    write(home / ".env", "OPENROUTER_API_KEY=unused-openrouter\nNOUS_KEY=unused-nous\nUNSLOTH_KEY=unused-unsloth\n"
                         "ANTHROPIC_API_KEY=unused-anthropic\nCOHERE_API_KEY=unused-cohere\nMYSTERY_API_KEY=unused\n"
                         "TELEGRAM_BOT_TOKEN=unused-telegram\nTELEGRAM_ALLOWED_USERS=111, 222\n"
                         "DISCORD_BOT_TOKEN=unused-discord\n# a comment\nEMPTY_KEY=\n")
    write(home / "SOUL.md", "You are Hermes, a careful assistant.\n")
    write(home / "memories" / "MEMORY.md", "The server runs on port 8123\n§\nBackups go to D:\n§\n\n§\nDeploys happen on Fridays")
    write(home / "memories" / "USER.md", "Prefers short answers\n§\nIs in New York")
    skills = home / "skills"
    skill(skills, "software-development/android-gradle-workflow", "Build Android apps with Gradle")
    skill(skills, "my-own-skill")
    skill(skills, "claude-code", "a skill that ships with Hermes")
    skill(skills, ".archive/old-skill")
    write(skills / ".bundled_manifest", "claude-code:0123abcd\n")
    skill(tmp_path / "more-skills", "extra-skill")
    write(home / "hooks" / "telegram-lifecycle" / "HOOK.yaml", "name: x\nevents: [gateway:startup]\n")
    write(home / "auth.json", "{}")
    write(home / "cron" / "jobs.json", {"jobs": [
        {"id": "a", "name": "Hourly check", "prompt": "Check the queue", "schedule": {"kind": "interval", "minutes": 60},
         "deliver": "telegram:111", "repeat": {"times": None}},
        {"id": "b", "name": "Morning brief", "prompt": "Summarise the news", "schedule": {"kind": "cron", "expr": "0 9 * * *"},
         "deliver": "local", "repeat": {"times": 5}},
        {"id": "c", "name": "Skills sync", "prompt": "x", "script": "sync.sh", "no_agent": True,
         "schedule": {"kind": "interval", "minutes": 60}},
        {"id": "d", "name": "Odd", "prompt": "y", "schedule": {"kind": "cron", "expr": "0 9 1 * *", "display": "monthly"}},
        {"id": "e", "name": "Once", "prompt": "z", "schedule": {"kind": "once", "run_at": "2026-01-01T00:00:00"}},
    ]})
    return home


def hermes_plan(tmp_path):
    return agents.hermes(tmp_path, tmp_path, hermes_home(tmp_path))


# ---- reading Hermes ------------------------------------------------------------------------------------------------
def test_hermes_providers_come_from_config_model_and_env(tmp_path):
    plan = hermes_plan(tmp_path)
    got = {p["name"]: p for p in plan.providers}
    assert set(got) == {"ollama", "unsloth", "nous", "openrouter"}
    assert got["nous"]["base_url"] == "https://inference-api.example/v1" and got["nous"]["api_key"] == "unused-nous"
    assert got["unsloth"]["api_key"] == "unused-unsloth" and got["ollama"]["api_key"] is None
    assert got["openrouter"]["catalog_id"] == "openrouter" and got["openrouter"]["api_key"] == "unused-openrouter"
    notes = "\n".join(plan.warnings)
    assert "Anthropic key is not imported" in notes
    assert "MYSTERY_API_KEY" in notes, "a key the catalog does not know is reported by name"
    assert "COHERE_API_KEY is for cohere, which has no OpenAI-compatible address" in notes


def test_hermes_models_go_to_the_router_but_never_claude(tmp_path):
    plan = hermes_plan(tmp_path)
    assert plan.router_also == ["ollama/qwen3:4b", "nous/meituan/longcat-2.0:free", "ollama/llama3.1:8b"]
    notes = "\n".join(plan.warnings)
    assert "anthropic/claude-x is not added" in notes and "mystery/m is not added" in notes


def test_hermes_mcp_args_in_every_real_shape(tmp_path):
    servers = {s["name"]: s for s in hermes_plan(tmp_path).mcp_servers}
    assert servers["arduino"]["args"] == ["Z:/tools/arduino server.py", "--port", "8"]
    assert servers["saver"]["args"] == ["-m", "saver", "--flag"] and servers["saver"]["env"] == {"SS_API_URL": "http://127.0.0.1:8125"}
    assert servers["trader"]["transport"] == "remote" and servers["trader"]["url"] == "http://127.0.0.1:8128/mcp"


def test_hermes_safety_settings_only_ever_narrow(tmp_path):
    plan = hermes_plan(tmp_path)
    assert plan.mode is None
    assert {(r["decision"], r["tool"], r["match"]) for r in plan.rules} == {("deny", "web_fetch", "evil.example"),
                                                                           ("deny", "web_fetch", "ads.example")}
    notes = "\n".join(plan.warnings)
    for expected in ("approvals were off", "2 command patterns", "'docker' backend", "hooks are not imported",
                     "telegram-lifecycle", "OAuth logins", "personalities are not imported", "limits which tools"):
        assert expected in notes, expected


def test_hermes_skills_are_linked_without_the_bundled_ones(tmp_path):
    plan = hermes_plan(tmp_path)
    libs = {Path(d["path"]).name: d for d in plan.skill_dirs}
    assert libs["skills"]["count"] == 2 and libs["skills"]["exclude"] == ["claude-code"]
    assert libs["more-skills"]["count"] == 1


def test_hermes_soul_memories_jobs_and_channels(tmp_path):
    plan = hermes_plan(tmp_path)
    assert plan.agent == {"name": "Hermes", "instructions": "You are Hermes, a careful assistant."}
    facts = [m["content"] for m in plan.memories if m["kind"] == "fact"]
    assert facts == ["The server runs on port 8123", "Backups go to D:", "Deploys happen on Fridays"]
    assert [m["content"] for m in plan.memories if m["kind"] == "user"] == ["Prefers short answers", "Is in New York"]
    jobs = {s["name"]: s for s in plan.schedules}
    assert set(jobs) == {"Hourly check", "Morning brief"}
    assert jobs["Hourly check"]["interval_s"] == 3600 and jobs["Hourly check"]["platform"] == "telegram"
    assert jobs["Hourly check"]["chat_id"] == "111"
    brief = jobs["Morning brief"]
    first = datetime.fromisoformat(brief["first_run_at"]).astimezone()
    assert brief["interval_s"] == 86400 and (first.hour, first.minute) == (9, 0) and brief["max_runs"] == 5
    notes = "\n".join(plan.warnings)
    assert "'Skills sync' runs a script" in notes and "'Odd' (monthly) does not repeat" in notes and "'Once'" in notes
    assert plan.channels == [{"platform": "telegram", "credentials": {"bot_token": "unused-telegram"},
                              "allowed_user_ids": [111, 222], "origin": ".env"}]
    assert "the discord bot allows no users" in notes


def test_secrets_never_appear_in_the_printed_plan(tmp_path):
    text = core.render(hermes_plan(tmp_path))
    assert "unused-" not in text
    assert "with its API key" in text and "created switched off" in text and "created paused" in text


def test_hermes_home_is_found_the_way_hermes_finds_it(tmp_path):
    home = tmp_path / "user"
    write(home / "AppData" / "Local" / "hermes" / "config.yaml", {"model": ""})
    write(home / ".hermes" / "config.yaml", {"model": ""})
    root, others = agents.hermes_home(home, None)
    assert root == home / "AppData" / "Local" / "hermes" and others == [home / ".hermes"]
    plan = agents.hermes(tmp_path, home)
    assert any("another Hermes folder" in w for w in plan.warnings)
    assert agents.hermes_home(tmp_path / "nobody", None) == (None, [])
    assert "no Hermes configuration found" in agents.hermes(tmp_path, tmp_path / "nobody").warnings[0]


# ---- cron expressions ----------------------------------------------------------------------------------------------
@pytest.mark.parametrize("expr, interval", [("*/15 * * * *", 900), ("30 * * * *", 3600), ("0 */6 * * *", 21600),
                                            ("0 9 * * *", 86400), ("0 9 * * 1", 604800), ("0 9 * * 0", 604800)])
def test_fixed_interval_crons_convert(expr, interval):
    now = datetime(2026, 9, 28, 10, 7, tzinfo=timezone.utc)                 # a Monday
    got, first = agents.cron_interval(expr, now)
    assert got == interval and first > now and (first - now).total_seconds() <= interval
    if expr == "0 9 * * 1":
        assert first.weekday() == 0 and first.day == 5                     # next Monday, not today (already past 9:00)
    if expr == "0 9 * * 0":
        assert first.weekday() == 6


@pytest.mark.parametrize("expr", ["0 9 1 * *", "0 9 * * 1-5", "*/7 * * * *", "0 */5 * * *", "bad", "0 25 * * *"])
def test_irregular_crons_are_not_approximated(expr):
    assert agents.cron_interval(expr, datetime(2026, 9, 28, tzinfo=timezone.utc)) is None


# ---- applying ------------------------------------------------------------------------------------------------------
def test_applying_a_hermes_plan(tmp_path):
    plan = hermes_plan(tmp_path)
    done = core.apply(plan)
    assert done["providers"] == 4 and done["mcp_servers"] == 4 and done["rules"] == 2
    assert done["router_models"] == 3 and done["skill_libraries"] == 2 and done["memories"] == 5 and done["schedules"] == 2
    assert providers.get_api_key("openrouter") == "unused-openrouter"
    na = config.current["native_agent"]
    assert na["router"]["also"][-3:] == plan.router_also
    linked = na["skills"]["external_dirs"]
    assert {"path": str(tmp_path / "hermes" / "skills"), "exclude": ["claude-code"]} in linked

    bots = {b["name"]: b for b in bot_instances.list_instances()}
    tg = bots["Hermes on Telegram"]
    assert tg["platform"] == "telegram" and not tg["enabled"] and tg["model"] == "auto"
    assert tg["custom_instructions"] == "You are Hermes, a careful assistant."
    assert done["bot_id"] == tg["id"]
    mems = memory.listing(tg["id"])
    assert len(mems) == 5 and all(m["status"] == "approved" for m in mems)
    jobs = db.list_scheduled_commands(tg["id"])
    assert len(jobs) == 2 and not any(j["enabled"] for j in jobs)
    assert {j["chat_id"] for j in jobs} == {"111", "import"}


def test_applying_twice_adds_nothing_twice(tmp_path):
    core.apply(hermes_plan(tmp_path))
    again = core.apply(hermes_plan(tmp_path))
    assert again["providers"] == 0 and again["router_models"] == 0 and again["skill_libraries"] == 0 and again["mcp_servers"] == 0
    assert again["memories"] == 0 and again["schedules"] == 0 and again["bots"] == 0, "a re-import reuses what the first made"
    assert len(bot_instances.list_instances()) == 1
    third = hermes_plan(tmp_path)
    core.apply(third)
    assert any("a telegram bot with that token already exists" in w for w in third.warnings)


def test_the_linked_library_really_serves_skills(tmp_path):
    core.apply(hermes_plan(tmp_path))
    found = skill_packs.discover(None)
    assert {"android-gradle-workflow", "my-own-skill", "extra-skill"} <= set(found)
    assert "claude-code" not in found and "old-skill" not in found
    assert found["android-gradle-workflow"].source == "linked"
    assert [s.name for s in skill_packs.search(None, "gradle android")] == ["android-gradle-workflow"]


def test_no_secrets_imports_no_key_token_or_channel(tmp_path):
    plan = hermes_plan(tmp_path)
    done = core.apply(plan, with_secrets=False)
    assert providers.get_api_key("openrouter") is None and done["providers"] == 4
    assert all(b["platform"] == "app" for b in bot_instances.list_instances())
    servers = {r["name"]: r for r in db.list_external_mcp_servers()}
    assert json.loads(servers["saver"]["env_json"]) == {"SS_API_URL": ""}
    assert "chat channels were not imported (--no-secrets)" in plan.warnings


def test_an_existing_bot_keeps_its_own_instructions(tmp_path):
    iid = bot_instances.create_instance(name="Mine", platform="app", backend="native_agent", credentials={}, allowed_user_ids=[],
                                        custom_instructions="My own rules")
    plan = hermes_plan(tmp_path)
    plan.channels = []
    done = core.apply(plan, instance_id=iid)
    assert done["bot_id"] == iid and done["bots"] == 0
    assert bot_instances.get_instance(iid)["custom_instructions"] == "My own rules"
    assert "already has instructions" in "\n".join(plan.warnings) and len(memory.listing(iid)) == 5
    with pytest.raises(ValueError, match="no bot instance"):
        core.apply(hermes_plan(tmp_path), instance_id=99999)


def test_an_existing_provider_is_left_alone(tmp_path):
    providers.set_provider("openrouter", "https://example.invalid/v1", api_key="mine")
    plan = hermes_plan(tmp_path)
    core.apply(plan)
    assert providers.get_provider("openrouter")["base_url"] == "https://example.invalid/v1"
    assert "provider 'openrouter' already exists" in "\n".join(plan.warnings)


# ---- OpenClaw ------------------------------------------------------------------------------------------------------
def openclaw_home(tmp_path: Path) -> Path:
    home = tmp_path / "openclaw"
    write(home / "openclaw.json", """{
      // OpenClaw's config is JSON5: comments and trailing commas are allowed
      "agents": {"defaults": {"model": {"primary": "ollama/glm-4.7-flash", "fallbacks": ["openrouter/x-ai/grok:free", "anthropic/claude-z"]}}},
      "env": {"vars": {"OR_KEY": "unused-or"}},
      "models": {"providers": {
        "ollama": {"api": "openai-completions", "apiKey": "ollama-local", "baseUrl": "http://127.0.0.1:11434/v1",
                   "models": [{"id": "glm-4.7-flash", "contextWindow": 131072}]},
        "openrouter": {"api": "openai-completions", "apiKey": "${OR_KEY}", "baseUrl": "https://openrouter.ai/api/v1"},
        "fromenv": {"api": "openai-responses", "apiKey": {"source": "env", "id": "FROMENV_KEY"}, "baseUrl": "https://r.example/v1"},
        "vaulted": {"api": "openai-completions", "apiKey": {"source": "exec", "command": "pass show x"}, "baseUrl": "https://v.example/v1"},
        "claude": {"api": "anthropic-messages", "apiKey": "unused", "baseUrl": "https://api.anthropic.com"},
      }},
      "mcp": {"servers": {"fs": {"command": "npx", "args": ["-y", "server-fs"], "env": {"ROOT": "/tmp"}}}},
      "tools": {"deny": ["exec", "browser", "canvas"]},
      "approvals": {"exec": {"mode": "auto"}},
      "channels": {
        "telegram": {"accounts": {"default": {"botToken": "unused-tg"}}},
        "discord": {"token": "${DISCORD_TOKEN}", "allowFrom": ["333"]},
        "whatsapp": {"allowFrom": ["+1555"]},
      },
      "cron": {"enabled": true},
    }""")
    write(home / ".env", "FROMENV_KEY=unused-fromenv\nDISCORD_TOKEN=unused-dc\n")
    write(home / "credentials" / "telegram-default-allowFrom.json", {"allowFrom": ["444", "555"]})
    write(home / "exec-approvals.json", {"agents": {"main": {"allowlist": [{"pattern": "/usr/bin/git"}]}}})
    ws = home / "workspace"
    write(ws / "SOUL.md", "Be kind.")
    write(ws / "IDENTITY.md", "Name: Claw")
    write(ws / "AGENTS.md", "Read HEARTBEAT.md every session.")
    write(ws / "USER.md", "# USER.md\n## Preferences\n- Likes tea\n- Works late\n")
    write(ws / "MEMORY.md", "# Projects\nABP is the main project.\n\n```\ncode is skipped\n```\n| a | table |\n")
    write(ws / "memory" / "2026-09-01.md", "- Deployed v2\n- Deployed v2\n")
    skill(ws / "skills", "ws-skill")
    skill(home / "skills", "managed-skill")
    return home


def test_openclaw_plan(tmp_path):
    plan = agents.openclaw(tmp_path, tmp_path, openclaw_home(tmp_path))
    got = {p["name"]: p for p in plan.providers}
    assert set(got) == {"ollama", "openrouter", "fromenv", "vaulted"}
    assert got["openrouter"]["api_key"] == "unused-or" and got["fromenv"]["api_key"] == "unused-fromenv"
    assert got["fromenv"]["protocol"] == "responses" and got["vaulted"]["api_key"] is None
    assert plan.router_also == ["ollama/glm-4.7-flash", "openrouter/x-ai/grok:free"]
    assert [s["name"] for s in plan.mcp_servers] == ["fs"]
    assert {(r["tool"], r["decision"]) for r in plan.rules} == {("run_shell", "deny"), ("browser", "deny")}
    assert plan.agent == {"name": "OpenClaw", "instructions": "Be kind.\n\nName: Claw"}
    assert [m["content"] for m in plan.memories if m["kind"] == "user"] == ["Preferences: Likes tea", "Preferences: Works late"]
    assert [m["content"] for m in plan.memories if m["kind"] == "fact"] == ["Projects: ABP is the main project.", "Deployed v2"]
    assert sorted(Path(d["path"]).parent.name for d in plan.skill_dirs) == ["openclaw", "workspace"]
    chans = {c["platform"]: c for c in plan.channels}
    assert chans["telegram"]["credentials"] == {"bot_token": "unused-tg"} and chans["telegram"]["allowed_user_ids"] == [444, 555]
    assert chans["discord"]["credentials"] == {"bot_token": "unused-dc"} and chans["discord"]["allowed_user_ids"] == [333]
    notes = "\n".join(plan.warnings)
    for expected in ("reads its key from a exec", "'anthropic-messages' API", "claude-z is not added", "'canvas' has no ABP",
                     "ran commands without asking", "exec-approvals.json", "scheduled jobs are not imported",
                     "AGENTS.md is not imported", "whatsapp channel is not imported"):
        assert expected in notes, expected
    assert "unused-" not in core.render(plan)


def test_openclaw_apply_and_the_legacy_config_name(tmp_path):
    home = openclaw_home(tmp_path)
    (home / "openclaw.json").rename(home / "clawdbot.json")
    plan = agents.openclaw(tmp_path, tmp_path, home)
    done = core.apply(plan)
    assert done["providers"] == 4 and done["bots"] == 2 and done["memories"] == 4
    bots = {b["platform"]: b for b in bot_instances.list_instances()}
    assert set(bots) == {"telegram", "discord"} and not any(b["enabled"] for b in bots.values())
    assert agents.openclaw(tmp_path, tmp_path / "nobody").warnings[0].startswith("no OpenClaw configuration found")


def test_markdown_entries_keep_their_headings_and_skip_code():
    assert agents.markdown_entries("# A\n## B\ntext one\ncontinues\n\n* bullet\n```\nno\n```\n") == [
        "A > B: text one continues", "A > B: bullet"]


# ---- the command line ----------------------------------------------------------------------------------------------
def test_the_command_line(tmp_path, capsys):
    home = hermes_home(tmp_path)
    assert main(["hermes", "--source", str(home)]) == 0
    out = capsys.readouterr().out
    assert "Dry run" in out and "skills link" in out and "unused-" not in out and not bot_instances.list_instances()
    assert main(["hermes", "--source", str(home), "--apply"]) == 0
    out = capsys.readouterr().out
    assert "Applied:" in out and "on bot" in out and "switched off" in out
    assert main(["openclaw", "--source", str(tmp_path / "nothing-here")]) == 0
    assert "no OpenClaw configuration found" in capsys.readouterr().out


def test_an_import_keeps_the_config_files_comments(tmp_path):
    before = Path(config.path).read_text(encoding="utf-8")
    assert "# " in before
    core.apply(hermes_plan(tmp_path))
    after = Path(config.path).read_text(encoding="utf-8")
    comments = [line.strip() for line in before.splitlines() if line.strip().startswith("#")]
    assert comments and all(c in after for c in comments), "backends.yaml is heavily documented; an import must not strip it"
