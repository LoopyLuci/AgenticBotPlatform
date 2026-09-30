"""Git stacks and their poller (bot/git_stacks.py) against a real local git repo, with `docker compose` stubbed:
the hazards octopus-ops/PORTAINER-EXIT.md lists (env wiped on redeploy, retrying a bad commit for ever, a push that
silently does not deploy, self-hosting stacks first) must not happen."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from bot import docker_mgr as dk, git_stacks as gs


def git(*a, cwd):
    subprocess.run(["git", *a], cwd=cwd, check=True, capture_output=True,
                   env={"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
                        "PATH": __import__("os").environ["PATH"], "SYSTEMROOT": __import__("os").environ.get("SYSTEMROOT", "")})


def commit(repo: Path, text: str) -> str:
    (repo / "docker-compose.yml").write_text(f"services:\n  web:\n    image: nginx # {text}\n", encoding="utf-8")
    git("add", "-A", cwd=repo)
    git("commit", "-qm", text, cwd=repo)
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def env(temp_db, tmp_path, monkeypatch):
    repo = tmp_path / "app"
    repo.mkdir()
    git("init", "-q", "-b", "main", cwd=repo)
    first = commit(repo, "v1")
    monkeypatch.setattr(dk, "_stack_dir", lambda n: tmp_path / "stacks" / n)
    monkeypatch.setattr(dk, "is_installed", lambda: True)
    calls = []

    def run(args, timeout=0, cwd=None, **kw):
        env_file = Path(args[args.index("--env-file") + 1])
        calls.append({"args": args, "env": env_file.read_text(encoding="utf-8") if env_file.exists() else None,
                      "compose": Path(args[args.index("-f") + 1]).read_text(encoding="utf-8")})
        return (not calls[-1]["compose"].count("broken")), "compose output"
    monkeypatch.setattr(dk, "_run", run)
    cfg = {"poll_interval_s": 60, "debounce_s": 0}
    monkeypatch.setattr(gs, "_cfg", lambda: cfg)
    return {"repo": repo, "first": first, "calls": calls, "cfg": cfg}


def test_deploy_writes_the_stored_env_every_time_and_never_keeps_it(env):
    gs.add("app", str(env["repo"]), env={"SECRET": "s3cret", "MODE": "prod"})
    r = gs.deploy("app")
    assert r["ok"] and r["commit"] == env["first"]
    assert env["calls"][0]["env"] == "SECRET=s3cret\nMODE=prod\n"
    assert "--env-file" in env["calls"][0]["args"] and "up" in env["calls"][0]["args"]
    assert not list(Path(env["repo"]).parent.rglob(".abp.env"))          # not left on disk
    st = gs.get("app")
    assert st["env_keys"] == ["MODE", "SECRET"] and "s3cret" not in str(st) and "env_sealed" not in st
    gs.update("app", ref="main")                                            # no env given: it is kept
    gs.deploy("app")
    assert env["calls"][1]["env"] == "SECRET=s3cret\nMODE=prod\n"


def test_the_poller_deploys_a_push_once_and_does_not_retry_a_failed_commit(env):
    gs.add("app", str(env["repo"]), auto_deploy=True)
    assert [d["decision"] for d in gs.poll_once()] == ["deployed"]
    assert [d["decision"] for d in gs.poll_once()] == ["current"]
    bad = commit(env["repo"], "broken")
    assert [d["decision"] for d in gs.poll_once()] == ["deploy_failed"]
    assert gs.get("app")["failed_commit"] == bad
    n = len(env["calls"])
    assert [d["decision"] for d in gs.poll_once()] == ["skip_failed"] and len(env["calls"]) == n   # no build loop
    fixed = commit(env["repo"], "v3")
    assert [d["decision"] for d in gs.poll_once()] == ["deployed"] and gs.get("app")["deployed_commit"] == fixed
    kinds = [e["kind"] for e in gs.events(stack="app")]
    assert "deploy_failed" in kinds and "new_commit" in kinds                # failures are loud


def test_debounce_shadow_pause_and_self_hosting_last(env):
    env["cfg"].update(debounce_s=30, shadow=True, self_hosting=["aaa-self"])
    gs.add("aaa-self", str(env["repo"]), auto_deploy=True)
    gs.add("zzz-app", str(env["repo"]), auto_deploy=True)
    first = gs.poll_once(now=1000)
    assert [(d["stack"], d["decision"]) for d in first] == [("zzz-app", "debounce"), ("aaa-self", "debounce")]
    assert [d["decision"] for d in gs.poll_once(now=1010)] == ["debounce", "debounce"]
    assert [d["decision"] for d in gs.poll_once(now=1040)] == ["would_deploy", "would_deploy"]
    assert env["calls"] == []                                                  # shadow mode deploys nothing
    env["cfg"]["paused"] = True
    assert gs.poll_once() == [{"stack": "*", "decision": "paused"}]


@pytest.mark.parametrize("kw", [{"repo": "--upload-pack=touch /tmp/x"}, {"repo": "file:///etc"}, {"ref": "../x"},
                                {"compose_file": "../../etc/passwd"}, {"env": {"BAD KEY": "x"}}, {"env": {"A": "1\nB=2"}}])
def test_bad_definitions_are_refused(env, kw):
    args = {"name": "app", "repo": str(env["repo"]), **kw}
    with pytest.raises(gs.StackError):
        gs.add(args.pop("name"), args.pop("repo"), **({"ref": args["ref"]} if "ref" in args else {}),
               **({"compose_file": args["compose_file"]} if "compose_file" in args else {}), env=args.get("env"))


def test_routes(env, monkeypatch):
    monkeypatch.setenv("DASHBOARD_TOKEN", "test-token")
    from bot.dashboard.server import build_app
    c = TestClient(build_app())
    D = {"X-Dashboard-Token": "test-token"}
    r = c.post("/api/docker/git-stacks", headers=D, json={"name": "app", "repo": str(env["repo"]), "env": {"K": "v"}})
    assert r.status_code == 200 and r.json()["env_keys"] == ["K"]
    assert c.post("/api/docker/git-stacks/app/deploy", headers=D, json={}).json()["ok"] is True
    assert c.get("/api/docker/git-stacks", headers=D).json()["stacks"][0]["deployed_commit"] == env["first"]
    key = c.post("/api/integrations/keys", headers=D, json={"scopes": ["docker:read", "docker:deploy"]}).json()["key"]
    K = {"X-Dashboard-Token": key}
    assert c.get("/api/docker/git-stacks", headers=K).status_code == 200
    assert c.post("/api/docker/git-stacks/app/deploy", headers=K, json={}).status_code == 200
    assert c.post("/api/docker/git-stacks", headers=K, json={"name": "x", "repo": "/x"}).status_code == 403
    assert c.patch("/api/docker/git-stacks/app", headers=K, json={"env": {}}).status_code == 403
    router = c.post("/api/integrations/keys", headers=D, json={"preset": "octopus-router", "allow_framing": False}).json()["key"]
    assert c.post("/api/docker/git-stacks/app/deploy", headers={"X-Dashboard-Token": router}, json={}).status_code == 403


def test_update_remove_and_a_missing_repo_are_handled(env, monkeypatch):
    gs.add("app", str(env["repo"]), env={"A": "1"})
    s = gs.update("app", compose_file="docker-compose.yml", auto_deploy=True, pull=True, env={"B": "2"})
    assert s["auto_deploy"] and s["pull"] and s["env_keys"] == ["B"]
    assert gs.deploy("app")["ok"] and "--pull" in env["calls"][-1]["args"] and env["calls"][-1]["env"] == "B=2\n"
    with pytest.raises(gs.StackError):
        gs.add("app", str(env["repo"]))                                     # the name is taken
    gs.add("gone", str(env["repo"]) + "-missing", auto_deploy=True)
    decisions = {d["stack"]: d["decision"] for d in gs.poll_once()}
    assert decisions["gone"] == "check_failed"
    assert any(e["kind"] == "check_failed" for e in gs.events(stack="gone"))
    r = gs.deploy("gone")
    assert r["ok"] is False and gs.get("gone")["last_error"]
    assert gs.remove("gone")["removed"] == "gone" and [x["name"] for x in gs.listing()] == ["app"]
    with pytest.raises(gs.StackError):
        gs.get("gone")
    missing = env["repo"] / "docker-compose.yml"
    missing.unlink()
    import subprocess as sp
    sp.run(["git", "commit", "-qam", "no compose"], cwd=env["repo"], capture_output=True,
           env={**__import__("os").environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"})
    assert gs.deploy("app")["ok"] is False and "not in" in gs.get("app")["last_error"]
