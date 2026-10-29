"""The hardware guard (bot/hwguard): the WHEA parser on recorded events, the governor's decisions, the heavy-work slot, the
settings check, the OS-counter parsing, the API and the panel's two copies."""
from __future__ import annotations

import datetime
import os
import time
from pathlib import Path

import pytest

from bot.hwguard import governor, sensors, slot, whea

ROOT = Path(__file__).resolve().parents[1]

# Recorded from a real machine's System log (wevtutil qe ... /f:xml), the computer name replaced and the RawData blob cut.
_EV = ("<Event xmlns='http://schemas.microsoft.com/win/2004/08/events/event'><System><Provider Name='Microsoft-Windows-WHEA-Logger' "
       "Guid='{{c26c4f3c-3f66-4e99-8f8a-39405cfed220}}'/><EventID>{eid}</EventID><Version>0</Version><Level>{lvl}</Level><Task>0</Task>"
       "<Opcode>0</Opcode><Keywords>0x8000000000000000</Keywords><TimeCreated SystemTime='{ts}'/><EventRecordID>{rid}</EventRecordID>"
       "<Correlation ActivityID='{{0478ba95-3e8f-4d25-b499-3adaaf4577d9}}'/><Execution ProcessID='6312' ThreadID='7152'/><Channel>System"
       "</Channel><Computer>host</Computer><Security UserID='S-1-5-19'/></System><EventData><Data Name='ErrorSource'>3</Data>"
       "<Data Name='ApicId'>{apic}</Data><Data Name='MCABank'>5</Data><Data Name='MciStat'>0xbaa0000000010150</Data>"
       "<Data Name='ErrorType'>{et}</Data><Data Name='TransactionType'>0</Data><Data Name='Length'>936</Data>"
       "<Data Name='RawData'>435045521002FFFFFFFF0300</Data></EventData></Event>")
SAMPLE = "\r\n".join([
    _EV.format(eid=18, lvl=2, ts="2026-10-25T18:33:31.8424432Z", rid=10510499, apic=21, et=9),
    _EV.format(eid=19, lvl=3, ts="2026-10-01T21:14:34.1537049Z", rid=10480754, apic=1, et=9),
    _EV.format(eid=18, lvl=2, ts="2026-10-01T21:14:34.0000000Z", rid=10480753, apic=0, et=10),
])
T_FATAL = datetime.datetime(2026, 10, 25, 18, 33, 31, 842443, tzinfo=datetime.timezone.utc).timestamp()


@pytest.fixture
def guard_on(monkeypatch, tmp_path):
    monkeypatch.delenv("ABP_HWGUARD", raising=False)
    monkeypatch.setenv("ABP_HWGUARD_DIR", str(tmp_path))
    return tmp_path


def _cfg(**kw):
    c = {**governor.DEFAULTS, "govern": dict(governor.DEFAULTS["govern"]), "caps": dict(governor.DEFAULTS["caps"])}
    c.update(kw)
    return c


def _snap(**kw):
    s = {"cpu": {"load_pct": 20.0, "temp_c": None, "cores": 24}, "gpu": {"vram_pct": 10.0, "temp_c": None},
         "memory": {"available_gb": 30.0, "commit": {"pct": 40.0}}, "disks": [{"mount": "E:\\", "free_gb": 500.0}]}
    for k, v in kw.items():
        s[k] = {**s[k], **v} if isinstance(v, dict) else v
    return s


# ---- WHEA --------------------------------------------------------------------------------------------------------------
def test_whea_parse_recorded_events():
    evs = whea.parse(SAMPLE)
    assert [e["record_id"] for e in evs] == [10510499, 10480754, 10480753]       # newest first
    top = evs[0]
    assert top["event_id"] == 18 and top["severity"] == "fatal" and top["kind"] == "fatal machine check"
    assert top["error_type"] == "Cache Hierarchy Error" and top["source"] == "Machine Check Exception"
    assert top["apic_id"] == "21" and top["bank"] == "5" and abs(top["ts"] - T_FATAL) < 1
    assert top["time"].startswith("2026-10-25T18:33:31")
    assert evs[1]["severity"] == "corrected" and evs[2]["error_type"] == "Bus/Interconnect Error"
    assert "RawData" not in top and "host" not in str(top)


def test_whea_parse_ignores_junk_and_other_providers():
    other = SAMPLE.replace("Microsoft-Windows-WHEA-Logger", "Some-Other", 1)
    assert len(whea.parse(other)) == 2
    assert whea.parse("") == [] and whea.parse("<Event>broken") == []


def test_whea_summary_new_and_recent():
    evs = whea.parse(SAMPLE)
    s = whea.summarize(evs, ack_ts=T_FATAL - 10, now=T_FATAL + 86400, caution_days=7)
    assert [e["record_id"] for e in s["new"]] == [10510499] and s["recent_fatal"] == 1 and s["recent_corrected"] == 0
    s = whea.summarize(evs, ack_ts=T_FATAL + 1, now=T_FATAL + 30 * 86400, caution_days=7)
    assert s["new"] == [] and s["recent_fatal"] == 0 and s["count"] == 3


# ---- governor decisions ------------------------------------------------------------------------------------------------
def test_decide_ok_gives_configured_caps():
    d = governor.decide(_snap(), {"new": [], "recent_fatal": 0}, _cfg())
    assert d["level"] == "ok" and d["heavy_allowed"] and d["reasons"] == []
    assert d["caps"] == {"pytest": 4, "factory": 2, "render": 1, "model_load": 1, "build": 2}


def test_decide_pytest_cap_follows_cores():
    assert governor.decide(_snap(cpu={"cores": 4}), None, _cfg())["caps"]["pytest"] == 2


@pytest.mark.parametrize("snap,word", [
    (_snap(cpu={"temp_c": 83.0}), "CPU temperature"), (_snap(gpu={"temp_c": 88.0}), "GPU temperature"),
    (_snap(cpu={"load_pct": 97.0}), "CPU load"), (_snap(memory={"available_gb": 5.0}), "free RAM"),
    (_snap(memory={"commit": {"pct": 90.0}}), "memory commit"), (_snap(gpu={"vram_pct": 95.0}), "VRAM"),
])
def test_decide_caution_halves_caps(snap, word):
    d = governor.decide(snap, None, _cfg())
    assert d["level"] == "caution" and d["heavy_allowed"] and word in d["reasons"][0]
    assert d["caps"] == {"pytest": 2, "factory": 1, "render": 1, "model_load": 1, "build": 1}


@pytest.mark.parametrize("snap,word", [
    (_snap(cpu={"temp_c": 92.0}), "CPU temperature"), (_snap(gpu={"temp_c": 96.0}), "GPU temperature"),
    (_snap(memory={"available_gb": 1.5}), "free RAM"), (_snap(memory={"commit": {"pct": 97.0}}), "memory commit"),
    (_snap(disks=[{"mount": "E:\\", "free_gb": 2.0}]), "disk E:"),
])
def test_decide_pause_stops_everything(snap, word):
    d = governor.decide(snap, None, _cfg())
    assert d["level"] == "pause" and not d["heavy_allowed"] and word in d["reasons"][0]
    assert set(d["caps"].values()) == {0}


def test_decide_new_whea_pauses_old_fatal_cautions():
    evs = whea.parse(SAMPLE)
    d = governor.decide(_snap(), whea.summarize(evs, T_FATAL - 1, T_FATAL + 60), _cfg())
    assert d["level"] == "pause" and "new hardware error" in d["reasons"][0] and "acknowledge" in d["reasons"][0]
    d = governor.decide(_snap(), whea.summarize(evs, T_FATAL + 1, T_FATAL + 60), _cfg())
    assert d["level"] == "caution" and "fatal hardware error" in d["reasons"][0]
    d = governor.decide(_snap(), whea.summarize(evs, T_FATAL - 1, T_FATAL + 60), _cfg(whea_watch=False))
    assert d["level"] == "ok"


def test_decide_unknown_readings_do_not_trip():
    d = governor.decide({"cpu": {"temp_c": None, "load_pct": None}, "gpu": {}, "memory": {}, "disks": []}, None, _cfg())
    assert d["level"] == "ok"


def test_decide_switches():
    hot = _snap(cpu={"temp_c": 95.0})
    off = governor.decide(hot, None, _cfg(enabled=False))
    assert off["level"] == "ok" and off["caps"]["render"] == 1 and not off["enabled"]
    gov = _cfg()
    gov["govern"]["render"] = False
    d = governor.decide(hot, None, gov)
    assert d["level"] == "pause" and d["caps"]["render"] == 1 and d["caps"]["pytest"] == 0


def test_decide_only_watched_disks():
    snap = _snap(disks=[{"mount": "C:\\", "free_gb": 1.0}, {"mount": "E:\\", "free_gb": 500.0}])
    assert governor.decide(snap, None, _cfg(disks=["E:"]))["level"] == "ok"
    assert governor.decide(snap, None, _cfg())["level"] == "pause"


def test_validate():
    assert governor.validate({"enabled": False, "caps": {"pytest": "3", "nope": 1}, "ram_pause_gb": "2.5"}) == \
        {"enabled": False, "caps": {"pytest": 3}, "ram_pause_gb": 2.5}
    for bad in ({"enabled": "yes"}, {"ram_pause_gb": "x"}, {"ram_pause_gb": -1}, {"rm_rf": 1}):
        with pytest.raises(ValueError):
            governor.validate(bad)


def test_test_suite_default_is_off():
    cfg = governor.settings()
    assert cfg["enabled"] is False and cfg["monitor"] is False
    assert governor.cap("pytest", 6) == 6


def test_cap_uses_fresh_shared_verdict(guard_on):
    v = {"level": "caution", "caps": {"pytest": 2}, "heavy_allowed": True, "reasons": ["x"]}
    governor.write_json(guard_on / "state.json", {"ts": time.time(), "verdict": v})
    assert governor.cap("pytest", 6) == 2 and governor.cap("pytest", 1) == 1


def test_ack_first_start_then_acknowledge(guard_on):
    t0 = governor.ack_ts()
    assert abs(t0 - time.time()) < 5 and governor.ack_ts() == t0
    rec = governor.acknowledge("tester", "checked")
    assert governor.ack_ts() == rec["ts"] >= t0


# ---- the slot ----------------------------------------------------------------------------------------------------------
@pytest.fixture
def allowed(guard_on, monkeypatch):
    state = {"v": {"level": "ok", "heavy_allowed": True, "reasons": [], "caps": {}}}
    monkeypatch.setattr(governor, "verdict", lambda max_age_s=60.0: state["v"])
    return state


def test_slot_exclusive_and_reentrant(allowed):
    a = slot.acquire("render", "r1", pid=os.getpid())
    assert a["granted"] and a["holder"]["kind"] == "render"
    assert slot.acquire("build", "same pid", pid=os.getpid())["granted"]          # the pipeline's tests then build
    b = slot.acquire("model_load", "other", token="someone-else")
    assert not b["granted"] and "busy: render" in b["reason"]
    assert not slot.release(token="someone-else")
    assert slot.release() and slot.holder() is None


def test_slot_clears_dead_holder(allowed, guard_on):
    governor.write_json(guard_on / "slot.json", {"kind": "render", "pid": 999999999, "token": "999999999"})
    assert slot.holder() is None
    assert slot.acquire("pytest", "now", token="t")["granted"]
    slot.release(token="t")


def test_slot_refused_while_paused(allowed):
    allowed["v"] = {"level": "pause", "heavy_allowed": False, "reasons": ["new hardware error"], "caps": {}}
    r = slot.acquire("render", "r", wait_s=0)
    assert not r["granted"] and "paused" in r["reason"] and slot.holder() is None
    with pytest.raises(slot.Refused):
        with slot.heavy("model_load", "m"):
            pass


def test_slot_heavy_blocks_another_job_in_the_same_process(allowed):
    with slot.heavy("render", "video"):
        with pytest.raises(slot.Refused, match="busy"):
            with slot.heavy("model_load", "load"):
                pass
    assert slot.holder() is None


def test_slot_not_exclusive_or_ungoverned(allowed, monkeypatch):
    assert slot.acquire("render", cfg={**_cfg(), "exclusive": False})["holder"] is None
    gov = _cfg()
    gov["govern"]["render"] = False
    assert slot.acquire("render", cfg=gov)["reason"] == "not governed"
    assert slot.holder() is None


# ---- OS counters -------------------------------------------------------------------------------------------------------
def test_parse_windows_counters():
    raw = {"gpu_mem": [{"p": "luid_0x0_phys_0", "v": 2 * 2**30}],
           "gpu_eng": [{"p": "pid_1_luid_0x0_eng_0_engtype_3D", "v": 30.0}, {"p": "pid_2_luid_0x0_eng_0_engtype_3D", "v": 25.0},
                       {"p": "pid_2_luid_0x0_eng_1_engtype_Compute", "v": 10.0}],
           "lhm": {"n": "Core (Tctl/Tdie)", "i": "/amdcpu/0/temperature/2", "v": 71.5}, "acpi": []}
    gpu, temps, un = sensors.parse_windows(raw, 24.0)
    assert gpu["vram_used_gb"] == 2.0 and gpu["vram_pct"] == 8.3 and gpu["load_pct"] == 55.0
    assert temps["cpu_c"] == 71.5 and temps["source"] == "hardware monitor"
    assert any("GPU temperature" in u for u in un) and not any("CPU temperature" in u for u in un)


def test_parse_windows_nothing_readable_says_why():
    gpu, temps, un = sensors.parse_windows({}, None)
    assert gpu["vram_used_gb"] is None and temps["cpu_c"] is None
    assert len(un) == 3 and any("ACPI" in u for u in un)


def test_parse_windows_acpi_zone():
    _, temps, _ = sensors.parse_windows({"acpi": [{"n": "TZ0", "v": 3232}]}, None)
    assert abs(temps["cpu_c"] - 50.05) < 0.1 and temps["source"] == "acpi thermal zone"   # tenths of a kelvin


# ---- API and panel -----------------------------------------------------------------------------------------------------
def test_api_routes(allowed, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from bot import db
    from bot.dashboard import hwguard_api
    from bot.hwguard import monitor

    st = {"ts": time.time(), "snapshot": _snap(), "whea": {"new": [], "ack_ts": 1.0}, "events": [],
          "verdict": governor.decide(_snap(), None, _cfg())}
    monkeypatch.setattr(governor, "evaluate", lambda **_: st)
    audits = []
    monkeypatch.setattr(db, "log_audit", lambda **kw: audits.append(kw))
    saved = {}
    from bot.config import config
    monkeypatch.setattr(config, "set_values", lambda changes, actor="": saved.update(changes))
    monitor._reset_for_tests()
    app = FastAPI()
    hwguard_api.register(app, lambda: None, lambda: None)
    c = TestClient(app)
    got = c.get("/api/hwguard/state").json()
    assert got["latest"]["verdict"]["level"] == "ok" and len(got["history"]) == 1
    assert c.put("/api/hwguard/settings", json={"alerts": False, "govern": {"render": False}}).status_code == 200
    assert saved == {("hwguard", "alerts"): False, ("hwguard", "govern", "render"): False}
    assert c.put("/api/hwguard/settings", json={"alerts": "no"}).status_code == 400
    assert c.post("/api/hwguard/ack", json={"note": "seen"}).status_code == 200
    assert c.post("/api/hwguard/slot/release").json()["released"] is False
    assert [a["action"] for a in audits] == ["hwguard_settings", "hwguard_whea_ack", "hwguard_slot_release"]


def test_panel_copies_identical_and_mounted():
    a = (ROOT / "bot/dashboard/static/hwguard-panel.js").read_text(encoding="utf-8")
    assert a == (ROOT / "desktop-app/ui/hwguard-panel.js").read_text(encoding="utf-8")
    for page, src in (("bot/dashboard/static/dashboard.html", "/static/hwguard-panel.js"), ("desktop-app/ui/index.html", "hwguard-panel.js")):
        html = (ROOT / page).read_text(encoding="utf-8")
        assert 'id="hw-root"' in html and 'href="#hardware"' in html and f'src="{src}"' in html


def test_cli_workers_and_acquire(allowed, capsys):
    from bot.hwguard.__main__ import main

    assert main(["acquire", "--kind", "render", "--pid", str(os.getpid())]) == 0
    assert main(["release", "--pid", str(os.getpid())]) == 0
    allowed["v"] = {"level": "pause", "heavy_allowed": False, "reasons": ["hot"], "caps": {"pytest": 0}}
    assert main(["acquire", "--kind", "render", "--pid", str(os.getpid())]) == 3
    capsys.readouterr()
    assert main(["workers", "--kind", "pytest", "--want", "6"]) == 0
    assert capsys.readouterr().out.strip() == "0"
