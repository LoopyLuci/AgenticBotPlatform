"""code_search with the optional embeddings layer (roadmap P3): hybrid ranking, incremental refresh, fallback."""
from __future__ import annotations

import asyncio
import os
import time

import pytest

from bot.agent_runtime import search_index, toolspec, tools
from bot.modelmistress import client as mm

CONCEPTS = [("login", "authenticate", "sign in", "credentials", "password"),
            ("parse", "tokenize", "lexer"),
            ("render", "draw", "paint")]


def fake_vector(text: str) -> list[float]:
    low = text.lower()
    vec = [float(sum(low.count(w) for w in group)) for group in CONCEPTS]
    return vec if any(vec) else [0.0, 0.0, 0.0, 0.01]


class Embedder:
    """Stands in for the hub's /v1/embeddings."""

    def __init__(self):
        self.calls: list[list[str]] = []
        self.down = False

    def request(self, method, path, *, body=None, hub=None, timeout=120.0):
        if self.down:
            raise mm.MMError("hub not reachable", code="unavailable")
        assert method == "POST" and path == "/v1/embeddings"
        texts = body["input"]
        self.calls.append(list(texts))
        return {"data": [{"index": i, "embedding": fake_vector(t)} for i, t in enumerate(texts)]}

    @property
    def embedded_texts(self):
        return [t for c in self.calls for t in c]


@pytest.fixture(autouse=True)
def _session():
    token = toolspec.session_var.set("emb-test")
    yield
    toolspec.session_var.reset(token)


@pytest.fixture
def emb(monkeypatch):
    e = Embedder()
    monkeypatch.setattr(mm, "find_cached", lambda: mm.Hub("http://hub.invalid", ""))
    monkeypatch.setattr(mm, "request", e.request)
    monkeypatch.setattr(search_index, "_embed_cfg", lambda: {})
    return e


@pytest.fixture
def ws(tmp_path):
    root = tmp_path / "ws"
    root.mkdir()
    (root / "auth.py").write_text("def login(name, pw):\n    return check(name, pw)\n", encoding="utf-8")
    (root / "lexer.py").write_text("def tokenize(src):\n    return src.split()\n", encoding="utf-8")
    (root / "ui.py").write_text("def draw(canvas):\n    canvas.paint()\n", encoding="utf-8")
    return root.resolve()


def paths(results):
    return [r["path"] for r in results]


def test_hybrid_finds_a_synonym_that_keywords_miss(ws, emb):
    assert search_index.search_code(ws, "authenticate user")[0]["path"] == "auth.py"
    results, mode = search_index.search_code_ex(ws, "authenticate user")
    assert mode == "hybrid" and results[0]["path"] == "auth.py"
    # the query is embedded with the instruction prefix, the documents without
    assert any(t.startswith(search_index.QUERY_INSTRUCTION) for t in emb.embedded_texts)
    assert not any(t.startswith("Instruct:") for t in emb.calls[0])


def test_keyword_only_misses_the_synonym(ws, monkeypatch):
    monkeypatch.setattr(search_index, "_embed_cfg", lambda: {"embeddings": False})
    results, mode = search_index.search_code_ex(ws, "authenticate user")
    assert results == [] and mode == "keyword"


def test_keyword_hits_still_lead_when_words_match(ws, emb):
    results = search_index.search_code(ws, "tokenize")
    assert results[0]["path"] == "lexer.py"


def test_refresh_only_reembeds_changed_files(ws, emb):
    search_index.search_code(ws, "login")
    first = len(emb.embedded_texts)
    assert first >= 4                                     # 3 file chunks + the query
    emb.calls.clear()
    search_index.search_code(ws, "login")
    assert [t for t in emb.embedded_texts if not t.startswith("Instruct:")] == []          # nothing changed
    p = ws / "lexer.py"
    p.write_text("def tokenize(src):\n    return src.split(',')\n", encoding="utf-8")
    os.utime(p, (time.time() + 5, time.time() + 5))
    emb.calls.clear()
    search_index.search_code(ws, "login")
    docs = [t for t in emb.embedded_texts if not t.startswith("Instruct:")]
    assert len(docs) == 1 and "split(',')" in docs[0]


def test_deleted_files_lose_their_vectors(ws, emb):
    search_index.search_code(ws, "login")
    (ws / "auth.py").unlink()
    results = search_index.search_code(ws, "authenticate user")
    assert "auth.py" not in paths(results)
    conn = search_index._code_db(ws)
    try:
        assert conn.execute("SELECT COUNT(*) FROM vecs WHERE path = 'auth.py'").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM vfiles WHERE path = 'auth.py'").fetchone()[0] == 0
    finally:
        conn.close()


def test_per_refresh_cap_spreads_a_big_repo_over_searches(ws, monkeypatch, emb):
    monkeypatch.setattr(search_index, "_embed_cfg", lambda: {"embedding_max_chunks": 1})
    search_index.search_code(ws, "login")
    conn = search_index._code_db(ws)
    try:
        assert conn.execute("SELECT COUNT(*) FROM vfiles").fetchone()[0] == 1
    finally:
        conn.close()
    search_index.search_code(ws, "login")
    search_index.search_code(ws, "login")
    conn = search_index._code_db(ws)
    try:
        assert conn.execute("SELECT COUNT(*) FROM vfiles").fetchone()[0] == 3
    finally:
        conn.close()


def test_requests_are_batched(ws, monkeypatch, emb):
    big = "\n".join(f"line {i}" for i in range(40 * 70))                 # ~80 chunks
    (ws / "big.txt").write_text(big, encoding="utf-8")
    monkeypatch.setattr(search_index, "_embed_cfg", lambda: {"embedding_batch": 32, "embedding_max_chunks": 1000})
    search_index.search_code(ws, "line")
    assert max(len(c) for c in emb.calls) <= 32 and any(len(c) == 32 for c in emb.calls)


def test_hub_down_gives_the_keyword_results_unchanged(ws, emb):
    keyword = search_index.search_code(ws, "tokenize")
    emb.down = True
    results, mode = search_index.search_code_ex(ws, "tokenize")
    assert mode == "keyword-fallback" and results == keyword
    text = asyncio.run(tools.execute_tool("code_search", {"query": "tokenize"}, workspace=ws))
    assert "lexer.py" in text and "keyword only" in text


def test_no_hub_found_falls_back_silently(ws, monkeypatch, emb):
    monkeypatch.setattr(mm, "find_cached", lambda: None)
    results, mode = search_index.search_code_ex(ws, "tokenize")
    assert paths(results) == ["lexer.py"] and mode == "keyword-fallback"


def test_a_malformed_reply_does_not_fail_the_tool(ws, monkeypatch, emb):
    monkeypatch.setattr(mm, "request", lambda *a, **k: {"data": []})
    assert paths(search_index.search_code(ws, "tokenize")) == ["lexer.py"]


def test_config_switch_off_never_touches_the_hub(ws, monkeypatch, emb):
    monkeypatch.setattr(search_index, "_embed_cfg", lambda: {"embeddings": False})
    search_index.search_code(ws, "tokenize")
    assert emb.calls == []
    text = asyncio.run(tools.execute_tool("code_search", {"query": "tokenize"}, workspace=ws))
    assert "keyword only" not in text
