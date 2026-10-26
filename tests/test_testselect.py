"""bot/conductor/testselect.py: the tests a change selects, and every case where it must fall back to the whole suite."""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pytest

from bot.conductor import testselect as ts


def write(root: Path, rel: str, text: str = "") -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    write(r, "bot/__init__.py")
    write(r, "bot/core.py", "X = 1\n")
    write(r, "bot/mid.py", "from bot import core\n")
    write(r, "bot/pkg/__init__.py")
    write(r, "bot/pkg/leaf.py", "from ..mid import core\n")              # relative import
    write(r, "bot/other.py", "Y = 2\n")
    write(r, "bot/dyn.py", "import importlib\nM = importlib.import_module('bot.other')\n")
    write(r, "scripts/tool.py", "Z = 3\n")
    write(r, "tests/test_core.py", "from bot.core import X\n")
    write(r, "tests/test_leaf.py", "from bot.pkg import leaf\n")
    write(r, "tests/test_other.py", "import bot.other\n")
    write(r, "tests/test_dyn.py", "import bot.dyn\n")
    write(r, "tests/test_tool.py", "import importlib.util\nSPEC = 'scripts/tool.py'\n")
    write(r, "tests/test_ui.py", "PAGE = 'desktop-app/src/page.js'\n")
    write(r, "tests/test_unrelated.py", "def test_x():\n    pass\n")
    write(r, "tests/test_more1.py")
    write(r, "tests/test_more2.py")
    write(r, "tests/test_more3.py")
    return r


def test_a_one_module_change_selects_its_importers_transitively(repo):
    sel = ts.select(repo, ["bot/core.py"], coverage=False)
    assert not sel.full
    assert sel.tests == ["tests/test_core.py", "tests/test_leaf.py"]
    assert sel.why["tests/test_core.py"] == "imports bot/core.py"
    assert sel.total_tests == 10


def test_a_leaf_change_selects_only_its_tests(repo):
    sel = ts.select(repo, ["bot/pkg/leaf.py"], coverage=False)
    assert sel.tests == ["tests/test_leaf.py"]


def test_a_changed_test_selects_itself(repo):
    sel = ts.select(repo, ["tests/test_unrelated.py"], coverage=False)
    assert sel.tests == ["tests/test_unrelated.py"] and sel.why["tests/test_unrelated.py"] == "changed"


def test_string_module_names_and_script_paths_count_as_imports(repo):
    assert ts.select(repo, ["bot/other.py"], coverage=False).tests == ["tests/test_dyn.py", "tests/test_other.py"]
    assert ts.select(repo, ["scripts/tool.py"], coverage=False).tests == ["tests/test_tool.py"]


def test_a_non_python_file_selects_the_tests_naming_it(repo):
    sel = ts.select(repo, ["desktop-app/src/page.js"], coverage=False)
    assert sel.tests == ["tests/test_ui.py"] and "named in tests/test_ui.py" in sel.why["tests/test_ui.py"]


def test_docs_and_other_gate_sources_select_nothing(repo):
    sel = ts.select(repo, ["docs/guide.md", "desktop-app/src-tauri/src/main.rs"], coverage=False)
    assert not sel.full and sel.tests == []


def test_depth_limit_and_hubs_stop_the_walk(tmp_path):
    r = tmp_path / "r"
    write(r, "bot/a.py")
    write(r, "bot/b.py", "import bot.a\n")
    write(r, "bot/c.py", "import bot.b\n")
    write(r, "bot/d.py", "import bot.c\n")
    write(r, "tests/test_far.py", "import bot.d\n")                       # 4 imports from bot/a.py
    write(r, "tests/test_near.py", "import bot.c\n")                      # 3 imports
    hub = "".join(f"import bot.m{i}\n" for i in range(25)) + "import bot.a\n"
    for i in range(25):
        write(r, f"bot/m{i}.py")
    write(r, "bot/hub.py", hub)
    write(r, "tests/test_via_hub.py", "import bot.hub\n")
    for i in range(6):
        write(r, f"tests/test_pad{i}.py")
    sel = ts.select(r, ["bot/a.py"], coverage=False)
    assert sel.tests == ["tests/test_near.py"]
    assert ts.select(r, ["bot/hub.py"], coverage=False).tests == ["tests/test_via_hub.py"]   # a changed hub still selects


@pytest.mark.parametrize("path", ["tests/conftest.py", "bot/conftest.py", "pytest.ini", "pyproject.toml", "requirements.txt",
                                  "requirements-dev.txt", "tests/fixtures/x/y.py", "tests/data/inv.json", "config/app.yaml"])
def test_test_infrastructure_and_config_force_the_full_suite(repo, path):
    sel = ts.select(repo, ["bot/core.py", path], coverage=False)
    assert sel.full and sel.tests == [] and path in sel.reasons[0]


@pytest.mark.parametrize("path, why", [("bot/schema.yaml", "does not understand"), ("examples/demo.py", "outside the indexed"),
                                       ("Dockerfile", "root-level")])
def test_unknown_files_no_test_names_force_the_full_suite(repo, path, why):
    sel = ts.select(repo, [path], coverage=False)
    assert sel.full and why in sel.reasons[0]


def test_an_unknown_change_set_forces_the_full_suite(repo):
    sel = ts.select(repo, None)
    assert sel.full and sel.pytest_args() == []


def test_a_selection_that_is_most_of_the_suite_runs_everything(repo):
    write(repo, "bot/core.py", "X = 1\n")
    for name in ("test_unrelated", "test_more1", "test_more2", "test_more3", "test_other", "test_ui", "test_tool"):
        write(repo, f"tests/{name}.py", "import bot.core\n")
    sel = ts.select(repo, ["bot/core.py"], coverage=False)
    assert sel.full and "most of the suite" in sel.reasons[0]


def test_force_full_is_reported(repo):
    sel = ts.select(repo, ["bot/core.py"], force_full="asked for")
    assert sel.full and sel.reasons == ["asked for"]


def test_coverage_contexts_add_tests_but_never_remove(repo):
    db = repo / ".coverage"
    conn = sqlite3.connect(db)
    conn.executescript("CREATE TABLE file (id INTEGER PRIMARY KEY, path TEXT);"
                       "CREATE TABLE context (id INTEGER PRIMARY KEY, context TEXT);"
                       "CREATE TABLE line_bits (file_id INTEGER, context_id INTEGER, numbits BLOB);")
    conn.execute("INSERT INTO file VALUES (1, ?)", (str((repo / "bot/other.py").resolve()),))
    conn.execute("INSERT INTO context VALUES (1, 'tests/test_unrelated.py::test_x|run')")
    conn.execute("INSERT INTO context VALUES (2, '')")
    conn.execute("INSERT INTO line_bits VALUES (1, 1, x'01')")
    conn.execute("INSERT INTO line_bits VALUES (1, 2, x'01')")
    conn.commit()
    conn.close()
    sel = ts.select(repo, ["bot/other.py"])
    assert sel.tests == ["tests/test_dyn.py", "tests/test_other.py", "tests/test_unrelated.py"]
    assert sel.why["tests/test_unrelated.py"] == "covers bot/other.py (coverage)"


def test_a_broken_coverage_file_is_ignored(repo):
    (repo / ".coverage").write_bytes(b"not a database")
    assert ts.select(repo, ["bot/pkg/leaf.py"]).tests == ["tests/test_leaf.py"]


def test_schedule_forces_a_full_run_and_resets_only_on_a_pass(repo, tmp_path):
    state_file = tmp_path / "state.json"
    st = ts.ScheduleState.load(state_file)
    assert ts.select(repo, ["bot/core.py"], schedule=st, every_runs=2, every_hours=24).full     # nothing on record yet
    st.record(state_file, full=True, passed=False)
    assert ts.ScheduleState.load(state_file).last_full == 0                                      # a failed full run doesn't count
    st.record(state_file, full=True, passed=True)
    st = ts.ScheduleState.load(state_file)
    assert not ts.select(repo, ["bot/core.py"], schedule=st, every_runs=2, every_hours=24).full
    st.record(state_file, full=False, passed=True)
    st.record(state_file, full=False, passed=True)
    sel = ts.select(repo, ["bot/core.py"], schedule=ts.ScheduleState.load(state_file), every_runs=2, every_hours=24)
    assert sel.full and "2 selected runs" in sel.reasons[0]
    old = ts.ScheduleState(last_full=time.time() - 30 * 3600, selected_since=0)
    assert "h ago" in old.due(every_runs=0, every_hours=24)
    assert ts.ScheduleState.load(tmp_path / "missing.json").last_full == 0


def test_worker_counts_are_capped(monkeypatch):
    monkeypatch.delenv("ABP_PYTEST_MAX_WORKERS", raising=False)
    assert ts.worker_cap(32) == 8 and ts.worker_cap(8) == 4 and ts.worker_cap(1) == 1
    assert ts.worker_count(None, cpus=32) == 8
    assert ts.worker_count(2, cpus=32) == 0                      # a tiny selection runs serially
    assert ts.worker_count(9, cpus=32) == 3
    monkeypatch.setenv("ABP_PYTEST_MAX_WORKERS", "0")
    assert ts.worker_count(None, cpus=32) == 0
    monkeypatch.setenv("ABP_PYTEST_MAX_WORKERS", "2")
    assert ts.worker_count(100, cpus=32) == 2


def test_report_names_each_test_and_its_cause(repo):
    sel = ts.select(repo, ["bot/core.py"], coverage=False)
    lines = sel.report_lines()
    assert lines[0] == "2 of 10 test files selected for 1 changed file(s)"
    assert "  tests/test_core.py  <- imports bot/core.py" in lines
    assert ts.Selection(True, ["x"]).summary() == "full suite: x"


def test_the_real_repo_selects_a_small_subset_for_a_one_module_change():
    root = Path(__file__).resolve().parents[1]
    sel = ts.select(root, ["bot/conductor/shards.py"], coverage=False)
    assert not sel.full and 0 < len(sel.tests) < sel.total_tests / 10
    assert "tests/test_conductor.py" in sel.tests or any("shards" in t or "conductor" in t for t in sel.tests)
