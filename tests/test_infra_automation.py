"""Infra automation rules: validation, watch conditions, cooldown, and that
actions only ever go through the validated manager functions."""
from __future__ import annotations

import pytest

from bot import docker_mgr as dk, infra_automation as auto, tailscale_mgr as ts


@pytest.fixture
def acted(monkeypatch, temp_db):
    seen = []
    monkeypatch.setattr(dk, "container_action", lambda t, a: seen.append((t, a)) or {"output": "done"})
    return seen


def _watch(when="exited", target="web"):
    return {"type": "watch", "resource": "container", "target": target, "when": when}


@pytest.mark.parametrize("trigger,action", [
    ({"type": "cron"}, {"type": "notify"}),
    ({"type": "interval", "every": "5s"}, {"type": "notify"}),
    (_watch("melting"), {"type": "notify"}),
    (_watch(), {"type": "shell", "cmd": "rm -rf /"}),
    (_watch(), {"type": "container"}),
    (_watch(), {"type": "tailscale_prefs", "settings": {"nonsense": 1}}),
    (_watch(), {"type": "exec", "target": "web", "command": "not a list"}),
])
def test_invalid_rules_are_rejected(temp_db, trigger, action):
    with pytest.raises(auto.RuleError):
        auto.create("r", trigger, action)


def test_a_watch_rule_restarts_an_exited_container_then_honours_cooldown(monkeypatch, acted):
    monkeypatch.setattr(dk, "containers", lambda all_=True: [
        {"Names": "web", "ID": "abc", "State": "exited", "Status": "Exited (1)"}])
    rule = auto.create("keep web up", _watch("exited"),
                       {"type": "container", "target": "web", "action": "restart"}, cooldown_s=300)
    assert auto.run_rule(rule["id"])["fired"] is True
    assert acted == [("web", "restart")]
    assert auto.run_rule(rule["id"]) == {"fired": False, "reason": "cooldown"}
    assert len(acted) == 1 and auto.get(rule["id"])["runs"] == 1
    assert auto.history(rule["id"])[0]["ok"] == 1


def test_a_healthy_container_does_not_trigger(monkeypatch, acted):
    monkeypatch.setattr(dk, "containers", lambda all_=True: [
        {"Names": "web", "ID": "abc", "State": "running", "Status": "Up 2h (healthy)"}])
    rule = auto.create("r", _watch("unhealthy"), {"type": "container", "target": "web", "action": "restart"})
    assert auto.run_rule(rule["id"])["fired"] is False and acted == []


def test_unhealthy_and_cpu_conditions(monkeypatch):
    monkeypatch.setattr(dk, "containers", lambda all_=True: [
        {"Names": "web", "ID": "abc", "State": "running", "Status": "Up (unhealthy)"}])
    monkeypatch.setattr(dk, "container_stats", lambda ident=None: [{"CPUPerc": "93.5%", "MemPerc": "10%"}])
    assert auto.condition_met(_watch("unhealthy"), 0, None)
    assert auto.condition_met(_watch("cpu>80"), 0, None)
    assert not auto.condition_met(_watch("mem>80"), 0, None)


def test_interval_rule_fires_first_then_waits(acted):
    rule = auto.create("nightly prune", {"type": "interval", "every": "1h"}, {"type": "notify", "message": "hi"})
    assert auto.run_rule(rule["id"])["fired"] is True
    assert auto.run_rule(rule["id"])["reason"] == "condition not met"


def test_tailscale_disconnected_reconnects(monkeypatch, temp_db):
    monkeypatch.setattr(ts, "status", lambda: {"BackendState": "Stopped"})
    monkeypatch.setattr(ts, "up", lambda *a, **k: {"ok": True})
    rule = auto.create("stay connected", {"type": "watch", "resource": "tailscale", "when": "disconnected"},
                       {"type": "tailscale_up"})
    assert auto.run_rule(rule["id"])["ok"] is True


def test_a_failing_action_is_recorded_not_raised(monkeypatch, temp_db):
    def boom(t, a):
        raise dk.DockerError("daemon down")
    monkeypatch.setattr(dk, "container_action", boom)
    rule = auto.create("r", {"type": "interval", "every": "1m"},
                       {"type": "container", "target": "web", "action": "restart"})
    res = auto.run_rule(rule["id"])
    assert res["ok"] is False and "daemon down" in res["detail"]
    assert auto.get(rule["id"])["last_result"].startswith("failed")


def test_disabled_rules_never_fire_and_can_be_deleted(acted):
    rule = auto.create("r", {"type": "interval", "every": "1m"}, {"type": "notify"}, enabled=False)
    assert auto.tick() == []
    auto.delete(rule["id"])
    assert auto.list_rules() == []


def test_cli_infra_paths_map_onto_the_routes():
    import asyncio
    from bot.dashboard_client import DashboardClient

    seen = []

    class C(DashboardClient):
        def __init__(self):
            pass

        async def _request(self, method, path, **kw):
            seen.append((method, path, kw.get("json")))
            return {}

    c = C()
    asyncio.run(c.infra("docker", "get", "containers"))
    asyncio.run(c.infra("vm", "get", ""))
    asyncio.run(c.infra("tailscale", "post", "serve", {"target": "3000"}))
    assert seen == [("GET", "/api/docker/containers", None), ("GET", "/api/vms", None),
                    ("POST", "/api/tailscale/serve", {"target": "3000"})]
