"""bot/codeintel: the index, the parsers (tree-sitter, Python ast, regex), the navigation tools, and their surfaces
(agent tools, MCP, HTTP API, CLI)."""
from __future__ import annotations

import asyncio
import io
import json
import shutil
import subprocess
import sys
import textwrap
import threading
import time
from contextlib import redirect_stdout

import pytest
from fastapi.testclient import TestClient

from bot.codeintel import bench, cli, indexer, parsers, tools
from bot.codeintel.treesitter import TreeSitterParser

TS = pytest.mark.skipif(not TreeSitterParser.available(), reason="tree-sitter is not installed")


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("ABP_AGENT_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(tools, "FRESH_TTL_S", 0.0)
    parsers.reset_backends()
    yield
    parsers.reset_backends()


PY_SAMPLE = textwrap.dedent('''\
    """Module doc."""
    import os
    from pkg.helpers import helper, other as alias

    LIMIT = 3


    def plain(a, b=2):
        """Add things.

        More text."""
        return helper(a) + b


    @decorator
    @other(1)
    async def fetch(url: str, *, retries: int = 3) -> str:
        def inner():
            return plain(1)
        return inner()


    class Outer(Base):
        """An outer class."""
        LEVEL = 1

        class Inner:
            def deep(self):
                return 1

        def method(self, x):
            return self.helper2(x)

        @staticmethod
        def static():
            pass
''')


def put(root, rel, text):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(text.encode("utf-8"))
    return p


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    put(root, "pkg/__init__.py", "")
    put(root, "pkg/helpers.py", textwrap.dedent('''\
        def helper(x):
            """Help with x."""
            return x * 2

        def lonely():
            return 0
    '''))
    put(root, "pkg/core.py", textwrap.dedent('''\
        from pkg.helpers import helper

        class Engine:
            def run(self, n):
                return helper(n)

        def start():
            return Engine().run(3)
    '''))
    put(root, "pkg/other.py", textwrap.dedent('''\
        def helper(y):
            return y

        def use():
            return helper(1)
    '''))
    put(root, "tests/test_core.py", textwrap.dedent('''\
        from pkg.core import Engine, start

        def test_engine_runs():
            assert Engine().run(2) == 4

        def test_start():
            assert start() == 6
    '''))
    put(root, "tests/test_helpers.py", textwrap.dedent('''\
        from pkg import helpers

        def test_helper_doubles():
            assert helpers.helper(2) == 4
    '''))
    return root.resolve()


def spans(parsed):
    return {(s.qname, s.kind, s.start, s.end) for s in parsed.symbols}


# ------------------------------------------------------------------ parsers
EXPECTED_PY = {
    ("LIMIT", "variable", 5, 5),
    ("plain", "function", 8, 12),
    ("fetch", "function", 15, 20),          # starts at the first decorator
    ("fetch.inner", "function", 18, 19),
    ("Outer", "class", 23, 36),
    ("Outer.LEVEL", "variable", 25, 25),
    ("Outer.Inner", "class", 27, 29),
    ("Outer.Inner.deep", "method", 28, 29),
    ("Outer.method", "method", 31, 32),
    ("Outer.static", "method", 34, 36),     # decorator line included
}


@pytest.mark.parametrize("backend", ["python-ast", pytest.param("tree-sitter", marks=TS)])
def test_python_symbol_ranges_are_exact(backend):
    parsed = parsers.parse_source(".py", PY_SAMPLE, backend=backend)
    assert parsed is not None and parsed.parser == backend
    assert spans(parsed) == EXPECTED_PY
    by = {s.qname: s for s in parsed.symbols}
    assert by["plain"].doc == "Add things."
    assert by["Outer"].doc == "An outer class."
    assert "async def fetch" in by["fetch"].signature and "retries: int" in by["fetch"].signature and "-> str" in by["fetch"].signature
    assert by["fetch"].decorators.startswith("@decorator")
    assert parsed.symbols[by["Outer.Inner.deep"].parent].qname == "Outer.Inner"
    calls = {(r.name, r.kind) for r in parsed.refs}
    assert ("helper", "call") in calls and ("plain", "call") in calls and ("helper2", "call") in calls
    imports = {(i.module, i.name) for i in parsed.imports}
    assert ("os", "") in imports and ("pkg.helpers", "helper") in imports and ("pkg.helpers", "other") in imports


@TS
def test_tree_sitter_is_the_primary_backend_and_python_ast_the_fallback():
    assert parsers.parse_source(".py", "def f():\n    pass\n").parser == "tree-sitter"
    assert parsers.parse_source(".py", "def f():\n    pass\n", backend="python-ast").parser == "python-ast"


def test_without_tree_sitter_python_uses_ast_and_the_rest_use_regex(monkeypatch):
    monkeypatch.setattr(parsers, "_BACKENDS", [parsers.PythonAstParser(), parsers.RegexParser()])
    assert parsers.parse_source(".py", "def f():\n    pass\n").parser == "python-ast"
    parsed = parsers.parse_source(".js", "function foo(a){\n  return bar(a)\n}\nclass K {\n  m(){ foo(1) }\n}\n")
    assert parsed.parser == "regex"
    assert {(s.qname, s.start, s.end) for s in parsed.symbols} >= {("foo", 1, 3), ("K", 4, 6)}
    assert any(r.name == "bar" and r.kind == "call" for r in parsed.refs)


@TS
def test_typescript_symbols():
    src = textwrap.dedent('''\
        import { a } from './a';
        /** Adds. */
        export const add = async (x: number): Promise<number> => {
          return helper(x);
        };
        export default class Shape extends Base {
          area(): number { return this.w.mul(2); }
        }
        interface Named { name(): string }
        type Id = string;
        enum Color { Red }
        export function top() { return 1 }
    ''')
    parsed = parsers.parse_source(".ts", src, backend="tree-sitter")
    got = {(s.qname, s.kind, s.start, s.end) for s in parsed.symbols}
    assert ("add", "function", 3, 5) in got
    assert ("Shape", "class", 6, 8) in got and ("Shape.area", "method", 7, 7) in got
    assert ("Named", "interface", 9, 9) in got and ("Id", "type", 10, 10) in got and ("Color", "enum", 11, 11) in got
    assert ("top", "function", 12, 12) in got
    assert next(s for s in parsed.symbols if s.qname == "add").doc == "Adds."
    assert ("./a", "") in {(i.module, i.name) for i in parsed.imports}
    assert {"helper", "mul"} <= {r.name for r in parsed.refs if r.kind == "call"}


@TS
def test_javascript_symbols_and_require():
    src = "const q = require('./q');\nfunction foo(a) {\n  return bar(a);\n}\nclass K {\n  m() { foo(1); }\n}\n"
    parsed = parsers.parse_source(".js", src, backend="tree-sitter")
    got = {(s.qname, s.kind, s.start, s.end) for s in parsed.symbols}
    assert got == {("foo", "function", 2, 4), ("K", "class", 5, 7), ("K.m", "method", 6, 6)}
    assert ("./q", "") in {(i.module, i.name) for i in parsed.imports}
    assert next(r for r in parsed.refs if r.name == "bar").sym == 0


@TS
def test_rust_symbols_with_impl_and_trait():
    src = textwrap.dedent('''\
        use std::fmt;
        /// A point.
        pub struct Point { x: i32 }
        impl Point {
            pub fn new(x: i32) -> Self { Point { x } }
            fn go(&self) -> i32 { helper(self.x) }
        }
        pub trait Shape { fn area(&self) -> f64; }
        const MAX: i32 = 3;
        pub enum Kind { A, B }
    ''')
    parsed = parsers.parse_source(".rs", src, backend="tree-sitter")
    got = {(s.qname, s.kind, s.start, s.end) for s in parsed.symbols}
    assert ("Point", "struct", 3, 3) in got and ("Point", "impl", 4, 7) in got
    assert ("Point.new", "method", 5, 5) in got and ("Point.go", "method", 6, 6) in got
    assert ("Shape", "trait", 8, 8) in got and ("Shape.area", "method", 8, 8) in got
    assert ("MAX", "variable", 9, 9) in got and ("Kind", "enum", 10, 10) in got
    assert next(s for s in parsed.symbols if s.qname == "Point" and s.kind == "struct").doc == "A point."
    assert ("std::fmt", "") in {(i.module, i.name) for i in parsed.imports}
    assert any(r.name == "helper" and r.kind == "call" and parsed.symbols[r.sym].qname == "Point.go" for r in parsed.refs)


@TS
@pytest.mark.parametrize("suffix,src,expect", [
    (".go", "package m\n\ntype S struct{ a int }\n\nfunc (s *S) Run(x int) int {\n\treturn helper(x)\n}\n\nfunc helper(x int) int { return x }\n",
     {("S", "struct", 3, 3), ("S.Run", "method", 5, 7), ("helper", "function", 9, 9)}),
    (".java", "class A {\n  void m(int x) { foo(x); }\n}\ninterface I { void z(); }\n",
     {("A", "class", 1, 3), ("A.m", "method", 2, 2), ("I", "interface", 4, 4), ("I.z", "method", 4, 4)}),
    (".rb", "module M\n  class A\n    def m(x)\n      foo(x)\n    end\n  end\nend\n",
     {("M", "module", 1, 7), ("M.A", "class", 2, 6), ("M.A.m", "method", 3, 5)}),
])
def test_other_languages_on_tree_sitter(suffix, src, expect):
    parsed = parsers.parse_source(suffix, src, backend="tree-sitter")
    assert parsed.parser == "tree-sitter"
    assert {(s.qname, s.kind, s.start, s.end) for s in parsed.symbols} >= expect


@pytest.mark.parametrize("suffix,src,names", [
    (".rs", "pub struct A { x: i32 }\nimpl A {\n    pub fn go(&self) -> i32 {\n        foo(1)\n    }\n}\n", {"A", "A.go"}),
    (".go", "package m\nfunc helper(x int) int {\n\treturn x\n}\n", {"helper"}),
    (".php", "<?php\nclass A {\n  public function m($x) {\n    foo($x);\n  }\n}\n", {"A", "A.m"}),
])
def test_regex_languages_smoke(suffix, src, names):
    parsed = parsers.parse_source(suffix, src, backend="regex")
    assert parsed.parser == "regex"
    assert names <= {s.qname for s in parsed.symbols}
    assert all(s.end >= s.start for s in parsed.symbols)


def test_a_file_that_does_not_parse_is_still_indexed_without_symbols(repo):
    put(repo, "pkg/broken.py", "def (:\n")
    indexer.update(repo)                       # must not raise
    assert tools.outline(repo, "pkg/core.py").text


# ------------------------------------------------------------------ index
def test_index_build_incremental_update_and_removal(repo):
    first = indexer.update(repo)
    assert first["indexed"] == 5 and first["unchanged"] == 0 and first["symbols"] >= 10   # the empty __init__.py is skipped
    again = indexer.update(repo)
    assert again["indexed"] == 0 and again["unchanged"] == first["files"]
    put(repo, "pkg/helpers.py", "def helper(x):\n    return x\n\ndef extra():\n    pass\n")
    changed = indexer.update(repo)
    assert changed["indexed"] == 1
    assert "extra" in tools.find_symbol(repo, "extra").text
    (repo / "pkg" / "other.py").unlink()
    assert indexer.update(repo)["removed"] == 1
    assert "pkg/other.py" not in tools.find_symbol(repo, "use").text


def test_touching_a_file_without_changing_it_does_not_reparse(repo):
    indexer.update(repo)
    p = repo / "pkg" / "helpers.py"
    p.write_bytes(p.read_bytes())
    again = indexer.update(repo)
    assert again["indexed"] == 0


def test_gitignored_vendored_and_generated_files_are_not_indexed(repo):
    put(repo, ".gitignore", "secret_*.py\nbuild_out/\n")
    put(repo, "secret_keys.py", "def hidden():\n    pass\n")
    put(repo, "build_out/gen.py", "def built():\n    pass\n")
    put(repo, "vendor/lib.py", "def vendored():\n    pass\n")
    put(repo, "node_modules/x/index.js", "function nm() {}\n")
    put(repo, "pkg/gen_pb2.py", "def pb():\n    pass\n")
    put(repo, "pkg/auto.py", "# Code generated by a tool. DO NOT EDIT.\ndef auto():\n    pass\n")
    indexer.update(repo)
    for name in ("hidden", "built", "vendored", "nm", "pb", "auto"):
        assert "no symbol matches" in tools.find_symbol(repo, name).text, name


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not available")
def test_in_a_git_work_tree_the_listing_comes_from_git(repo):
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    put(repo, ".gitignore", "ignored_mod.py\n")
    put(repo, "ignored_mod.py", "def zebra_hidden():\n    pass\n")
    put(repo, "fresh_untracked.py", "def walrus_visible():\n    pass\n")
    assert indexer._git_files(repo) is not None
    indexer.update(repo)
    assert "no symbol matches" in tools.find_symbol(repo, "zebra_hidden").text
    assert "walrus_visible" in tools.find_symbol(repo, "walrus_visible").text


def test_the_index_lives_in_the_agent_state_folder_and_shares_code_searchs_file(repo, tmp_path):
    from bot.agent_runtime import search_index

    indexer.update(repo, chunks=True)
    db = search_index.code_db_path(repo)
    assert str(db).startswith(str(tmp_path / "state"))
    import sqlite3

    c = sqlite3.connect(db)
    tables = {r[0] for r in c.execute("select name from sqlite_master")}
    c.close()
    assert {"files", "chunks", "ci_files", "ci_symbols", "ci_refs", "ci_imports", "ci_testmap"} <= tables
    hits = search_index.search_code_ex(repo, "helper doubles", 3)[0]
    assert hits                                    # code_search still works on the shared file


def test_concurrent_use_from_several_threads(repo):
    errors = []

    def work(i):
        try:
            for _ in range(3):
                indexer.update(repo)
                assert "helper" in tools.find_symbol(repo, "helper").text
                tools.outline(repo, "pkg/core.py")
                tools.callers(repo, "helper")
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=work, args=(i,)) for i in range(4)]
    [t.start() for t in threads]
    [t.join(60) for t in threads]
    assert not errors


# ------------------------------------------------------------------ tools
def test_outline_lists_signatures_and_ranges_without_bodies(repo):
    out = tools.outline(repo, "pkg/core.py")
    assert "L3-5 class Engine" in out.text and "  L4-5 def run(self, n)" in out.text and "L7-8 def start()" in out.text
    assert "return helper" not in out.text
    assert out.tokens == tools.est_tokens(out.text)
    folder = tools.outline(repo, "pkg")
    assert "pkg/core.py" in folder.text and "Engine" in folder.text


def test_outline_hides_nested_helper_functions_but_shows_nested_classes(repo):
    put(repo, "pkg/nest.py", PY_SAMPLE)
    text = tools.outline(repo, "pkg/nest.py").text
    assert "fetch" in text and "inner" not in text.split("fetch")[1].split("class Outer")[0]
    assert "class Inner" in text and "def deep" in text


def test_outline_enforces_its_budget(repo):
    put(repo, "pkg/big.py", "\n".join(f"def function_number_{i}(alpha, beta):\n    return alpha\n" for i in range(300)))
    out = tools.outline(repo, "pkg/big.py", max_tokens=300)
    assert out.truncated and out.tokens <= 300 + 40
    assert "more line(s) not shown" in out.text


def test_outline_refuses_a_path_outside_the_repo_and_unknown_files(repo):
    with pytest.raises(tools.CodeIntelError):
        tools.outline(repo, "../elsewhere.py")
    with pytest.raises(tools.CodeIntelError):
        tools.outline(repo, "pkg/missing.py")


def test_find_symbol_exact_qualified_prefix_and_words(repo):
    assert "pkg/core.py:3-5 class pkg.core.Engine" in tools.find_symbol(repo, "Engine").text
    assert "pkg.core.Engine.run" in tools.find_symbol(repo, "Engine.run").text.split("\n")[1]
    assert "pkg.core.Engine.run" in tools.find_symbol(repo, "pkg.core.Engine.run").text
    assert "start" in tools.find_symbol(repo, "sta").text            # prefix
    assert "helper" in tools.find_symbol(repo, "help with x").text    # words from the docstring
    only = tools.find_symbol(repo, "helper", kind="class")
    assert "no symbol matches" in only.text
    assert "no symbol matches" in tools.find_symbol(repo, "zzzqqq").text


def test_read_symbol_by_qualified_name_and_by_path_line(repo):
    by_name = tools.read_symbol(repo, "pkg.core.Engine.run")
    assert by_name.text.splitlines()[0] == "pkg/core.py:4-5 method pkg.core.Engine.run"
    assert by_name.text.splitlines()[1:] == ["    def run(self, n):", "        return helper(n)"]
    by_line = tools.read_symbol(repo, "pkg/core.py:5")
    assert by_line.text.splitlines()[0].startswith("pkg/core.py:4-5 method")                 # innermost symbol
    assert tools.read_symbol(repo, "pkg/core.py::Engine").text.splitlines()[0].startswith("pkg/core.py:3-5 class")
    ctx = tools.read_symbol(repo, "start", context_lines=1)
    assert "class Engine" not in ctx.text and "return Engine().run(3)" in ctx.text
    outside = tools.read_symbol(repo, "pkg/core.py:6")
    assert "outside any symbol" in outside.text


def test_read_symbol_says_when_a_name_is_ambiguous_and_when_it_is_unknown(repo):
    amb = tools.read_symbol(repo, "helper")
    assert amb.text.startswith("ambiguous: 2 symbols") and "pkg/helpers.py:1-3" in amb.text and "pkg/other.py:1-2" in amb.text
    assert "return x * 2" in tools.read_symbol(repo, "pkg.helpers.helper").text
    with pytest.raises(tools.CodeIntelError, match="no symbol"):
        tools.read_symbol(repo, "nothing_like_this")


def test_read_symbol_sees_an_edit_made_a_moment_ago(repo):
    tools.read_symbol(repo, "pkg.helpers.helper")
    put(repo, "pkg/helpers.py", "# moved down\n\n\ndef helper(x):\n    return x * 3\n")
    out = tools.read_symbol(repo, "pkg.helpers.helper")
    assert out.text.splitlines()[0].startswith("pkg/helpers.py:4-5") and "x * 3" in out.text


def test_read_symbol_over_budget_shows_members_for_a_class_and_a_cut_for_a_function(repo):
    body = "\n".join(f"    value_{i} = {i}" for i in range(400))
    put(repo, "pkg/long.py", f"def long_one():\n{body}\n\n\nclass Wide:\n    def a(self):\n{body}\n    def b(self):\n        pass\n")
    cut = tools.read_symbol(repo, "long_one", max_tokens=300)
    assert "more line(s) of this symbol" in cut.text and cut.tokens < 420
    members = tools.read_symbol(repo, "Wide", max_tokens=300)
    assert "members instead" in members.text and "def a(self)" in members.text and "def b(self)" in members.text


def test_references_and_callers_prefer_import_linked_uses_and_report_ambiguity(repo):
    refs = tools.references(repo, "helper")
    assert "AMBIGUOUS - 2 definitions" in refs.text
    assert "pkg/core.py:5 in Engine.run" in refs.text            # imports pkg.helpers
    assert "pkg/other.py:5 in use" in refs.text                  # same file as the second definition
    assert refs.data["ambiguous"] is True
    one = tools.references(repo, "pkg.helpers.helper")
    assert "defined at pkg/helpers.py:1-3" in one.text and "AMBIGUOUS" not in one.text
    callers = tools.callers(repo, "helper")
    assert "AMBIGUOUS" in callers.text and "pkg/core.py:5 Engine.run" in callers.text
    uniq = tools.callers(repo, "Engine")
    assert "pkg/core.py:8 start" in uniq.text and "tests/test_core.py" in uniq.text
    assert "AMBIGUOUS" not in uniq.text
    unknown = tools.callers(repo, "never_defined_anywhere")
    assert "no definition in the index" in unknown.text


def test_related_tests_by_import_by_naming_and_for_a_symbol(repo):
    out = tools.related_tests(repo, "pkg/core.py")
    assert "tests/test_core.py" in out.text and "run: pytest tests/test_core.py" in out.text
    assert "test_engine_runs" in out.text                       # symbol-level reference inside the test
    named = tools.related_tests(repo, "pkg/helpers.py")
    assert "tests/test_helpers.py" in named.text and "import" in named.text and "name" in named.text
    # a test file that imports nothing resolvable is still found by its name
    put(repo, "tests/test_other.py", "def test_nothing():\n    assert True\n")
    assert "tests/test_other.py" in tools.related_tests(repo, "pkg/other.py").text
    sym = tools.related_tests(repo, "pkg.core.start")
    assert "tests/test_core.py" in sym.text and "test_start" in sym.text
    assert "is itself a test file" in tools.related_tests(repo, "tests/test_core.py").text


def test_related_tests_use_coverage_contexts_when_present(repo):
    put(repo, "coverage.json", json.dumps({"files": {"pkg/other.py": {"contexts": {"2": ["tests/test_core.py::test_start|run"]}}}}))
    out = tools.related_tests(repo, "pkg/other.py")
    assert "tests/test_core.py" in out.text and "coverage" in out.text and "test_start" in out.text


DIFF = textwrap.dedent('''\
    diff --git a/pkg/helpers.py b/pkg/helpers.py
    index 111..222 100644
    --- a/pkg/helpers.py
    +++ b/pkg/helpers.py
    @@ -2,1 +2,1 @@ def helper(x):
    -    """Help."""
    +    """Help with x."""
''')


def test_impact_from_a_small_diff_and_from_paths(repo):
    out = tools.impact(repo, DIFF)
    assert "pkg/helpers.py:1-3 function pkg.helpers.helper" in out.text
    assert "lonely" not in out.text.split("callers:")[0]
    assert "Engine.run" in out.text and "imported by" in out.text and "pkg/core.py" in out.text
    assert "tests/test_helpers.py" in out.text and "run: pytest" in out.text
    paths = tools.impact(repo, "pkg/core.py")
    assert "pkg.core.Engine" in paths.text and "tests/test_core.py" in paths.text
    with pytest.raises(tools.CodeIntelError):
        tools.impact(repo, "")


def test_parse_changes_reads_hunks_and_path_lists():
    assert tools.parse_changes(DIFF) == {"pkg/helpers.py": [(2, 2)]}
    assert tools.parse_changes("a.py\nb/c.py, d.py") == {"a.py": [], "b/c.py": [], "d.py": []}


def test_context_pack_respects_its_budget_and_ranks_the_relevant_code(repo):
    for budget in (400, 900, 3000):
        out = tools.context_pack(repo, "the engine run method calls helper to double a number", budget)
        assert out.tokens <= budget, (budget, out.tokens)
    out = tools.context_pack(repo, "the engine run method calls helper to double a number", 3000)
    assert "## signatures" in out.text and "Engine" in out.text and "## code" in out.text
    assert "return helper(n)" in out.text                       # a key body
    assert "## related tests" in out.text and "tests/test_core.py" in out.text
    with pytest.raises(tools.CodeIntelError):
        tools.context_pack(repo, "   ", 1000)


def test_context_pack_adds_rules_from_a_context_vault_when_one_exists(repo, monkeypatch):
    import types

    class _Vault:                         # the context vault's recall(query, project, kinds, k, budget_tokens)
        def recall(self, query, project, kinds, k, budget):
            assert "rule" in kinds and "decision" in kinds
            return [{"kind": "rule", "text": "never call helper twice"}]

    vault = types.ModuleType("bot.context_vault")
    vault.get_vault = lambda: _Vault()
    import bot

    monkeypatch.setitem(sys.modules, "bot.context_vault", vault)
    monkeypatch.setattr(bot, "context_vault", vault, raising=False)   # `from bot import x` reads the attribute first
    out = tools.context_pack(repo, "change the helper function", 2000)
    assert "## rules and decisions" in out.text and "never call helper twice" in out.text
    vault.get_vault = lambda: None                                     # vault off: nothing to add
    assert "## rules and decisions" not in tools.context_pack(repo, "change the helper function", 2000).text


def test_per_tool_token_counters(repo):
    tools.outline(repo, "pkg/core.py")
    tools.outline(repo, "pkg/core.py")
    tools.find_symbol(repo, "Engine")
    s = tools.stats(repo)
    assert s["usage"]["outline"]["calls"] == 2 and s["usage"]["find_symbol"]["calls"] == 1
    assert s["usage"]["outline"]["tokens"] > 0
    assert s["ci_files"] == 5 and s["parsers"]


# ------------------------------------------------------------------ surfaces
def _call(ws, name, **inp):
    from bot.agent_runtime import tools as agent_tools

    return asyncio.run(agent_tools.execute_tool(name, inp, workspace=ws))


def test_the_native_agent_has_the_tools_and_each_answer_says_what_it_cost(repo):
    from bot.agent_runtime import toolspec, tools as agent_tools

    names = {s["name"] for s in agent_tools.TOOL_SCHEMAS} | set(toolspec.registered_names())
    for n in ("outline", "find_symbol", "read_symbol", "references", "callers", "related_tests", "impact", "context_pack"):
        assert n in names
        assert toolspec.registered_dangerous(n, {}) is False
    out = _call(repo, "read_symbol", target="pkg.core.start")
    assert "def start():" in out and out.rstrip().endswith("tokens]")
    assert "[~" in _call(repo, "outline", path="pkg/core.py")
    with pytest.raises(Exception, match="no symbol"):
        _call(repo, "read_symbol", target="nope_nope")


def test_the_tools_never_block_the_event_loop(repo, monkeypatch):
    def slow_update(root, **kw):
        time.sleep(0.6)
        return {"files": 0}

    monkeypatch.setattr(indexer, "update", slow_update)
    monkeypatch.setattr(tools.indexer, "update", slow_update)

    async def scenario():
        ticks = 0
        stop = False

        async def ticker():
            nonlocal ticks
            while not stop:
                await asyncio.sleep(0.01)
                ticks += 1

        t = asyncio.create_task(ticker())
        from bot.agent_runtime import tools as agent_tools

        try:
            await agent_tools.execute_tool("outline", {"path": "pkg"}, workspace=repo)
        finally:
            stop = True
            await t
        return ticks

    assert asyncio.run(scenario()) >= 20             # the loop kept running while the index worked


def test_read_file_points_at_the_cheaper_tools_for_a_big_source_file(repo):
    put(repo, "pkg/big.py", "\n".join(f"def f{i}(a):\n    return a + {i}\n" for i in range(900)))
    out = _call(repo, "read_file", path="pkg/big.py")
    assert "outline(path)" in out and "read_symbol" in out
    put(repo, "pkg/small.py", "def tiny():\n    return 1\n")
    assert _call(repo, "read_file", path="pkg/small.py") == "def tiny():\n    return 1\n"
    put(repo, "notes.txt", "x" * 30000)
    assert "outline(path)" not in _call(repo, "read_file", path="notes.txt")


def test_the_agent_prompt_prefers_the_navigation_tools():
    from bot.agent_runtime import prompt

    assert "outline" in prompt.GUIDANCE and "read_symbol" in prompt.GUIDANCE and "context_pack" in prompt.GUIDANCE


def test_the_mcp_server_lists_the_code_tools():
    from bot import mcp_server

    listed = {t.name for t in mcp_server.mcp._tool_manager.list_tools()}
    assert {"code_outline", "code_find_symbol", "code_read_symbol", "code_references", "code_callers", "code_related_tests",
            "code_impact", "code_context_pack"} <= listed


def test_the_mcp_tools_proxy_to_the_api_with_the_working_folder_as_default(monkeypatch):
    from bot import mcp_server

    seen = {}

    async def fake(method, path, timeout=15.0, **kwargs):
        seen.update(method=method, path=path, kwargs=kwargs)
        return {"text": "ok"}

    monkeypatch.setattr(mcp_server, "_request", fake)
    asyncio.run(mcp_server.code_read_symbol("a.b"))
    assert seen["path"] == "/api/codeintel/read" and seen["kwargs"]["params"]["target"] == "a.b"
    assert seen["kwargs"]["params"]["root"]                    # the process's working folder
    asyncio.run(mcp_server.code_impact("x.py", root="/some/where"))
    assert seen["method"] == "POST" and seen["kwargs"]["json"] == {"changes": "x.py", "root": "/some/where"}


def test_the_http_api_is_read_only_token_protected_and_limited_to_allowed_roots(repo, monkeypatch):
    from bot.codeintel import roots
    from bot.config import config
    from bot.dashboard.server import build_app

    monkeypatch.setenv("DASHBOARD_TOKEN", "test-token")
    monkeypatch.setitem(config._data, "native_agent", {"codeintel": {"roots": [str(repo)]}})
    client = TestClient(build_app())
    h = {"X-Dashboard-Token": "test-token"}
    assert client.get("/api/codeintel/stats").status_code == 401
    r = client.get("/api/codeintel/read", params={"target": "pkg.core.start", "root": str(repo)}, headers=h)
    assert r.status_code == 200 and "def start" in r.json()["text"] and r.json()["tokens"] > 0
    assert client.get("/api/codeintel/outline", params={"path": "pkg/core.py", "root": str(repo)}, headers=h).json()["tokens"] > 0
    assert "pkg/core.py" in client.get("/api/codeintel/find", params={"q": "Engine", "root": str(repo)}, headers=h).json()["text"]
    assert client.get("/api/codeintel/refs", params={"symbol": "Engine", "root": str(repo)}, headers=h).status_code == 200
    assert client.get("/api/codeintel/callers", params={"symbol": "Engine", "root": str(repo)}, headers=h).status_code == 200
    assert client.get("/api/codeintel/tests", params={"target": "pkg/core.py", "root": str(repo)}, headers=h).status_code == 200
    assert client.post("/api/codeintel/impact", json={"changes": "pkg/core.py", "root": str(repo)}, headers=h).status_code == 200
    assert client.post("/api/codeintel/pack", json={"task": "engine run", "root": str(repo), "budget_tokens": 800}, headers=h).status_code == 200
    assert client.get("/api/codeintel/read", params={"target": "nope", "root": str(repo)}, headers=h).status_code == 400
    assert client.post("/api/codeintel/index", json={"root": str(repo)}, headers=h).json()["files"] == 5
    assert client.get("/api/codeintel/stats", params={"root": str(repo)}, headers=h).json()["ci_symbols"] > 0
    other = repo.parent / "not_allowed"
    other.mkdir()
    assert client.get("/api/codeintel/outline", params={"path": ".", "root": str(other)}, headers=h).status_code == 403
    with pytest.raises(roots.RootNotAllowed):
        roots.resolve_root(str(other))
    # nothing here writes or changes the repository; the only POSTs refresh the index or carry long inputs
    paths = [p for p in client.app.openapi()["paths"] if p.startswith("/api/codeintel")]
    assert all(m in ("get", "post") for p in paths for m in client.app.openapi()["paths"][p])


def _run_cli(*argv):
    buf = io.StringIO()
    with redirect_stdout(buf):
        code = cli.main(list(argv))
    return code, buf.getvalue()


def test_the_cli_commands(repo):
    r = str(repo)
    code, out = _run_cli("--root", r, "index")
    assert code == 0 and "files" in out and "symbols" in out
    for argv, needle in (
        (["outline", "pkg/core.py"], "class Engine"), (["find", "Engine"], "pkg/core.py:3-5"),
        (["read", "pkg.core.start"], "def start"), (["refs", "Engine"], "Engine"), (["callers", "Engine"], "start"),
        (["tests", "pkg/core.py"], "tests/test_core.py"), (["impact", "pkg/core.py"], "pkg.core.Engine"),
        (["pack", "engine", "run", "helper", "--budget", "800"], "## signatures"), (["stats"], "ci_symbols")):
        code, out = _run_cli("--root", r, *argv)
        assert code == 0 and needle in out, (argv, out)
    code, out = _run_cli("--root", r, "--json", "find", "Engine")
    assert json.loads(out)["tokens"] > 0
    assert _run_cli("--root", r, "read", "no_such_thing")[0] == 2


def test_the_bench_compares_grep_plus_read_file_with_the_tools(repo):
    q = bench.Question("What calls helper?", r"\bhelper\b", lambda r: [], lambda r: [tools.callers(r, "pkg.helpers.helper")])
    report = bench.run(repo, [q])
    row = report["rows"][0]
    assert row["baseline_tokens"] > 0 and row["tool_tokens"] > 0 and report["total_ratio"] == row["ratio"]
    assert "TOTAL" in bench.render(report)


def test_repo_map_and_code_search_still_work_on_the_same_workspace(repo):
    from bot.agent_runtime import repo_map

    assert "pkg/core.py" in repo_map.build(repo)
    from bot.agent_runtime import search_index

    indexer.update(repo, chunks=True)
    assert search_index.search_code_ex(repo, "Engine run helper", 3)[0]
