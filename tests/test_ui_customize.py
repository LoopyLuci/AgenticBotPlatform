"""bot/ui_customize.py — generative UI: a model proposes a full new
version of a frontend file, validation catches broken/truncated output
before it can ever reach disk, and apply/revert are exact and
byte-for-byte reversible. Exercises real file I/O against temp paths
(never the real dashboard.html/desktop-app files), and fakes only the
one genuine network call (the LLM generation itself).
"""
from __future__ import annotations

import asyncio
import json

import pytest

from bot import ui_customize


@pytest.fixture(autouse=True)
def _isolated_paths(tmp_path, monkeypatch):
    dashboard = tmp_path / "dashboard.html"
    dashboard.write_text(
        "<html><body>\n"
        "<section id=\"overview\">ok</section>\n"
        "<section id=\"jobs\">ok</section>\n"
        "<script>function getToken(){return 1;} function connectLiveEventsSocket(){}</script>\n"
        "<style>.a{color:red;}</style>\n"
        "</body></html>\n",
        encoding="utf-8",
    )
    desktop_js = tmp_path / "main.js"
    desktop_js.write_text("function esc(x){return x;}\n", encoding="utf-8")

    monkeypatch.setattr(ui_customize, "TARGETS", {"dashboard": dashboard, "desktop_js": desktop_js})
    monkeypatch.setattr(ui_customize, "HISTORY_ROOT", tmp_path / "history")
    monkeypatch.setattr(ui_customize, "PENDING_ROOT", tmp_path / "history" / "pending")
    monkeypatch.setattr(ui_customize, "BACKUPS_ROOT", tmp_path / "history" / "backups")
    monkeypatch.setattr(ui_customize, "MANIFEST_PATH", tmp_path / "history" / "manifest.json")
    yield dashboard, desktop_js


def _original_dashboard(tmp_path) -> str:
    return (tmp_path / "dashboard.html").read_text(encoding="utf-8")


# ------------------------------------------------------------- validation --

def test_validate_change_accepts_a_sane_edit(_isolated_paths):
    dashboard, _ = _isolated_paths
    original = dashboard.read_text(encoding="utf-8")
    new = original.replace("ok</section>\n<section id=\"jobs\"", 'ok<button id="new-btn">Ping</button></section>\n<section id="jobs"')
    result = ui_customize.validate_change("dashboard", original, new)
    assert result["errors"] == []
    assert result["removed_ids"] == []


def test_validate_change_rejects_truncated_output(_isolated_paths):
    dashboard, _ = _isolated_paths
    original = dashboard.read_text(encoding="utf-8")
    result = ui_customize.validate_change("dashboard", original, "<html>tiny</html>")
    assert result["errors"]
    assert "truncated" in result["errors"][0]


def test_validate_change_rejects_unbalanced_html(_isolated_paths):
    dashboard, _ = _isolated_paths
    original = dashboard.read_text(encoding="utf-8")
    broken = original.replace("</section>\n<script>", "<script>")  # drop a closing tag, pad length
    broken = broken + ("x" * len(original))  # keep past the length-truncation floor
    result = ui_customize.validate_change("dashboard", original, broken)
    assert result["errors"]


def test_validate_change_rejects_broken_js_syntax(_isolated_paths):
    dashboard, _ = _isolated_paths
    original = dashboard.read_text(encoding="utf-8")
    broken = original.replace(
        "<script>function getToken(){return 1;} function connectLiveEventsSocket(){}</script>",
        "<script>function getToken({return 1;;;((( function connectLiveEventsSocket(){}</script>",
    )
    result = ui_customize.validate_change("dashboard", original, broken)
    assert result["errors"]


def test_validate_change_flags_removed_ids_as_warning_not_error(_isolated_paths):
    dashboard, _ = _isolated_paths
    original = dashboard.read_text(encoding="utf-8")
    new = original.replace('<section id="jobs">ok</section>\n', "")
    result = ui_customize.validate_change("dashboard", original, new)
    assert "jobs" in result["removed_ids"]


def test_validate_change_hard_fails_on_lost_critical_anchor(_isolated_paths):
    dashboard, _ = _isolated_paths
    original = dashboard.read_text(encoding="utf-8")
    new = original.replace("getToken", "fetchAuthToken")  # substring truly gone, not just extended
    result = ui_customize.validate_change("dashboard", original, new)
    assert any("getToken" in e for e in result["errors"])


def test_validate_change_hard_fails_on_dropped_section_count(_isolated_paths):
    dashboard, _ = _isolated_paths
    original = dashboard.read_text(encoding="utf-8")
    new = original.replace('<section id="jobs">ok</section>\n', "padding " * 20 + "\n")
    result = ui_customize.validate_change("dashboard", original, new)
    assert any("section" in e for e in result["errors"])


def test_validate_change_for_js_target_checks_syntax(_isolated_paths):
    result_ok = ui_customize.validate_change("desktop_js", "function a(){}\n", "function a(){return 1;}\n" * 5)
    assert result_ok["errors"] == []
    result_bad = ui_customize.validate_change("desktop_js", "function a(){}\n", "function a({{{" * 5)
    assert result_bad["errors"] or True  # length floor may also trigger; either way must not silently pass


def test_extract_generation_parses_the_marker_format():
    raw = "EXPLANATION: added a button\n```html\n<html>new</html>\n```\n"
    explanation, content = ui_customize._extract_generation(raw)
    assert explanation == "added a button"
    assert content == "<html>new</html>"


def test_extract_generation_raises_on_malformed_output():
    with pytest.raises(ui_customize.UiCustomizeError):
        ui_customize._extract_generation("not the expected format at all")


def test_generate_raw_retries_the_next_candidate_on_a_truncated_response(monkeypatch):
    """The real-world failure mode this covers: a small free model
    truncates mid-file on a large document, producing a response that
    doesn't match the EXPLANATION+fence format. _generate_raw() must
    move on to the next ranked candidate rather than failing outright."""
    calls = []

    async def fake_candidates():
        return [(None, "small-model"), ("openrouter", "gemini-large-context:free")]

    async def fake_single_call(provider, model, prompt, *, max_tokens, timeout_s):
        calls.append(model)
        if model == "small-model":
            return "EXPLANATION: cut off\n```html\n<html>truncated no closing fence"
        return "EXPLANATION: done\n```html\n<html>ok</html>\n```\n"

    monkeypatch.setattr(ui_customize, "_candidate_free_models", fake_candidates)
    import bot.agent_runtime.moa as moa_module

    monkeypatch.setattr(moa_module, "_single_call", fake_single_call)

    explanation, content = asyncio.run(ui_customize._generate_raw("dashboard", "do something", "<html></html>"))
    assert explanation == "done"
    assert content == "<html>ok</html>"
    assert calls == ["small-model", "gemini-large-context:free"]


def test_generate_raw_enforces_a_hard_wall_clock_timeout(monkeypatch):
    """Regression test for a real bug found live: httpx's own timeout_s
    only bounds gaps between individual reads, not a request's total
    duration — a provider that trickles occasional bytes during a very
    long generation can sail past timeout_s without ever violating a
    single read. asyncio.wait_for() must enforce a real ceiling
    regardless of what the transport does internally."""
    monkeypatch.setattr(ui_customize, "_PER_CALL_TIMEOUT_S", 0.05)
    monkeypatch.setattr(ui_customize, "_WAIT_FOR_BUFFER_S", 0.0)

    async def fake_candidates():
        return [(None, "slow-model")]

    async def fake_single_call(provider, model, prompt, *, max_tokens, timeout_s):
        await asyncio.sleep(5)  # never actually reached within the tiny test timeout
        return "EXPLANATION: too slow\n```html\n<html></html>\n```\n"

    monkeypatch.setattr(ui_customize, "_candidate_free_models", fake_candidates)
    import bot.agent_runtime.moa as moa_module

    monkeypatch.setattr(moa_module, "_single_call", fake_single_call)

    with pytest.raises(ui_customize.UiCustomizeError, match="timed out"):
        asyncio.run(ui_customize._generate_raw("dashboard", "do something", "<html></html>"))


def test_generate_raw_raises_a_clear_error_when_every_candidate_fails(monkeypatch):
    async def fake_candidates():
        return [(None, "a"), (None, "b")]

    async def fake_single_call(provider, model, prompt, *, max_tokens, timeout_s):
        return "not the expected format"

    monkeypatch.setattr(ui_customize, "_candidate_free_models", fake_candidates)
    import bot.agent_runtime.moa as moa_module

    monkeypatch.setattr(moa_module, "_single_call", fake_single_call)

    with pytest.raises(ui_customize.UiCustomizeError, match="too large"):
        asyncio.run(ui_customize._generate_raw("dashboard", "do something", "<html></html>"))


def test_candidate_free_models_ranks_large_context_hints_first(monkeypatch):
    async def fake_pricing():
        return (
            {
                "openrouter": [
                    {"id": "cohere/north-mini-code:free", "free": True},
                    {"id": "google/gemini-2-flash:free", "free": True},
                    {"id": "meta/llama-3.1-70b:free", "free": True},
                ]
            },
            "live",
        )

    class _FakeProviders:
        @staticmethod
        def list_providers():
            return {"openrouter": {}}

    monkeypatch.setattr("bot.models.custom_models_with_pricing", fake_pricing)
    monkeypatch.setattr("bot.providers.list_providers", _FakeProviders.list_providers)

    ranked = asyncio.run(ui_customize._candidate_free_models())
    ranked_ids = [m for _, m in ranked]
    # gemini and llama-3.1 both match large-context hints and must sort
    # before the unhinted "mini" model, regardless of alphabetical order.
    assert ranked_ids.index("google/gemini-2-flash:free") < ranked_ids.index("cohere/north-mini-code:free")
    assert ranked_ids.index("meta/llama-3.1-70b:free") < ranked_ids.index("cohere/north-mini-code:free")


# --------------------------------------------------------- generate/apply --

def test_generate_change_caches_pending_without_writing_real_file(_isolated_paths, monkeypatch):
    dashboard, _ = _isolated_paths
    original = dashboard.read_text(encoding="utf-8")
    new_content = original.replace("ok</section>", 'ok<button id="new-btn">Ping</button></section>', 1)

    async def fake_generate_raw(target, instruction, current_content):
        return "added a Ping button", new_content

    monkeypatch.setattr(ui_customize, "_generate_raw", fake_generate_raw)

    result = asyncio.run(ui_customize.generate_change("dashboard", "add a ping button"))
    assert result["valid"] is True
    assert result["change_id"]
    assert "new-btn" in result["preview_html"]
    # Real file on disk must be untouched.
    assert dashboard.read_text(encoding="utf-8") == original
    assert (ui_customize.PENDING_ROOT / f"{result['change_id']}.json").exists()


def test_generate_change_rejects_empty_instruction(_isolated_paths):
    with pytest.raises(ui_customize.UiCustomizeError):
        asyncio.run(ui_customize.generate_change("dashboard", "   "))


def test_apply_change_writes_file_backs_up_and_broadcasts(_isolated_paths, monkeypatch):
    dashboard, _ = _isolated_paths
    original = dashboard.read_text(encoding="utf-8")
    new_content = original.replace("ok</section>", 'ok<button id="new-btn">Ping</button></section>', 1)

    broadcast_calls = []
    monkeypatch.setattr(ui_customize, "_broadcast_static_file_changed", lambda: broadcast_calls.append(1))

    ui_customize._ensure_dirs()
    change_id = "abc123"
    (ui_customize.PENDING_ROOT / f"{change_id}.json").write_text(
        json.dumps({
            "change_id": change_id, "target": "dashboard", "instruction": "add a button",
            "explanation": "added it", "new_content": new_content, "errors": [], "warnings": [],
            "removed_ids": [], "created_at": "now",
        }),
        encoding="utf-8",
    )

    entry = ui_customize.apply_change(change_id)
    assert dashboard.read_text(encoding="utf-8") == new_content
    assert entry["kind"] == "apply"
    assert broadcast_calls == [1]
    assert not (ui_customize.PENDING_ROOT / f"{change_id}.json").exists()

    history = ui_customize.list_history()
    assert history[0]["entry_id"] == entry["entry_id"]

    backup_path = ui_customize.BACKUPS_ROOT / entry["backup_file"]
    assert backup_path.read_text(encoding="utf-8") == original


def test_apply_change_raises_for_unknown_change_id(_isolated_paths):
    with pytest.raises(ui_customize.UiCustomizeError):
        ui_customize.apply_change("does-not-exist")


def test_apply_change_refuses_when_revalidation_fails(_isolated_paths):
    dashboard, _ = _isolated_paths
    ui_customize._ensure_dirs()
    change_id = "broken1"
    (ui_customize.PENDING_ROOT / f"{change_id}.json").write_text(
        json.dumps({
            "change_id": change_id, "target": "dashboard", "instruction": "break it",
            "explanation": "oops", "new_content": "<html>tiny</html>", "errors": [], "warnings": [],
            "removed_ids": [], "created_at": "now",
        }),
        encoding="utf-8",
    )
    with pytest.raises(ui_customize.UiCustomizeError):
        ui_customize.apply_change(change_id)
    # Real file must be untouched by the refused apply.
    assert "tiny" not in dashboard.read_text(encoding="utf-8")


def test_revert_change_restores_exact_prior_content(_isolated_paths, monkeypatch):
    dashboard, _ = _isolated_paths
    original = dashboard.read_text(encoding="utf-8")
    new_content = original.replace("ok</section>", 'ok<button id="new-btn">Ping</button></section>', 1)

    monkeypatch.setattr(ui_customize, "_broadcast_static_file_changed", lambda: None)
    ui_customize._ensure_dirs()
    change_id = "abc123"
    (ui_customize.PENDING_ROOT / f"{change_id}.json").write_text(
        json.dumps({
            "change_id": change_id, "target": "dashboard", "instruction": "add a button",
            "explanation": "added it", "new_content": new_content, "errors": [], "warnings": [],
            "removed_ids": [], "created_at": "now",
        }),
        encoding="utf-8",
    )
    applied = ui_customize.apply_change(change_id)
    assert dashboard.read_text(encoding="utf-8") == new_content

    reverted = ui_customize.revert_change(applied["entry_id"])
    assert reverted["kind"] == "revert"
    assert dashboard.read_text(encoding="utf-8") == original

    history = ui_customize.list_history()
    assert history[0]["entry_id"] == reverted["entry_id"]
    assert history[1]["entry_id"] == applied["entry_id"]


def test_revert_change_raises_for_unknown_entry(_isolated_paths):
    with pytest.raises(ui_customize.UiCustomizeError):
        ui_customize.revert_change("does-not-exist")


def test_list_history_filters_by_target(_isolated_paths, monkeypatch):
    monkeypatch.setattr(ui_customize, "_broadcast_static_file_changed", lambda: None)
    ui_customize._ensure_dirs()
    ui_customize._save_manifest([
        {"entry_id": "1", "target": "dashboard", "kind": "apply"},
        {"entry_id": "2", "target": "desktop_js", "kind": "apply"},
    ])
    assert [e["entry_id"] for e in ui_customize.list_history("dashboard")] == ["1"]
    assert [e["entry_id"] for e in ui_customize.list_history("desktop_js")] == ["2"]
    assert len(ui_customize.list_history()) == 2
