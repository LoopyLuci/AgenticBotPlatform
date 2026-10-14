"""Small-model mode (Local Factory LF2): who gets it, what it trims, and that cloud models are untouched."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from bot.agent_runtime import context_window as cw
from bot.agent_runtime import prompt, small_model, text_toolcalls
from bot.agent_runtime import tools as agent_tools


@pytest.fixture
def cfg(monkeypatch):
    values: dict = {}
    monkeypatch.setattr(small_model, "_cfg", lambda: values)
    monkeypatch.setattr(cw, "_cfg", lambda: {})
    monkeypatch.setattr(small_model, "_announced", set())
    return values


def local(provider="ollama", **extra):
    return SimpleNamespace(provider_key=provider, router_provider=provider, **extra)


def cloud():
    return SimpleNamespace(provider_key="openai", router_provider="openai")


def test_auto_is_on_for_a_small_local_model(cfg):
    p = small_model.profile_for(local(), "unknown-local")           # 8192 default for an unknown local model
    assert p is not None and p.window == 8192


def test_auto_is_off_for_a_large_local_window(cfg):
    assert small_model.profile_for(local(), "llama-3.1-70b") is None   # 128k
    cfg["max_window"] = 200_000
    assert small_model.profile_for(local(), "llama-3.1-70b") is not None


def test_auto_follows_max_window(cfg):
    cfg["max_window"] = 4096
    assert small_model.profile_for(local(), "unknown-local") is None


def test_auto_is_off_for_cloud_even_with_a_small_window(cfg):
    assert small_model.profile_for(cloud(), "gpt-4") is None          # 8192 window, but not a local engine


def test_forced_on_and_off(cfg):
    cfg["enabled"] = True
    assert small_model.profile_for(cloud(), "claude-sonnet-5") is not None
    cfg["enabled"] = "false"
    assert small_model.profile_for(local(), "unknown-local") is None


def test_tools_are_a_subset_of_existing_tools(cfg):
    existing = {s["name"] for s in agent_tools.all_tool_schemas()}
    p = small_model.profile_for(local(), "unknown-local")
    assert p.tools and p.tools <= existing
    assert {"read_file", "list_dir", "grep", "glob", "edit_file", "write_file", "run_shell"} <= p.tools
    assert "hosting_dns" not in p.tools and len(p.tools) < len(existing) / 2


def test_configured_tools_ignore_unknown_names(cfg):
    cfg["tools"] = ["read_file", "no_such_tool"]
    assert small_model.profile_for(local(), "unknown-local").tools == frozenset({"read_file"})


def test_filter_schemas(cfg):
    schemas = agent_tools.all_tool_schemas()
    assert small_model.filter_schemas(schemas, None) is schemas
    p = small_model.profile_for(local(), "unknown-local")
    assert {s["name"] for s in small_model.filter_schemas(schemas, p)} == set(p.tools)


def test_compact_prompt_is_shorter_and_skips_optional_sections():
    full = prompt.build(None, include_agents=True)
    short = prompt.build(None, include_agents=True, compact=True)
    assert len(short) < len(full)
    assert "Navigate code" in full and "Navigate code" not in short
    assert "Do the task, then check it" in short
    names = {n for n, _ in prompt.sections(None, include_agents=True, compact=True)}
    assert not names & prompt.COMPACT_SKIP_SECTIONS


def test_manage_thresholds_are_overridden(cfg):
    history = [{"role": "user", "content": "x" * 7000}]
    transport = local()
    plain = cw.manage(history, transport, "unknown-local")                       # 8192 * 0.7 = 5734 tokens
    assert plain.window == 8192 and plain.before > 0
    cfg["compact_at"], cfg["keep_tool_results"] = 0.1, 1
    seen = {}
    transport.prune_tool_results = lambda h, keep: (seen.setdefault("keep", keep), (h, 0))[1]
    p = small_model.profile_for(transport, "unknown-local")
    cw.manage(history, transport, "unknown-local", small=p)
    assert seen["keep"] == 1
    assert cw.manage(history, transport, "unknown-local", small=p).needs_summary is True
    assert cw.manage(history, transport, "unknown-local").needs_summary is False   # no profile: unchanged


def test_defaults_for_manage(cfg):
    p = small_model.profile_for(local(), "unknown-local")
    assert (p.compact_at, p.keep_tool_results) == (0.6, 3)


def test_tool_mode_text_only_when_transport_cannot_do_native(cfg, monkeypatch):
    monkeypatch.setattr("bot.config.config", SimpleNamespace(current={"native_agent": {"tool_call_modes": {}}}))
    assert text_toolcalls.mode_for("unknown-local", "ollama", local()) == "native"
    assert text_toolcalls.mode_for("unknown-local", "ollama", local(supports_native_tools=False)) == "text"
    # an explicit entry always wins
    monkeypatch.setattr("bot.config.config", SimpleNamespace(
        current={"native_agent": {"tool_call_modes": {"unknown-local": "grammar"}}}))
    assert text_toolcalls.mode_for("unknown-local", "ollama", local(supports_native_tools=False)) == "grammar"
    # not small -> never changed
    assert text_toolcalls.mode_for("gpt-4", "openai", SimpleNamespace(provider_key="openai", supports_native_tools=False)) == "native"


def test_announced_once_per_session(cfg):
    notes = []
    p = small_model.profile_for(local(), "unknown-local")
    assert small_model.announce("s1", p, "unknown-local", notes.append) is True
    assert small_model.announce("s1", p, "unknown-local", notes.append) is False
    assert small_model.announce("s2", p, "unknown-local", notes.append) is True
    assert small_model.announce("s3", None, "gpt-4", notes.append) is False
    assert len(notes) == 2 and "small-model mode" in notes[0]


def test_bare_loopback_base_url_counts_as_local_for_auto(cfg):
    def bare(url):
        return SimpleNamespace(provider_key="openai", router_provider="", base_url=url)

    for url in ("http://127.0.0.1:8080/v1", "http://localhost:1234/v1", "http://[::1]:8000/v1"):
        assert small_model.profile_for(bare(url), "unknown-local") is not None, url
    for url in ("https://api.example.com/v1", "http://192.168.1.5:8080/v1", ""):
        assert small_model.profile_for(bare(url), "unknown-local") is None, url
    assert small_model.profile_for(SimpleNamespace(provider_key="openai", router_provider="openai"), "gpt-4") is None


def test_filter_allowed_intersects_but_keeps_edit_and_verify_tools(cfg):
    schemas = agent_tools.all_tool_schemas()
    p = small_model.profile_for(local(), "unknown-local")
    allowed = [s for s in schemas if s["name"] in {"read_file", "hosting_dns", "run_tests", "edit_file"}]
    assert {s["name"] for s in small_model.filter_allowed(allowed, p)} == {"read_file", "run_tests", "edit_file"}
    cfg["tools"] = ["read_file"]
    p = small_model.profile_for(local(), "unknown-local")
    assert {s["name"] for s in small_model.filter_allowed(allowed, p)} == {"read_file", "run_tests", "edit_file"}
    only_extra = [s for s in schemas if s["name"] == "hosting_dns"]
    assert small_model.filter_allowed(only_extra, p) == only_extra            # nothing left: send what was allowed
    assert small_model.filter_allowed(allowed, None) is allowed
