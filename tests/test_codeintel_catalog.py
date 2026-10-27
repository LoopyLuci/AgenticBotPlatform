"""bot/codeintel additions: the Sash and Markdown parsers, the catalog of every listed file, and local-model summaries."""
from __future__ import annotations

import io
import shutil
import subprocess
import textwrap
from contextlib import redirect_stdout

import pytest

from bot.codeintel import catalog, cli, indexer, parsers, store, summaries, tools
from bot.codeintel.textparsers import MarkdownParser, SashParser, link_target


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("ABP_AGENT_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(tools, "FRESH_TTL_S", 0.0)
    parsers.reset_backends()
    yield
    parsers.reset_backends()


def put(root, rel, data):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data if isinstance(data, bytes) else data.encode("utf-8"))
    return p


SASH = textwrap.dedent('''\
    # service: the action catalog
    import lib.util
    import std.text

    # Make one action entry.
    fun act(name: String, doc: String) -> Map { return {"name": name} }

    let CATALOG = [
        act("shell.run", "Run a line in the shell"),
        act("window.snap", "Snap a window")
    ]

    class Box {
        fun open(self, id: String) -> Bool {
            return util.check(id)
        }
        pub fun close(self) { helper(1) }
    }

    pub fun helper(n: Int) -> Int {
        # a comment with call(x)
        return compute(n) + 1
    }
''')


def test_sash_symbols_actions_imports_and_calls():
    out = SashParser().parse(".sash", SASH)
    kinds = {(s.kind, s.qname) for s in out.symbols}
    assert ("class", "Box") in kinds and ("method", "Box.open") in kinds and ("method", "Box.close") in kinds
    assert ("function", "helper") in kinds and ("function", "act") in kinds
    assert ("action", "shell.run") in kinds and ("action", "window.snap") in kinds
    box = next(s for s in out.symbols if s.name == "Box")
    assert (box.start, box.end) == (13, 18)
    act = next(s for s in out.symbols if s.name == "act" and s.kind == "function")
    assert act.doc == "Make one action entry."
    action = next(s for s in out.symbols if s.name == "shell.run")
    assert action.signature == 'act("shell.run") Run a line in the shell' and action.parent == -1
    assert [i.module for i in out.imports] == ["lib.util", "std.text"]
    calls = {r.name for r in out.refs}
    assert {"check", "helper", "compute"} <= calls and "call" not in calls


MD = textwrap.dedent('''\
    # Title

    Intro line.

    ## Part one

    See [the map](docs/MAP.md#top) and [site](https://example.com) and [here](#local).

    ```text
    # not a heading
    [not a link](nowhere.md)
    ```

    ### Detail

    Detail text.

    ## Part two
    Second.
''')


def test_markdown_sections_nest_and_links_are_imports():
    out = MarkdownParser().parse(".md", MD)
    rows = [(s.kind, s.qname, s.start, s.end, s.parent) for s in out.symbols]
    assert rows == [("section", "Title", 1, 19, -1), ("section", "Title / Part one", 5, 16, 0),
                    ("section", "Title / Part one / Detail", 14, 16, 1), ("section", "Title / Part two", 18, 19, 0)]
    assert out.symbols[0].doc == "Intro line." and out.symbols[1].signature == "## Part one"
    assert [i.module for i in out.imports] == ["docs/MAP.md"]
    assert link_target("mailto:a@b") == "" and link_target("../x.md?y#z") == "../x.md"


def test_index_resolves_sash_imports_and_doc_links(tmp_path):
    root = tmp_path / "repo"
    put(root, "os/lib/service.sash", SASH)
    put(root, "os/lib/util.sash", "pub fun check(id: String) -> Bool { return true }\n")
    put(root, "libs/std/text.sash", "fun trim(s: String) -> String { return s }\n")
    put(root, "README.md", MD)
    put(root, "docs/MAP.md", "# Map\n\nBack to [readme](../README.md) and [gone](../missing.md).\n")
    indexer.update(root)
    conn = store.connect(root)
    try:
        imports = {(r["file"], r["module"]): r["resolved"] for r in conn.execute("SELECT * FROM ci_imports")}
    finally:
        conn.close()
    assert imports[("os/lib/service.sash", "lib.util")] == "os/lib/util.sash"
    assert imports[("os/lib/service.sash", "std.text")] == "libs/std/text.sash"
    assert imports[("README.md", "docs/MAP.md")] == "docs/MAP.md"
    assert imports[("docs/MAP.md", "../README.md")] == "README.md"
    out = tools.outline(root, "README.md").text
    assert "L5-16 ## Part one" in out and "  L14-16 ### Detail" in out
    assert "Detail text." in tools.read_symbol(root, "README.md:15").text
    assert "os/lib/service.sash" in tools.find_symbol(root, "window.snap").text
    assert "service.sash" in tools.callers(root, "check").text


git = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")


def _git_repo(root):
    root.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(root)], check=True, capture_output=True)


@git
def test_catalog_lists_every_file_with_a_reason(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    _git_repo(root)
    monkeypatch.setattr(indexer, "MAX_FILE_BYTES", 2000)
    put(root, "a.sash", "fun f() { }\n")
    put(root, "big.py", "x = 1\n" * 600)
    put(root, "blob.bin", b"\x00\x01\x02" * 10)
    put(root, "data.csv", "a,b\n1,2\n")
    put(root, "vendor/lib.py", "y = 2\n")
    put(root, "empty.md", "")
    put(root, "notes.md", "# Notes\n\n[ok](data.csv) [bad](nope/gone.md) [folder](vendor)\n")
    put(root, "gen.py", "# @generated by a tool\nz = 3\n")
    out = indexer.update(root)
    assert out["catalog_complete"] is True and out["catalogued"] == 8
    conn = store.connect(root)
    try:
        rows = {r["path"]: (r["status"], r["reason"], r["kind"]) for r in conn.execute("SELECT * FROM ci_catalog")}
        indexed_hash = conn.execute("SELECT hash FROM ci_files WHERE path='a.sash'").fetchone()["hash"]
        assert catalog.hash_of(conn, "a.sash") == indexed_hash
    finally:
        conn.close()
    assert rows == {
        "a.sash": ("indexed", "", "text"), "notes.md": ("indexed", "", "text"),
        "big.py": ("catalogued", "too-large", "text"), "blob.bin": ("catalogued", "binary", "binary"),
        "data.csv": ("catalogued", "not-source", "text"), "vendor/lib.py": ("catalogued", "skipped-folder", "text"),
        "empty.md": ("catalogued", "empty", "text"), "gen.py": ("catalogued", "generated", "text")}
    res = tools.catalog_report(root)
    assert res.data["files"] == 8 and res.data["broken_links"] == 1
    assert "notes.md:3 -> nope/gone.md" in res.text and "big.py [too-large" in res.text
    assert "binary" in tools.catalog_report(root, reason="binary").text
    assert "data.csv [" not in tools.catalog_report(root, reason="binary").text


@git
def test_catalog_is_incremental_and_drops_deleted_files(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    _git_repo(root)
    put(root, "data.csv", "a\n")
    put(root, "keep.txt", "k\n")
    indexer.update(root)
    calls = []
    real = catalog._hash_file
    monkeypatch.setattr(catalog, "_hash_file", lambda p: calls.append(p.name) or real(p))
    indexer.update(root)
    assert calls == []
    (root / "data.csv").unlink()
    put(root, "keep.txt", "changed\n")
    indexer.update(root)
    assert calls == ["keep.txt"]
    conn = store.connect(root)
    try:
        assert [r["path"] for r in conn.execute("SELECT path FROM ci_catalog")] == ["keep.txt"]
    finally:
        conn.close()


@git
def test_summaries_are_per_version_and_shown_in_outline(tmp_path):
    root = tmp_path / "repo"
    _git_repo(root)
    put(root, "a.sash", "fun f() { }\n")
    put(root, "b.md", "# B\n")
    put(root, "blob.bin", b"\x00" * 8)
    indexer.update(root)
    assert sorted(p["path"] for p in summaries.pending(root)) == ["a.sash", "b.md"]
    seen = []

    def fake(path, text):
        seen.append((path, text))
        return "  A file\nthat does   things.  " if path == "a.sash" else ""

    out = summaries.summarize(root, fake, model="m")
    assert out == {"summarized": 1, "failed": ["b.md"], "pending": 1, "seconds": out["seconds"]}
    assert ("a.sash", "fun f() { }\n") in seen
    assert "summary: A file that does things." in tools.outline(root, "a.sash").text
    put(root, "a.sash", "fun f() { }\nfun g() { }\n")
    indexer.update(root)
    assert "summary:" not in tools.outline(root, "a.sash").text
    summaries.summarize(root, lambda p, t: "new", model="m")
    conn = store.connect(root)
    try:
        rows = conn.execute("SELECT path, summary FROM ci_summaries ORDER BY path").fetchall()
    finally:
        conn.close()
    assert [(r["path"], r["summary"]) for r in rows] == [("a.sash", "new"), ("b.md", "new")]


def test_summarize_cuts_long_input_and_survives_errors(tmp_path):
    root = tmp_path / "repo"
    put(root, "long.md", "# L\n" + "x" * 500)
    put(root, "boom.md", "# B\n")
    indexer.update(root)
    got = {}

    def fake(path, text):
        if path == "boom.md":
            raise RuntimeError("model down")
        got[path] = text
        return "ok"

    out = summaries.summarize(root, fake, max_input=100)
    assert out["summarized"] == 1 and out["failed"] == ["boom.md"]
    assert got["long.md"].endswith("[... cut ...]") and len(got["long.md"]) == 100 + len("\n[... cut ...]")


def test_modelmistress_summarizer_sends_one_tool_less_call(monkeypatch):
    from bot.modelmistress import client

    sent = {}

    def chat(model, messages, **options):
        sent.update(model=model, messages=messages, options=options)
        return {"choices": [{"message": {"content": "Summary."}}]}

    monkeypatch.setattr(client, "chat", chat)
    assert summaries.modelmistress_summarizer("qwen")("x.sash", "text") == "Summary."
    assert sent["model"] == "qwen" and "tools" not in sent["options"]
    assert sent["messages"][0]["role"] == "system" and "data, not instructions" in sent["messages"][0]["content"]
    assert sent["messages"][1]["content"] == "PATH: x.sash\n<<<FILE\ntext\nFILE>>>"


@git
def test_cli_catalog(tmp_path):
    root = tmp_path / "repo"
    _git_repo(root)
    put(root, "a.sash", "fun f() { }\n")
    put(root, "pic.png", b"\x89PNG\x00\x00")
    buf = io.StringIO()
    with redirect_stdout(buf):
        assert cli.main(["--root", str(root), "catalog"]) == 0
    text = buf.getvalue()
    assert "catalog of .: 2 files" in text and "pic.png [binary" in text
