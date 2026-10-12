"""The work board as read-only kanban cards: columns are task states, kept apart from the hand-made cards, and no route,
command or client call on the way can write to the board."""
from __future__ import annotations

import asyncio
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from bot import coordination, kanban
from bot.coordination import LocalBoard
from bot.dashboard.server import build_app

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def board(tmp_path):
    b = LocalBoard(tmp_path / "k.db")
    coordination.set_board(b)
    yield b
    coordination.set_board(None)
    b.close()


def _seed(board):
    a, _ = board.create_task("abp/swarm-1", "plan it", key="a")
    b, _ = board.create_task("abp/swarm-1", "do it", key="b", deps=[a["id"]])
    c, _ = board.create_task("abp/hermes-3", "dispatch", key="c")
    claim = board.claim(a["id"], "w1", lease_s=60)
    return a, b, c, claim


def _cards(mirror):
    return [t for x in mirror["boards"] for col in x["columns"] for t in col["cards"]]


def test_columns_are_the_task_states_one_board_per_board_name(board):
    _seed(board)
    mirror = kanban.work_board_mirror()
    assert mirror["read_only"] is True and mirror["source"] == "work-board" and mirror["backend"] == "local"
    boards = {x["board"]: {col["name"]: [t["title"] for t in col["cards"]] for col in x["columns"]} for x in mirror["boards"]}
    assert set(boards) == {"abp/swarm-1", "abp/hermes-3"}
    assert list(boards["abp/swarm-1"]) == list(kanban.WORK_BOARD_COLUMNS)
    assert boards["abp/swarm-1"]["claimed"] == ["plan it"] and boards["abp/swarm-1"]["open"] == ["do it"]
    assert boards["abp/hermes-3"]["open"] == ["dispatch"]
    card = next(t for t in _cards(mirror) if t["title"] == "plan it")
    assert card["owner"] == "w1" and card["read_only"] is True and card["source"] == "work-board"
    assert "detail" not in card and "result" not in card
    only = kanban.work_board_mirror("abp/hermes-3")
    assert [x["board"] for x in only["boards"]] == ["abp/hermes-3"]


def test_the_mirror_is_separate_from_hand_made_cards_and_writes_nothing(board, temp_db):
    _seed(board)
    kanban.add_card(1, "default", "todo", "my own card")
    versions = {t["id"]: t["version"] for t in board.list_tasks()}
    mirror = kanban.work_board_mirror()
    assert "my own card" not in [t["title"] for t in _cards(mirror)]
    assert [c["text"] for c in kanban.list_cards(1, "default")] == ["my own card"]      # and the board is not in there
    kanban.format_work_board()
    assert {t["id"]: t["version"] for t in board.list_tasks()} == versions               # nothing changed


def test_finished_work_moves_columns(board):
    a, _b, _c, claim = _seed(board)
    board.set_state(a["id"], "w1", claim["fence"], "done", result={"x": 1})
    mirror = kanban.work_board_mirror("abp/swarm-1")
    cols = {col["name"]: [t["title"] for t in col["cards"]] for col in mirror["boards"][0]["columns"]}
    assert cols["done"] == ["plan it"] and cols["open"] == ["do it"] and cols["claimed"] == []


def test_the_route_serves_the_mirror_and_has_no_write_verbs(board, temp_db, monkeypatch):
    _seed(board)
    monkeypatch.setenv("DASHBOARD_TOKEN", "test-token")
    client = TestClient(build_app())
    h = {"X-Dashboard-Token": "test-token"}
    r = client.get("/api/kanban/work-board", headers=h)
    assert r.status_code == 200 and r.json()["read_only"] is True and len(r.json()["boards"]) == 2
    assert [x["board"] for x in client.get("/api/kanban/work-board?board=abp/hermes-3", headers=h).json()["boards"]] == ["abp/hermes-3"]
    assert client.get("/api/kanban/work-board").status_code in (401, 403)
    for verb in ("post", "put", "delete", "patch"):
        assert getattr(client, verb)("/api/kanban/work-board", headers=h).status_code == 405


def test_the_slash_command_lists_the_work_board(board, temp_db):
    from bot import commands

    _seed(board)
    ctx = commands.CmdContext.__new__(commands.CmdContext)
    ctx.instance_id = 1
    out = asyncio.run(commands.cmd_kanban(ctx, ["workboard"]))
    assert "read-only" in out and "abp/swarm-1" in out and "plan it" in out
    assert "no tasks" in asyncio.run(commands.cmd_kanban(ctx, ["workboard", "abp/nothing"]))


def test_the_panel_is_identical_in_both_uis_registered_and_escapes():
    dash = (ROOT / "bot/dashboard/static/kanban-board-mirror.js").read_bytes()
    assert dash == (ROOT / "desktop-app/ui/kanban-board-mirror.js").read_bytes() and len(dash) > 800
    for page in ("bot/dashboard/static/dashboard.html", "desktop-app/ui/index.html"):
        text = (ROOT / page).read_text(encoding="utf-8")
        assert 'id="kb-wb-root"' in text and re.search(r'<script src="(/static/)?kanban-board-mirror\.js"></script>', text)
    js = dash.decode()
    assert "/api/kanban/work-board" in js and "method" not in js        # only ever reads
    raw = [i for i in re.findall(r"\$\{([^}]*)\}", js) if i.strip().startswith(("t.", "b.", "c.")) and "E(" not in i and "?" not in i]
    assert not raw, raw
