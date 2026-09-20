"""repo_map, code_search and session_search (roadmap P3)."""
from __future__ import annotations

import asyncio
import os
import time

import pytest

from bot import db
from bot.agent_runtime import repo_map, search_index, toolspec, tools
from bot.agent_runtime.errors import ToolError


@pytest.fixture(autouse=True)
def _session():
    token = toolspec.session_var.set("search-test")
    repo_map._cache.clear()
    yield
    toolspec.session_var.reset(token)


@pytest.fixture
def ws(tmp_path):
    root = tmp_path / "ws"
    root.mkdir()
    return root.resolve()


def put(ws, rel, text):
    p = ws / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


def call(ws, name, **inp):
    return plain(asyncio.run(tools.execute_tool(name, inp, workspace=ws)))


def plain(text):
    """Search results mark matched words with >> <<; tests compare the words."""
    return text.replace(">>", "").replace("<<", "")


# ---- repo_map -----------------------------------------------------------------------
PY = '''
MAX_RETRIES = 3

class Client(Base):
    def __init__(self, url):
        pass
    def fetch(self, path, *, timeout=5):
        pass
    async def close(self):
        pass

def helper(a, b, *args, **kw):
    pass
'''


def test_repo_map_lists_python_declarations_with_signatures(ws):
    put(ws, "pkg/client.py", PY)
    out = call(ws, "repo_map")
    assert "pkg/client.py" in out and "class Client(Base)" in out and "def fetch(self, path, timeout)" in out
    assert "async def close(self)" in out and "def helper(a, b, *args, **kw)" in out and "MAX_RETRIES" in out
    assert "__init__" not in out


@pytest.mark.parametrize("name,source,expected", [
    ("a.ts", "export class Store {}\nexport async function load(x) {}\nexport const parse = (s) => s\nexport interface Opts {}\nexport type Id = string\n",
     ["class Store", "function load", "function parse", "interface Opts", "type Id"]),
    ("a.go", "package a\nfunc (s *Server) Handle(w int) {}\nfunc Run() {}\ntype Config struct {\n}\n", ["func Handle", "func Run", "type Config"]),
    ("a.rs", "pub struct Point;\npub async fn go() {}\nimpl Point {\n    pub fn new() {}\n}\npub trait Draw {}\n", ["type Point", "fn go", "fn new", "type Draw"]),
    ("A.java", "public class Widget {\n    public void render() {}\n}\ninterface Shape {}\n", ["class Widget", "class Shape"]),
    ("a.rb", "module Tools\n  class Runner\n    def run!\n    end\n  end\nend\n", ["class Tools", "class Runner", "def run!"]),
])
def test_repo_map_reads_other_languages_with_regexes(ws, name, source, expected):
    put(ws, name, source)
    out = call(ws, "repo_map")
    for item in expected:
        assert item in out, (name, item, out)


def test_the_most_referenced_file_comes_first_and_vendored_folders_are_skipped(ws):
    put(ws, "core.py", "class CoreEngine:\n    pass\n")
    put(ws, "a.py", "from core import CoreEngine\ndef use_a():\n    return CoreEngine()\n")
    put(ws, "b.py", "from core import CoreEngine\ndef use_b():\n    return CoreEngine()\n")
    put(ws, "node_modules/x/lib.py", "def vendored_function():\n    pass\n")
    put(ws, "broken.py", "def (((\n")
    out = call(ws, "repo_map")
    assert out.index("core.py") < out.index("a.py") and "vendored_function" not in out and "broken.py" not in out


def test_the_budget_trims_and_says_so(ws):
    for i in range(60):
        put(ws, f"m{i:02}.py", "".join(f"def function_number_{i}_{j}(argument):\n    pass\n" for j in range(15)))
    out = call(ws, "repo_map", max_tokens=400)
    assert "more not shown" in out and len(out) < 3000
    assert len(call(ws, "repo_map", max_tokens=12000)) > len(out)


def test_a_sub_folder_can_be_mapped_and_outside_paths_are_refused(ws):
    put(ws, "a/one.py", "def in_a():\n    pass\n")
    put(ws, "b/two.py", "def in_b():\n    pass\n")
    out = call(ws, "repo_map", path="a")
    assert "in_a" in out and "in_b" not in out
    with pytest.raises(ToolError, match="outside the working directory"):
        call(ws, "repo_map", path="..")
    with pytest.raises(ToolError, match="not a folder"):
        call(ws, "repo_map", path="a/one.py")


def test_an_empty_project_says_so_and_results_are_cached_until_a_file_changes(ws, monkeypatch):
    assert "No source files" in call(ws, "repo_map")
    put(ws, "a.py", "def first_version():\n    pass\n")
    built = []
    real = repo_map.build
    monkeypatch.setattr(repo_map, "build", lambda root, tokens: built.append(1) or real(root, tokens))
    call(ws, "repo_map")
    call(ws, "repo_map")
    assert len(built) == 1
    time.sleep(0.02)
    put(ws, "a.py", "def second_version():\n    pass\n")
    assert "second_version" in call(ws, "repo_map") and len(built) == 2


# ---- code_search ----------------------------------------------------------------------
def test_code_search_finds_chunks_by_words_and_reports_file_and_line(ws):
    put(ws, "auth/session.py", "def refresh_token(client):\n    # retry with backoff when the token refresh fails\n    return client.renew()\n")
    put(ws, "ui/button.py", "def draw_button(label):\n    return label\n")
    out = call(ws, "code_search", query="token refresh retry")
    assert out.startswith("auth/session.py:1") and "button" not in out


def test_identifiers_with_underscores_and_camel_case_are_found(ws):
    put(ws, "a.py", "def compute_total_price(items):\n    pass\n")
    put(ws, "b.js", "function parseHttpResponse(r) { return r }\n")
    assert "a.py" in call(ws, "code_search", query="compute_total_price")
    assert "b.js" in call(ws, "code_search", query="parseHttpResponse")
    assert "b.js" in call(ws, "code_search", query="http response parse")


def test_it_falls_back_to_any_word_when_no_chunk_has_all_of_them(ws):
    put(ws, "a.py", "def alpha_only():\n    pass\n")
    assert "a.py" in call(ws, "code_search", query="alpha zebra")


def test_the_index_follows_edits_deletions_and_new_files(ws):
    a = put(ws, "a.py", "def old_name():\n    pass\n")
    assert "a.py" in call(ws, "code_search", query="old_name")
    time.sleep(0.02)
    a.write_text("def new_name():\n    pass\n")
    put(ws, "b.py", "def brand_new():\n    pass\n")
    assert "No matches" in call(ws, "code_search", query="old_name")
    assert "a.py" in call(ws, "code_search", query="new_name") and "b.py" in call(ws, "code_search", query="brand_new")
    (ws / "b.py").unlink()
    assert "No matches" in call(ws, "code_search", query="brand_new")


def test_code_search_skips_vendored_binary_and_hidden_files_and_can_be_narrowed(ws):
    put(ws, "node_modules/x.js", "function vendored_thing() {}\n")
    put(ws, ".hidden/y.py", "def hidden_thing():\n    pass\n")
    (ws / "bin.dat").write_bytes(b"\x00\x01 binary_thing")
    put(ws, "src/keep.py", "def shared_word():\n    pass\n")
    put(ws, "docs/notes.md", "The shared_word is described here.\n")
    for word in ("vendored_thing", "hidden_thing", "binary_thing"):
        assert "No matches" in call(ws, "code_search", query=word)
    both = call(ws, "code_search", query="shared_word")
    assert "src/keep.py" in both and "docs/notes.md" in both
    only = call(ws, "code_search", query="shared_word", path="src")
    assert "src/keep.py" in only and "docs/" not in only


def test_code_search_errors(ws):
    with pytest.raises(ToolError, match="query is required"):
        call(ws, "code_search", query="  ")
    with pytest.raises(ToolError, match="no searchable words"):
        call(ws, "code_search", query="!!! ???")
    with pytest.raises(ToolError, match="outside the working directory"):
        call(ws, "code_search", query="x", path="..")
    put(ws, "a.py", "print('quote \"marks\" and (parens) and AND OR NOT')\n")
    assert "a.py" in call(ws, "code_search", query="\"marks\" (parens) AND OR NOT")      # FTS syntax in the question is harmless


def test_two_workspaces_have_separate_indexes(tmp_path):
    a, b = (tmp_path / "a").resolve(), (tmp_path / "b").resolve()
    put(a, "x.py", "def only_in_a():\n    pass\n")
    put(b, "x.py", "def only_in_b():\n    pass\n")
    assert "x.py" in call(a, "code_search", query="only_in_a") and "No matches" in call(b, "code_search", query="only_in_a")


# ---- session_search ----------------------------------------------------------------------
@pytest.fixture
def two_instances(temp_db):
    from bot import bot_instances

    def make(name):
        return bot_instances.create_instance(
            name=name, platform="telegram", backend="api",
            credentials={"bot_token": "123456789:AAExampleTokenFromBotFather1234"}, allowed_user_ids=[1])
    a, b = make("a"), make("b")
    conn = db.get_conn()
    for iid, key in ((a, "sess-a1"), (a, "sess-a2"), (b, "sess-b1")):
        conn.execute("INSERT INTO chat_sessions (instance_id, chat_id, desktop_session_key, created_at, last_used_at) "
                     "VALUES (?, '1', ?, 'now', 'now')", (iid, key))
    conn.commit()
    db.append_agent_message("sess-a1", "user", "Which database should we use for the analytics service?")
    db.append_agent_message("sess-a1", "assistant", [{"type": "text", "text": "Use PostgreSQL for analytics; it handles the reporting queries."},
                                                     {"type": "tool_use", "id": "t", "name": "read_file", "input": {}}])
    db.append_agent_message("sess-a2", "user", "Remind me what we decided about the deployment schedule")
    db.append_agent_message("sess-b1", "user", "The secret plan for instance b involves PostgreSQL too")
    return a, b


def test_session_search_covers_the_instances_sessions_and_never_another_instances(two_instances):
    a, b = two_instances
    out = call_with(a, "sess-a2", query="PostgreSQL analytics")
    assert "PostgreSQL for analytics" in out and "secret plan" not in out
    assert "session" in out and "a1" in out
    only_b = call_with(b, "sess-b1", query="PostgreSQL")
    assert "secret plan" in only_b and "reporting queries" not in only_b


def call_with(iid, session, **inp):
    token = toolspec.session_var.set(session)
    try:
        return plain(asyncio.run(tools.execute_tool("session_search", inp, workspace=tools.WORKSPACES_ROOT, instance_id=iid)))
    finally:
        toolspec.session_var.reset(token)


def test_scope_this_limits_to_the_current_session(two_instances):
    a, _ = two_instances
    assert "No matches" in call_with(a, "sess-a2", query="PostgreSQL", scope="this")
    assert "deployment schedule" in call_with(a, "sess-a2", query="deployment", scope="this")
    with pytest.raises(ToolError, match="scope must be"):
        call_with(a, "sess-a2", query="x", scope="everything")


def test_new_messages_are_found_and_cleared_conversations_disappear(two_instances):
    a, _ = two_instances
    call_with(a, "sess-a2", query="deployment")
    db.append_agent_message("sess-a2", "user", "Also the rollback plan uses blue green switching")
    assert "blue green" in call_with(a, "sess-a2", query="rollback blue green")
    db.clear_agent_messages("sess-a1")
    assert "No matches" in call_with(a, "sess-a2", query="PostgreSQL analytics")


def test_tool_outputs_are_indexed_briefly_and_thinking_is_not(two_instances):
    a, _ = two_instances
    db.append_agent_message("sess-a2", "assistant", [{"type": "thinking", "thinking": "private reasoning zebra", "signature": "s"},
                                                     {"type": "tool_result", "tool_use_id": "t", "content": "result body " + "q" * 5000 + " tailmarker"}])
    assert "No matches" in call_with(a, "sess-a2", query="zebra")
    assert "result body" in call_with(a, "sess-a2", query="result body") and "No matches" in call_with(a, "sess-a2", query="tailmarker")


def test_search_tools_are_read_only_and_parallel_safe():
    for n in ("repo_map", "code_search", "session_search"):
        assert toolspec.is_concurrency_safe(n) and not tools.is_dangerous(n)
