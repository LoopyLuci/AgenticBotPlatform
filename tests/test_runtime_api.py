"""The runtime's HTTP API, state store and branch store."""
from __future__ import annotations

import asyncio

import httpx
from fastapi import FastAPI, Header, HTTPException
from runtime_toys import make_engine

from bot.runtime.api import build_router
from bot.runtime.manifest import BranchStore, InstanceSpec, Manifest
from bot.runtime.state import StateStore


def _auth(token: str = Header(default="")):
    if token != "ok":
        raise HTTPException(status_code=401, detail="no")


def test_api_round_trip_and_auth(tmp_path):
    async def go():
        eng = make_engine(tmp_path)
        app = FastAPI()
        app.include_router(build_router(eng, _auth, _auth))
        h = {"token": "ok"}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
            assert (await c.get("/api/runtime")).status_code == 401
            r = await c.post("/api/runtime/units/alpha/load", json={"version": "1"}, headers=h)
            assert r.status_code == 200 and r.json()["generation"] == 1
            st = (await c.get("/api/runtime", headers=h)).json()
            assert st["generation"] == 1 and st["units"][0]["ref"] == "alpha@default"
            assert (await c.post("/api/runtime/instances", json={"unit": "alpha", "instance": "b"}, headers=h)).status_code == 200
            assert (await c.post("/api/runtime/units/alpha/swap", json={"version": "2"}, headers=h)).status_code == 200
            bad = await c.post("/api/runtime/units/alpha/swap",
                               json={"version": "3", "settings": {"health": "failed"}}, headers=h)
            assert bad.status_code == 409 and bad.json()["detail"]["failed"]
            assert (await c.post("/api/runtime/branches", json={"name": "x", "from": "live"}, headers=h)).status_code == 200
            assert (await c.post("/api/runtime/units/alpha/unload", json={"branch": "x"}, headers=h)).status_code == 200
            plan = (await c.get("/api/runtime/plan?target=x", headers=h)).json()
            assert plan["ops"][0]["action"] == "unload"
            ev = (await c.get("/api/runtime/events?since=1", headers=h)).json()
            assert [e["generation"] for e in ev["events"]] == [2, 3]
            ui = (await c.get("/api/ui/manifest", headers=h)).json()
            assert ui["generation"] == 3 and len(ui["panels"]) == 2
            assert (await c.post("/api/runtime/switch", json={"branch": "x"}, headers=h)).status_code == 200
            assert (await c.post("/api/runtime/switch", json={"branch": "nope"}, headers=h)).status_code == 404
            assert (await c.post("/api/runtime/rollback", headers=h)).status_code == 200
        await eng.settle()
    asyncio.run(go())


def test_state_namespaces_are_durable_and_copy_on_write(tmp_path):
    store = StateStore(tmp_path / "s.db")
    base = store.namespace("u@default")
    base.set("a", {"x": 1})
    base.set("b", 2)
    branch = store.namespace("u@default#t", base=base)
    assert branch.get("a") == {"x": 1}
    branch.set("a", 9)
    branch.delete("b")
    assert (base.get("a"), base.get("b")) == ({"x": 1}, 2)
    assert (branch.get("a"), branch.get("b", "gone")) == (9, "gone")
    assert branch.keys() == ["a"] and base.keys() == ["a", "b"]
    store.close()
    again = StateStore(tmp_path / "s.db").namespace("u@default")
    assert again.get("a") == {"x": 1}


def test_branch_store_round_trip(tmp_path):
    branches = BranchStore(tmp_path / "b")
    m = Manifest({"k": 1}, {"u@default": InstanceSpec("u", "3", {"a": 1})})
    branches.save("live", m)
    assert branches.get("live").id == m.id and branches.names() == ["live"]
    assert not list((tmp_path / "b").glob("*.tmp"))
    assert branches.get("missing") is None


def test_ui_manifest_shape(tmp_path):
    async def go():
        eng = make_engine(tmp_path)
        await eng.load("alpha", version="7")
        panel = eng.ui_manifest()["panels"][0]
        assert panel == {"id": "alpha-panel", "unit": "alpha", "instance": "default", "version": "7",
                         "title": "alpha", "group": "modules", "icon": None, "order": 5, "kind": "builtin",
                         "section": "alpha-panel"}
        await eng.settle()
    asyncio.run(go())
