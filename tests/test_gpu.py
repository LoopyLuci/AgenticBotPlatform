"""bot/gpu.py: the probe is replaced, so these run without a GPU."""
import asyncio
import json

import pytest

from bot import gpu

GIB = 1 << 30


@pytest.fixture
def fake(monkeypatch):
    def set_rows(rows, source="dxgi"):
        monkeypatch.setattr(gpu, "_probe", lambda: (source, rows))
    return set_rows


def _card(total=24, used=4, budget=23):
    return {"name": "Test GPU", "vendor": "amd", "total": total * GIB, "used": used * GIB, "budget": budget * GIB}


def test_status_free_is_budget_minus_usage(fake):
    fake([_card()])
    st = gpu.gpu_status()
    assert st["known"] and st["total_gb"] == 24 and st["used_gb"] == 4 and st["free_gb"] == 19 and st["budget_gb"] == 23


def test_status_picks_largest_adapter_and_never_negative(fake):
    fake([_card(8, 1, 7), _card(24, 30, 23)])
    st = gpu.gpu_status()
    assert st["total_gb"] == 24 and st["free_gb"] == 0 and len(st["adapters"]) == 2


def test_unknown_when_nothing_answers(fake):
    fake([], "none")
    st = gpu.gpu_status()
    assert st["known"] is False and st["free_gb"] is None
    assert gpu.free_vram_gb() is None and gpu.can_fit(1) is None and gpu.total_vram_bytes() == 0


def test_total_only_source_is_not_known(fake):
    fake([{"name": "x", "vendor": "amd", "total": 8 * GIB, "used": None, "budget": None}], "registry")
    st = gpu.gpu_status()
    assert st["known"] is False and st["total_gb"] == 8 and st["free_gb"] is None


def test_can_fit_math(fake):
    fake([_card()])                          # 19 free
    assert gpu.can_fit(18) is True           # 18 + 1 headroom
    assert gpu.can_fit(18.5) is False
    assert gpu.can_fit(18.5, headroom_gb=0.5) is True
    rep = gpu.fit_report(30)
    assert rep["fits"] is False and rep["need_gb"] == 30


def test_probe_failure_is_swallowed(monkeypatch):
    monkeypatch.setattr(gpu, "_probe", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    assert gpu.gpu_status()["known"] is False


def test_tool_shape(fake):
    fake([_card()])
    from bot.agent_runtime import gpu_tool, toolspec
    assert toolspec.spec_for("gpu_status").read_only
    out = json.loads(asyncio.run(gpu_tool._gpu_status({"need_gb": 10})))
    assert out["fits"] is True and out["free_gb"] == 19 and out["name"] == "Test GPU"
    assert "fits" not in json.loads(asyncio.run(gpu_tool._gpu_status({})))


def test_engine_budget_uses_measured_total(fake, monkeypatch):
    from bot.localai import engine
    fake([_card(24, 0, 23)])
    monkeypatch.setattr(engine, "settings", lambda: {"gpu_memory_gb": 0})
    assert engine.gpu_budget() == int(24 * GIB * 0.92)
    monkeypatch.setattr(engine, "settings", lambda: {"gpu_memory_gb": 10})
    assert engine.gpu_budget() == 10 * GIB


def test_cli_can_fit_exit_codes(fake, capsys):
    from abp_cli import __main__ as cli
    fake([_card()])
    assert cli.main(["gpu", "can-fit", "10"]) == 0
    assert cli.main(["gpu", "can-fit", "30"]) == 1
    assert cli.main(["--json", "gpu"]) == 0
    fake([], "none")
    assert cli.main(["gpu", "can-fit", "1"]) == 2


@pytest.mark.skipif(__import__("sys").platform != "win32", reason="DXGI is Windows only")
def test_real_dxgi_smoke():
    st = gpu.gpu_status()                    # whatever box this is, it must not raise and must be self-consistent
    if st["known"]:
        assert 0 <= st["free_gb"] <= st["total_gb"] + 1


def test_api_gpu(fake, monkeypatch, tmp_path):
    from fastapi.testclient import TestClient
    from bot.dashboard.server import build_app
    monkeypatch.setenv("DASHBOARD_TOKEN", "t" * 48)
    fake([_card()])
    c = TestClient(build_app())
    r = c.get("/api/gpu?need_gb=10", headers={"X-Dashboard-Token": "t" * 48})
    assert r.status_code == 200 and r.json()["free_gb"] == 19 and r.json()["fits"] is True
