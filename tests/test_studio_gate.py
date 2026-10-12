"""bot/studio/gate.py: a variant is applied to a temporary copy first and the targeted checks run there in a sandbox
cell; only a passing variant reaches the live files. The UI trees are a temporary copy ($ABP_STUDIO_CODE_ROOT) and the
tests the gate runs are tiny ones written here ($ABP_STUDIO_GATE_TESTS), so nothing touches the real UI."""
from __future__ import annotations

import hashlib
import shutil
import time
from pathlib import Path

import pytest

from bot.studio import gate, variants
from bot.studio.variants import StudioError

ROOT = Path(__file__).resolve().parent.parent
PANEL_A = "bot/dashboard/static/vision-panel.js"
PANEL_B = "desktop-app/ui/vision-panel.js"

MARKER_TEST = '''from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent


def test_no_marker():
    assert "BROKEN" not in (ROOT / "bot/dashboard/static/vision-panel.js").read_text(encoding="utf-8")


def test_other_file_is_the_workspace_copy():
    assert (ROOT / "desktop-app/ui/vision-panel.js").is_file()
'''
SLOW_TEST = "import time\n\n\ndef test_slow():\n    time.sleep(60)\n"


def _tree_hash(root: Path) -> dict:
    return {str(p.relative_to(root)): hashlib.sha1(p.read_bytes()).hexdigest() for p in sorted(root.rglob("*"))
            if p.is_file()}


@pytest.fixture
def env(tmp_path, monkeypatch):
    code = tmp_path / "code"
    for root in variants.UI_ROOTS:
        shutil.copytree(ROOT / root, code / root, ignore=shutil.ignore_patterns("node_modules"))
    tests = tmp_path / "gate-tests"
    tests.mkdir()
    (tests / "test_vision_panel.py").write_text(MARKER_TEST, encoding="utf-8")
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setenv("ABP_STUDIO_CODE_ROOT", str(code))
    monkeypatch.setenv("ABP_STUDIO_DIR", str(tmp_path / "studio"))
    monkeypatch.setenv("ABP_STUDIO_GATE_TESTS", str(tests))
    monkeypatch.setattr("tempfile.tempdir", str(scratch))
    cfg = {"enabled": True, "timeout_s": 120.0, "memory_mb": 0, "always": [], "map": {}}
    monkeypatch.setattr(gate, "settings", lambda: dict(cfg))
    return {"code": code, "tests": tests, "scratch": scratch, "cfg": cfg}


def _variant(env, rel, edit):
    v = variants.create("t")["id"]
    variants.write(v, rel, edit((env["code"] / rel).read_text(encoding="utf-8")))
    return v


def test_select_tests_maps_changed_paths_to_test_files(env):
    (env["tests"] / "test_usage_panel.py").write_text("", encoding="utf-8")
    (env["tests"] / "test_csp.py").write_text("", encoding="utf-8")
    cfg = {"always": ["tests/test_csp.py"], "map": {"desktop-app/ui/*.css": ["tests/test_usage_panel.py"]}}
    assert gate.select_tests([PANEL_A, PANEL_B], cfg) == ["tests/test_vision_panel.py", "tests/test_csp.py"]
    assert gate.select_tests(["desktop-app/ui/app.css"], cfg) == ["tests/test_usage_panel.py", "tests/test_csp.py"]
    assert gate.select_tests(["desktop-app/ui/nothing.js"], {"always": [], "map": {}}) == []


def test_a_good_variant_passes_the_gate_and_is_applied_to_both_uis(env):
    v = _variant(env, PANEL_A, lambda t: t + "\n// a harmless note\n")
    res = gate.apply_checked(v)
    assert res["applied"] == v and res["gate"]["passed"] is True
    assert sorted(res["files"]) == [PANEL_A, PANEL_B]
    assert "a harmless note" in (env["code"] / PANEL_A).read_text(encoding="utf-8")
    assert (env["code"] / PANEL_A).read_bytes() == (env["code"] / PANEL_B).read_bytes()
    kept = variants.meta(v)["gate"]
    assert kept["passed"] and kept["tests"] == ["tests/test_vision_panel.py"]
    assert {c["name"] for c in kept["checks"]} >= {"validators", "parity", f"node:{PANEL_A}", f"node:{PANEL_B}",
                                                   "pytest"}


def test_a_failing_targeted_test_stops_the_apply_and_leaves_live_files_untouched(env):
    before = _tree_hash(env["code"])
    v = _variant(env, PANEL_A, lambda t: t + "\n// BROKEN\n")
    with pytest.raises(StudioError) as e:
        gate.apply_checked(v)
    assert "pytest:tests/test_vision_panel.py::test_no_marker" in str(e.value)
    assert "BROKEN" in str(e.value)          # a short tail of the output is returned
    assert _tree_hash(env["code"]) == before
    m = variants.meta(v)
    assert m["state"] == "open" and m["gate"]["passed"] is False
    assert m["gate"]["failed"] == ["pytest:tests/test_vision_panel.py::test_no_marker"]


def test_broken_javascript_is_stopped(env):
    before = _tree_hash(env["code"])
    v = _variant(env, PANEL_A, lambda t: t + "\nfunction (((\n")
    with pytest.raises(StudioError):
        gate.apply_checked(v)
    assert _tree_hash(env["code"]) == before


def test_node_check_runs_on_the_workspace_copy_when_the_validators_are_bypassed(env, monkeypatch):
    if not shutil.which("node"):
        pytest.skip("node is not installed")
    monkeypatch.setattr(gate, "_validators", lambda vid: [])
    v = _variant(env, PANEL_A, lambda t: t + "\nlet = = 1;\n")
    res = gate.run(v)
    assert not res["passed"] and f"node:{PANEL_A}" in res["failed"]
    assert "SyntaxError" in next(c for c in res["checks"] if c["name"] == f"node:{PANEL_A}")["tail"]


def test_a_variant_that_breaks_parity_is_stopped(env):
    v = variants.create("t")["id"]
    variants.write(v, PANEL_A, (env["code"] / PANEL_A).read_text(encoding="utf-8") + "\n// one\n")
    variants.write(v, PANEL_B, (env["code"] / PANEL_B).read_text(encoding="utf-8") + "\n// two\n")
    before = _tree_hash(env["code"])
    res = gate.run(v)
    assert "parity" in res["failed"] and not res["passed"]
    with pytest.raises(StudioError):
        gate.apply_checked(v)
    assert _tree_hash(env["code"]) == before


def test_the_timeout_stops_a_gate_that_runs_too_long(env):
    (env["tests"] / "test_vision_panel.py").write_text(SLOW_TEST, encoding="utf-8")
    env["cfg"]["timeout_s"] = 3.0
    v = _variant(env, PANEL_A, lambda t: t + "\n// ok\n")
    before = _tree_hash(env["code"])
    t0 = time.monotonic()
    with pytest.raises(StudioError):
        gate.apply_checked(v)
    assert time.monotonic() - t0 < 40
    assert "pytest" in variants.meta(v)["gate"]["failed"]
    assert "timed out" in next(c for c in variants.meta(v)["gate"]["checks"] if c["name"] == "pytest")["tail"]
    assert _tree_hash(env["code"]) == before
    assert list(env["scratch"].iterdir()) == []


def test_the_gate_writes_only_inside_its_temp_dir_and_cleans_it_up(env, monkeypatch):
    seen = {}
    real = gate._build_workspace

    def spy(vid, ws, tests, troot):
        seen["ws"] = ws
        return real(vid, ws, tests, troot)

    monkeypatch.setattr(gate, "_build_workspace", spy)
    v = _variant(env, PANEL_A, lambda t: t + "\n// ok\n")
    code_before, tests_before = _tree_hash(env["code"]), _tree_hash(env["tests"])
    variants_before = _tree_hash(variants._dir(v) / "files")
    assert gate.run(v)["passed"]
    assert seen["ws"].parent == env["scratch"] and not seen["ws"].exists()
    assert list(env["scratch"].iterdir()) == []
    assert _tree_hash(env["code"]) == code_before and _tree_hash(env["tests"]) == tests_before
    assert _tree_hash(variants._dir(v) / "files") == variants_before


def test_the_workspace_is_removed_after_a_failure_too(env):
    v = _variant(env, PANEL_A, lambda t: t + "\n// BROKEN\n")
    assert not gate.run(v)["passed"]
    assert list(env["scratch"].iterdir()) == []


def test_switching_the_gate_off_skips_it(env):
    env["cfg"]["enabled"] = False
    v = _variant(env, PANEL_A, lambda t: t + "\n// BROKEN\n")     # the synthetic test would have stopped it
    res = gate.apply_checked(v)
    assert res["gate"]["skipped"] is True and res["applied"] == v
    assert list(env["scratch"].iterdir()) == []


def test_output_tails_hide_secrets_and_the_workspace_path(tmp_path):
    ws = tmp_path / "ws"
    out = gate.tail(f"File {ws}/x.js\nAPI_KEY=abc123 and token: s3cret\n{'a1' * 30}\n", ws)
    assert "<ws>/x.js" in out and "abc123" not in out and "s3cret" not in out and "a1a1a1a1" not in out


def test_settings_default_to_on_with_a_timeout():
    s = gate.settings()
    assert s["enabled"] is True and s["timeout_s"] >= 1


def test_the_gate_route_and_apply_route(env, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from bot.dashboard import studio_api
    app = FastAPI()
    studio_api.register(app, lambda: None)
    c = TestClient(app)
    bad = _variant(env, PANEL_A, lambda t: t + "\n// BROKEN\n")
    r = c.post(f"/api/studio/variants/{bad}/gate")
    assert r.status_code == 200 and r.json()["passed"] is False
    r = c.post(f"/api/studio/variants/{bad}/apply")
    assert r.status_code == 400 and "sandbox gate failed" in r.json()["detail"]
    good = _variant(env, PANEL_A, lambda t: t + "\n// fine\n")
    r = c.post(f"/api/studio/variants/{good}/apply")
    assert r.status_code == 200 and r.json()["gate"]["passed"] is True


def test_the_environment_switch_turns_the_gate_off(monkeypatch):
    monkeypatch.setenv("ABP_STUDIO_GATE", "off")
    assert gate.settings()["enabled"] is False
