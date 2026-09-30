"""abp_modkit: making any project an ABP module. The spec, detection over a small polyglot project, a real hub running
commands, jobs and the project's own server, adopting (and refreshing without losing hand edits), ABP finding and
driving an adopted project (hub, operations, provider, web frame), the adopt API, and publishing's safety checks."""
from __future__ import annotations

import json
import socket
import subprocess
import sys
import textwrap
import time
import tomllib
from pathlib import Path

import pytest

from abp_modkit import adopt as ad
from abp_modkit import detect as dt
from abp_modkit import repo as rp
from abp_modkit import spec as sp

ROOT = Path(__file__).resolve().parent.parent


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _w(p: Path, text: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(textwrap.dedent(text).lstrip("\n"), encoding="utf-8")


# ---- the spec ---------------------------------------------------------------------------------------------------------
def test_a_spec_round_trips_and_bad_ones_are_refused():
    s = sp.Spec(sp.Service("demo", "Demo", base_url="http://127.0.0.1:1234", health="/health", start=["{python}", "s.py"]),
                [sp.Op("items.get_by_id", "http", method="GET", path="/items/{id}"),
                 sp.Op("cli.scan", "cmd", argv=["{python}", "cli.py", "scan", "{target}"], extra_args=True,
                       inputs={"target": {"type": "string", "required": True}}, summary="Scan")])
    back = sp.parse(tomllib.loads(sp.dump(s)))
    assert [o.id for o in back.ops] == ["items.get_by_id", "cli.scan"]
    assert back.op("items.get_by_id").params == ["id"] and back.op("cli.scan").params == ["target"]
    assert back.op("cli.scan").mutating and not back.op("items.get_by_id").mutating
    for bad, why in [({"service": {"id": "x"}}, "needs id and name"),
                     ({"service": {"id": "x", "name": "X"}, "op": [{"id": "a.b", "kind": "http", "path": "/x"}]},
                      "base_url is not set"),
                     ({"service": {"id": "x", "name": "X"}, "op": [{"id": "service.start", "argv": ["y"]}]}, "built-in"),
                     ({"service": {"id": "x", "name": "X"}, "op": [{"id": "a.b", "argv": ["y", "{nope}"]}]}, "nope"),
                     ({"service": {"id": "x", "name": "X", "web": "/"}}, "need service.base_url")]:
        with pytest.raises(sp.SpecError, match=why):
            sp.parse(bad)


# ---- detection ----------------------------------------------------------------------------------------------------
@pytest.fixture
def poly(tmp_path) -> Path:
    """A small project in five languages: a FastAPI service with a CLI, an Express server, a clap binary, a PowerShell
    module, MicroPython firmware."""
    root = tmp_path / "PolyGlot"
    _w(root / "README.md", "# PolyGlot\n\nA small project in many languages, for testing adoption end to end.\n")
    _w(root / "pyproject.toml", """
        [project]
        name = "polyglot"
        version = "0.1.0"
        [project.scripts]
        poly = "polyglot.cli:main"
    """)
    _w(root / "polyglot/__init__.py", "")
    _w(root / "polyglot/cli.py", """
        import argparse
        def main():
            ap = argparse.ArgumentParser()
            sub = ap.add_subparsers(dest="cmd")
            sub.add_parser("scan", help="Scan a folder")
            sub.add_parser("report")
    """)
    _w(root / "polyglot/api.py", """
        from fastapi import APIRouter, FastAPI
        app = FastAPI()
        items = APIRouter(prefix="/items")
        @app.get("/health")
        def health(): return {"ok": True}
        @items.get("/{item_id}")
        def one(item_id: int): return {}
        @app.post("/v1/chat/completions")
        def chat(): return {}
    """)
    _w(root / "web/package.json", json.dumps({"name": "poly-web", "scripts": {"start": "node server.js", "lint": "eslint ."},
                                              "dependencies": {"express": "^4"}}))
    _w(root / "web/server.js", "const app = require('express')();\napp.get('/api/things/:id', h);\n"
                               "app.post('/api/things', h);\napp.listen(process.env.PORT || 4000);\n")
    _w(root / "tool/Cargo.toml", '[package]\nname = "polytool"\nversion = "0.1.0"\n[dependencies]\nclap = "4"\n')
    _w(root / "tool/src/main.rs", """
        #[derive(Subcommand)]
        enum Cmd {
            /// Pack a folder
            Pack { path: String },
            #[command(name = "unpack-all")]
            UnpackAll,
            Status,
        }
    """)
    _w(root / "ps/Poly.psm1", """
        function Get-Thing {
            param([Parameter(Mandatory)][string]$Name, [int]$Count)
            $Name
        }
        function Remove-Thing { param([string]$Name) }
        function helper { }
        Export-ModuleMember -Function Get-Thing, Remove-Thing
    """)
    _w(root / "firmware/pico/boot.py", "")
    _w(root / "firmware/pico/main.py", "print('hi')\n")
    return root


def test_detect_reads_every_part_of_a_polyglot_project(poly):
    plan = dt.detect(poly)
    ids = {o.id for o in plan.ops}
    assert plan.id == "poly-glot" and plan.name == "PolyGlot" and "many languages" in plan.description
    assert {"python", "node", "rust", "powershell", "micropython"} <= set(plan.stacks)
    # the Python CLI's subcommands, the clap subcommands (renamed ones too), the PowerShell exports, npm scripts
    assert {"poly.scan", "poly.report", "polytool.pack", "polytool.unpack_all", "polytool.status",
            "ps.get_thing", "ps.remove_thing", "poly_web.start", "poly_web.lint"} <= ids, ids
    assert "ps.helper" not in ids                                            # not exported
    get = next(o for o in plan.ops if o.id == "ps.get_thing")
    assert get.inputs["Name"].get("required") and get.inputs["Count"]["type"] == "integer" and not get.mutating
    assert next(o for o in plan.ops if o.id == "ps.remove_thing").mutating
    # the FastAPI app is the server: its routes (with the router prefix) are operations, and it speaks OpenAI
    s = plan.service
    assert s.start[:4] == ["{python}", "-m", "uvicorn", "polyglot.api:app"] and s.base_url.startswith("http://127.0.0.1:")
    assert {("GET", "/health"), ("GET", "/items/{item_id}"), ("POST", "/v1/chat/completions")} <= \
        {(o.method, o.path) for o in plan.ops if o.kind == "http"}
    assert s.health == "/health" and s.openai == "/v1"
    assert any(o.id.startswith("firmware.pico_deploy") for o in plan.ops)
    assert ["cargo", "build", "--release", "-p", "polytool", "--manifest-path", "tool/Cargo.toml"] in plan.build
    assert plan.build_env == {"CARGO_TARGET_DIR": "{target}"}
    assert {"python", "node", "cargo", "powershell"} <= {r["tool"] for r in plan.requires}


def test_a_template_library_gets_a_few_parameterised_ops_not_hundreds(tmp_path):
    for i in range(15):
        _w(tmp_path / "lib" / "starters" / f"w{i}" / "wrangler.toml", f'name = "w{i}"\n')
        _w(tmp_path / "lib" / "starters" / f"w{i}" / "package.json", json.dumps({"name": f"w{i}", "scripts": {"dev": "x"}}))
    plan = dt.detect(tmp_path / "lib")
    assert sorted(o.id for o in plan.ops) == ["worker.deploy", "worker.dev", "worker.install", "worker.list"]
    assert next(o for o in plan.ops if o.id == "worker.deploy").destructive


# ---- the hub ------------------------------------------------------------------------------------------------------
SERVER = """
import json, sys
from http.server import BaseHTTPRequestHandler, HTTPServer
class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def _send(self, code, obj, ctype="application/json"):
        b = obj.encode() if isinstance(obj, str) else json.dumps(obj).encode()
        self.send_response(code); self.send_header("Content-Type", ctype); self.send_header("Content-Length", str(len(b)))
        self.end_headers(); self.wfile.write(b)
    def do_GET(self):
        if self.path == "/health": return self._send(200, {"ok": True})
        if self.path.startswith("/items/"): return self._send(200, {"item": self.path.rsplit("/", 1)[1]})
        if self.path == "/v1/models": return self._send(200, {"data": [{"id": "tiny"}]})
        if self.path == "/": return self._send(200, "<html>hello</html>", "text/html")
        self._send(404, {"error": "no"})
    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0); body = json.loads(self.rfile.read(n) or b"{}")
        self._send(200, {"echo": body, "auth": self.headers.get("Authorization", "")})
HTTPServer(("127.0.0.1", int(sys.argv[1])), H).serve_forever()
"""


@pytest.fixture
def project(tmp_path) -> Path:
    """A project with a server and commands, and a hand-written abp-ops.toml."""
    root = tmp_path / "Tiny"
    port = _free_port()
    _w(root / "server.py", SERVER)
    _w(root / "sub" / "marker.txt", "x")
    _w(root / "abp-ops.toml", f"""
        [service]
        id = "tiny"
        name = "Tiny"
        base_url = "http://127.0.0.1:{port}"
        health = "/health"
        start = ["{{python}}", "server.py", "{{port}}"]
        web = "/"
        openai = "/v1"
        auth = "bearer-env:TINY_TOKEN"
        ready_timeout_s = 20

        [[op]]
        id = "items.get"
        kind = "http"
        method = "GET"
        path = "/items/{{id}}"

        [[op]]
        id = "echo.post"
        kind = "http"
        method = "POST"
        path = "/echo"

        [[op]]
        id = "cli.greet"
        kind = "cmd"
        argv = ["{{python}}", "-c", "import json,os,sys; print(json.dumps({{'argv': sys.argv[1:], 'env': json.loads(os.environ['ABP_OP_ARGS'])}}))", "--who", "{{who}}"]
        extra_args = true
        mutating = false
        [op.inputs]
        who = {{ type = "string" }}

        [[op]]
        id = "cli.where"
        kind = "cmd"
        argv = ["{{python}}", "-c", "import os; print(os.path.basename(os.getcwd()))"]
        cwd = "{{place}}"
        [op.inputs]
        place = {{ type = "string", required = true, enum = ["sub", "."] }}

        [[op]]
        id = "cli.slow"
        kind = "cmd"
        argv = ["{{python}}", "-c", "import time; print('start', flush=True); time.sleep(0.5); print('end')"]
        background = true

        [[op]]
        id = "cli.hang"
        kind = "cmd"
        argv = ["{{python}}", "-c", "import time; time.sleep(60)"]
        timeout_s = 1
    """)
    return root


@pytest.fixture
def hub(project, tmp_path):
    from abp_modkit.cli import _client
    home = tmp_path / "home"
    p = subprocess.Popen([sys.executable, "-m", "abp_modkit", "serve", "--spec", str(project / "abp-ops.toml"),
                          "--project", str(project), "--home", str(home)], cwd=ROOT, stdout=subprocess.DEVNULL,
                         stderr=subprocess.PIPE, env={**__import__("os").environ, "TINY_TOKEN": "tiny-secret"})
    for _ in range(100):
        if (home / "control.json").is_file():
            break
        time.sleep(0.1)
    req = _client(home)

    def call(op, args=None):
        return req("POST", f"/v1/call/{op}", args or {})["result"]
    yield req, call
    try:
        call("service.stop")
        req("POST", "/v1/service/stop", {})
    except Exception:  # noqa: BLE001
        pass
    try:
        p.wait(10)
    except subprocess.TimeoutExpired:
        p.kill()


def test_the_hub_runs_commands_jobs_and_the_projects_server(hub):
    req, call = hub
    ops = {o["id"]: o for o in req("GET", "/v1/operations")["operations"]}
    assert {"service.status", "service.start", "service.stop", "service.logs", "api.request", "jobs.get",
            "project.info", "items.get", "cli.greet"} <= set(ops)
    assert ops["cli.greet"]["input_schema"]["properties"]["args"]["type"] == "array"
    # commands: inputs become arguments (and $ABP_OP_ARGS), extra args are appended, a missing optional one drops out
    out = call("cli.greet", {"who": "ada", "args": ["x"]})
    assert out["ok"] and json.loads(out["output"]) == {"argv": ["--who", "ada", "x"], "env": {"who": "ada"}}
    assert json.loads(call("cli.greet")["output"])["argv"] == []
    assert call("cli.where", {"place": "sub"})["output"].strip() == "sub"
    with pytest.raises(RuntimeError, match="one of the listed"):
        call("cli.where", {"place": ".."})
    with pytest.raises(RuntimeError, match="place is required"):
        call("cli.where")
    hung = call("cli.hang")
    assert hung["timed_out"] and not hung["ok"]
    job = call("cli.slow")
    assert job["state"] == "running"
    for _ in range(50):
        j = call("jobs.get", {"id": job["id"]})
        if j["state"] != "running":
            break
        time.sleep(0.1)
    assert j["state"] == "done" and "end" in j["output"]
    # the project's own server: not up yet, then started by the hub and driven through its routes
    st = call("service.status")["service"]
    assert st["managed"] and not st["answers"]
    with pytest.raises(RuntimeError, match="does not answer"):
        call("items.get", {"id": "7"})
    st = call("service.start")
    assert st["running"] and st["answers"] and st["web"].endswith("/") and st["openai"].endswith("/v1")
    assert call("items.get", {"id": "7"})["data"] == {"item": "7"}
    echoed = call("echo.post", {"body": {"a": 1}})["data"]
    assert echoed["echo"] == {"a": 1} and echoed["auth"] == "Bearer tiny-secret"      # auth = bearer-env:TINY_TOKEN
    assert call("api.request", {"method": "GET", "path": "/v1/models"})["data"]["data"][0]["id"] == "tiny"
    with pytest.raises(RuntimeError, match="must start with /"):
        call("api.request", {"method": "GET", "path": "/../etc"})
    assert call("service.start")["pid"] == st["pid"]                                    # already running: no second one
    assert call("service.stop")["answers"] is False
    assert call("project.info")["path"].endswith("Tiny")


# ---- adopting -------------------------------------------------------------------------------------------------------
def test_adopt_writes_both_files_and_a_refresh_keeps_hand_edits(poly):
    res = ad.adopt(poly)
    assert set(res["wrote"]) == {"abp-module.toml", "abp-ops.toml"} and res["operations"] > 10
    man = tomllib.loads((poly / "abp-module.toml").read_text(encoding="utf-8"))
    assert man["module"]["id"] == "poly-glot" and man["hub"]["start"][:3] == ["{abp_python}", "-m", "abp_modkit"]
    assert man["provider"]["openai"] == "service"
    from bot.modules import manifest as mf
    mf.parse(man)                                                     # ABP accepts it as it is
    # a person edits a summary, the port and adds an op; adopting again keeps all three
    s = sp.load(poly / "abp-ops.toml")
    s.op("poly.scan").summary = "Scan, carefully"
    s.service.base_url = "http://127.0.0.1:18999"
    s.ops.append(sp.Op("mine.hello", "cmd", argv=["echo", "hi"], summary="mine"))
    (poly / "abp-ops.toml").write_text(sp.dump(s), encoding="utf-8")
    ad.adopt(poly)
    s2 = sp.load(poly / "abp-ops.toml")
    assert s2.op("poly.scan").summary == "Scan, carefully" and s2.op("mine.hello") is not None
    assert s2.service.base_url == "http://127.0.0.1:18999"
    # a manifest the person took over (no generated marker) is left alone
    (poly / "abp-module.toml").write_text((poly / "abp-module.toml").read_text(encoding="utf-8").split("\n", 1)[1],
                                          encoding="utf-8")
    assert ad.adopt(poly)["kept"] == ["abp-module.toml"]
    # and a curated abp-ops.toml (its first line is no longer the generated one) is left exactly as it is
    curated = "\n".join(["# PolyGlot's operations, kept by hand.", "", "[service]", 'id = "poly-glot"',
                         'name = "PolyGlot"', "", "[[op]]", 'id = "only.this"', 'kind = "cmd"',
                         'argv = ["echo", "hi"]', ""])
    (poly / "abp-ops.toml").write_text(curated, encoding="utf-8")
    res = ad.adopt(poly)
    assert set(res["kept"]) == {"abp-module.toml", "abp-ops.toml"} and res["operations"] == 1
    assert (poly / "abp-ops.toml").read_text(encoding="utf-8") == curated


def test_new_makes_a_module_that_passes_its_own_check(tmp_path):
    from abp_modkit import cli
    for lang in ("python", "node"):
        d = tmp_path / f"new-{lang}"
        cli.new(d, lang, "")
        res = cli.check(d, verbose=False)
        assert res["ok"], res["checks"]


# ---- ABP drives an adopted project ---------------------------------------------------------------------------------
@pytest.fixture
def adopted(project, tmp_path, monkeypatch):
    import shutil

    from bot.config import config
    from bot.modules import adoption, harness, registry
    ad.adopt(project)                       # writes abp-module.toml; the hand-written abp-ops.toml is merged, kept
    cfg_file = tmp_path / "backends.yaml"   # a real copy of the config, registered into for real
    shutil.copy(config.path, cfg_file)
    monkeypatch.setattr(config, "path", cfg_file)
    monkeypatch.setattr(config, "_data", dict(config._data))
    config.reload(actor="test")
    adoption.register(project)
    monkeypatch.setattr(registry, "data_dir", lambda m: tmp_path / "data" / m.id)
    monkeypatch.setenv("TINY_TOKEN", "tiny-secret")
    registry.modules(refresh=True)
    yield project
    try:
        harness.call("tiny", "service.stop", {})
        harness.stop_hub("tiny")
    except Exception:  # noqa: BLE001
        pass
    monkeypatch.undo()
    registry.modules(refresh=True)


def test_abp_finds_an_adopted_project_and_drives_it(adopted, monkeypatch):
    from bot import integrations, providers
    from bot.modules import harness, registry
    m = registry.get("tiny")
    assert registry.install_dir(m) == adopted and m.web == "service" and m.openai == "service"
    assert registry.placeholders(m)["abp_python"] == sys.executable
    assert harness.start_hub("tiny")["running"]
    assert "items.get" in {o["id"] for o in harness.operations("tiny")}
    assert harness.service_urls(m) == {}                                  # its server is not running yet
    harness.call("tiny", "service.start", {})
    harness._svc_cache.clear()
    urls = harness.service_urls(m)
    assert urls["web"].endswith("/") and urls["openai"].endswith("/v1")
    assert harness.status("tiny")["service"] == urls
    # while it runs it is a model provider, and ABP's pages may show its web UI
    monkeypatch.delenv("ABP_NO_MODULE_PROVIDERS", raising=False)
    monkeypatch.setattr(providers, "_module_cache", (0.0, {}))
    entry = providers.module_providers()["tiny"]
    assert entry["protocol"] == "openai" and entry["base_url"] == urls["openai"]
    monkeypatch.setattr(integrations, "_mod_origins", (0.0, []))
    monkeypatch.setattr(integrations, "_origins", lambda key: [])
    assert urls["web"].rstrip("/") in integrations.frame_src()


def test_the_adopt_route_the_tool_and_candidates(poly, tmp_path, monkeypatch, temp_db):
    from fastapi.testclient import TestClient

    from bot.config import config
    from bot.dashboard.server import build_app
    from bot.modules import adoption, registry
    import shutil
    monkeypatch.setenv("DASHBOARD_TOKEN", "test-token")
    cfg_file = tmp_path / "backends.yaml"                  # a real copy of the config: adopting really writes to it
    shutil.copy(config.path, cfg_file)
    monkeypatch.setattr(config, "path", cfg_file)
    monkeypatch.setattr(config, "_data", dict(config._data))
    config.reload(actor="test")
    c = TestClient(build_app())
    H = {"X-Dashboard-Token": "test-token"}
    r = c.post("/api/modules/adopt", headers=H, json={"path": str(poly), "dry_run": True})
    assert r.status_code == 200 and r.json()["files"]["abp-module.toml"].startswith(ad.MARKER)
    assert not (poly / "abp-module.toml").exists()                          # a preview writes nothing
    assert c.post("/api/modules/adopt", headers=H, json={}).status_code == 400
    assert c.post("/api/modules/adopt", json={"path": str(poly)}).status_code == 401
    r = c.post("/api/modules/adopt", headers=H, json={"path": str(poly)})
    assert r.status_code == 200 and r.json()["registered"] is True
    import yaml
    on_disk = yaml.safe_load(cfg_file.read_text(encoding="utf-8"))["modules"]["projects"]
    assert str(poly.resolve()).replace("\\", "/") in on_disk
    assert registry.get("poly-glot").name == "PolyGlot"
    rows = c.get("/api/modules/candidates", headers=H, params={"folder": str(tmp_path)}).json()["projects"]
    assert {"name": "PolyGlot", "module": "poly-glot"}.items() <= next(x for x in rows if x["name"] == "PolyGlot").items()
    assert c.post("/api/modules/poly-glot/forget", headers=H).json()["forgotten"] == "poly-glot"
    assert c.post("/api/modules/vm-harness/forget", headers=H).status_code == 400    # built in, not adopted
    assert adoption.adopt(str(poly), dry_run=True)["id"] == "poly-glot"


# ---- publishing -----------------------------------------------------------------------------------------------------
def test_publish_refuses_secrets_and_commits_only_the_module_files(tmp_path):
    d = tmp_path / "Proj"
    _w(d / "app.py", "print('hi')\n")
    _w(d / "deploy" / "id_rsa", "not a real key\n")
    with pytest.raises(rp.PublishError, match=r"deploy/id_rsa: a secrets file"):
        rp.publish(d, stacks=["python"], create=False, push=False)
    (d / "deploy" / "id_rsa").unlink()
    _w(d / "big.dat", "x")
    res = rp.publish(d, stacks=["python"], create=False, push=False)
    assert any("committed" in x for x in res["log"])                       # (the refused try already ran git init)
    ignore = (d / ".gitignore").read_text(encoding="utf-8")
    assert ".venv/" in ignore and ".env" in ignore and "*.gguf" in ignore
    # a project with history: only the named files are committed; other work in progress is left alone
    _w(d / "abp-module.toml", "x = 1\n")
    _w(d / "wip.py", "unfinished\n")
    rp.publish(d, stacks=["python"], only=["abp-module.toml"], create=False, push=False)
    status = subprocess.run(["git", "-C", str(d), "status", "--porcelain"], capture_output=True, text=True).stdout
    assert "wip.py" in status and "abp-module.toml" not in status
    assert rp.gitignore(d, ["python"]) is False                                  # nothing more to add


def test_the_scan_names_the_file_and_kind_never_the_value(tmp_path):
    token = "ghp_" + "A1b2C3d4" * 5
    _w(tmp_path / "conf.py", f"TOKEN = '{token}'\n")
    found = rp.scan(tmp_path, ["conf.py"])
    assert found == [("conf.py", "github token")] and token not in repr(found)


def test_a_process_with_no_http_side_runs_with_its_stored_secret(tmp_path):
    """A chat bot or worker: the hub keeps its process running and hands it its token from the secret store."""
    from abp_modkit.cli import _client
    proj = tmp_path / "Bot"
    _w(proj / "bot.py", """
        import os, sys, time
        tok = os.environ.get("BOT_TOKEN", "")
        if not tok:
            sys.exit("no token")
        print("logged in with a token of", len(tok), "characters", flush=True)
        while True:
            time.sleep(1)
    """)
    _w(proj / "abp-ops.toml", """
        [service]
        id = "bot"
        name = "Bot"
        start = ["{python}", "bot.py"]
        env = { BOT_TOKEN = "{secret:BOT_TOKEN}" }
    """)
    home = tmp_path / "home"
    p = subprocess.Popen([sys.executable, "-m", "abp_modkit", "serve", "--spec", str(proj / "abp-ops.toml"), "--project",
                          str(proj), "--home", str(home)], cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(100):
            if (home / "control.json").is_file():
                break
            time.sleep(0.1)
        req = _client(home)

        def call(op, args=None):
            return req("POST", f"/v1/call/{op}", args or {})["result"]
        assert call("service.status")["service"]["secrets"] == {"BOT_TOKEN": False}
        with pytest.raises(RuntimeError, match="needs BOT_TOKEN"):
            call("service.start")
        call("service.set_secret", {"name": "BOT_TOKEN", "value": "a-token-of-24-characters"})
        st = call("service.start")
        assert st["running"] and st["answers"] and st["secrets"] == {"BOT_TOKEN": True}
        assert "24 characters" in call("service.logs")["log"]
        assert "a-token-of" not in json.dumps(call("service.status"))                   # never handed back
        assert call("service.stop")["running"] is False
    finally:
        try:
            req("POST", "/v1/service/stop", {})
        except Exception:  # noqa: BLE001
            pass
        try:
            p.wait(10)
        except subprocess.TimeoutExpired:
            p.kill()


def test_publish_leaves_other_staged_work_alone_and_checks_history_before_a_first_push(tmp_path):
    d = tmp_path / "Old"
    _w(d / "app.py", "x = 1\n")
    subprocess.run(["git", "init", "-q", "-b", "main", str(d)], check=True)
    git = ["git", "-C", str(d), "-c", "user.name=t", "-c", "user.email=t@example.invalid"]
    subprocess.run([*git, "add", "."], check=True)
    subprocess.run([*git, "commit", "-q", "-m", "one"], check=True)
    _w(d / "staged.py", "wip\n")
    subprocess.run([*git, "add", "staged.py"], check=True)                   # the person's own staged work
    _w(d / "abp-module.toml", "x = 1\n")
    rp.publish(d, stacks=["python"], only=["abp-module.toml"], create=False, push=False)
    status = subprocess.run([*git, "status", "--porcelain"], capture_output=True, text=True).stdout
    assert "A  staged.py" in status                                           # still staged, not in our commit
    assert "abp-module.toml" in subprocess.run([*git, "show", "--stat", "HEAD"], capture_output=True, text=True).stdout
    # a key committed once and deleted later is still in the history a first push would publish
    _w(d / "keys" / "id_ed25519", "not a real key\n")
    subprocess.run([*git, "add", "keys"], check=True)
    subprocess.run([*git, "commit", "-q", "-m", "oops", "--", "keys"], check=True)
    subprocess.run([*git, "rm", "-q", "-r", "keys"], check=True)
    subprocess.run([*git, "commit", "-q", "-m", "remove", "--", "keys"], check=True)
    assert ("keys/id_ed25519", "a secrets file, in its history") in rp.scan_history(d)
    bare = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
    subprocess.run([*git, "remote", "add", "origin", str(bare)], check=True)
    with pytest.raises(rp.PublishError, match="its history"):
        rp.publish(d, stacks=["python"], only=[], create=False, push=True)
    assert not subprocess.run(["git", "-C", str(bare), "rev-parse", "-q", "--verify", "refs/heads/main"],
                              capture_output=True, text=True).stdout.strip()          # nothing was pushed


def test_a_module_that_uses_abp_back_gets_a_key_of_its_own(adopted, temp_db, monkeypatch):
    """[abp] connect = true: ABP mints the module an integration key (companion-app scopes) once, keeps it in the
    module's data folder, and its hub's commands see ABP_URL and ABP_KEY."""
    from bot import integrations
    from bot.modules import harness, registry
    man = adopted / "abp-module.toml"
    man.write_text(man.read_text(encoding="utf-8") + '\n[abp]\nconnect = true\n', encoding="utf-8")
    ops = adopted / "abp-ops.toml"
    ops.write_text(ops.read_text(encoding="utf-8") + '''
[[op]]
id = "cli.abp"
kind = "cmd"
argv = ["{python}", "-c", "import os; print(os.environ.get('ABP_URL', '') + '|' + os.environ.get('ABP_KEY', ''))"]
mutating = false
''', encoding="utf-8")
    registry.modules(refresh=True)
    m = registry.get("tiny")
    assert m.abp_connect and m.public()["uses_abp"]
    monkeypatch.setenv("DASHBOARD_PORT", "8799")
    harness.start_hub("tiny")
    url, key = harness.call("tiny", "cli.abp", {})["output"].strip().split("|")
    assert url == "http://127.0.0.1:8799"
    assert integrations.scopes_for(key) == sorted(integrations.PRESETS["companion-app"]["scopes"])
    assert harness.abp_env(m)["ABP_KEY"] == key                           # the same key next time, not a new one
    row = next(k for k in integrations.list_keys() if k["label"] == "module: Tiny")
    integrations.revoke(row["id"])
    assert harness.abp_env(m)["ABP_KEY"] != key                           # a revoked key is replaced
    # a module without [abp] gets nothing
    assert harness.abp_env(registry.get("vm-harness")) == {}


# ---- overlays: a third-party repo as a module, its checkout untouched -------------------------------------------------
def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, check=True).stdout


@pytest.fixture
def upstream(tmp_path) -> Path:
    """A repo nobody prepared for ABP: a CMake project with a module that is only part of it, a standalone sample, and
    a Python CLI; committed, so any write into it shows in git status."""
    up = tmp_path / "upstream-lib"
    _w(up / "CMakeLists.txt", """
        cmake_minimum_required(VERSION 3.16)
        project(UpLib CXX)
        option(UPLIB_WITH_EXTRAS "Build the extras" OFF)
        add_subdirectory(modules/core)
        """)
    _w(up / "modules/core/CMakeLists.txt", "add_library(core STATIC core.cpp)\n")
    _w(up / "apps/hello/CMakeLists.txt", "cmake_minimum_required(VERSION 3.16)\nproject(Hello CXX)\n")
    _w(up / "stats.py", '''
        """Print statistics."""
        import argparse
        if __name__ == "__main__":
            ap = argparse.ArgumentParser()
            ap.add_argument("--n", default="3")
            print("stats", ap.parse_args().n)
        ''')
    _w(up / "README.md", "# UpLib\n\nA small library that is not ours.\n")
    _git(up.parent, "init", "-q", str(up))
    _git(up, "-c", "user.email=t@example.invalid", "-c", "user.name=t", "add", "-A")
    _git(up, "-c", "user.email=t@example.invalid", "-c", "user.name=t", "commit", "-q", "-m", "upstream")
    return up


def test_cmake_projects_are_detected_and_parts_of_a_build_are_not_projects(upstream):
    plan = dt.detect(upstream)
    ids = [o.id for o in plan.ops]
    assert "cmake" in plan.stacks
    assert {"cmake.configure", "cmake.build", "cmake.test", "cmake.install", "cmake.options"} <= set(ids)
    assert not any(i.startswith("cmake_core.") for i in ids)            # no project(): a part of the outer build
    assert not any(i.startswith("cmake_hello.") for i in ids)           # inside the outer project: its build builds it
    # samples with no outer project (a folder of standalone samples): each gets configure and build, nothing more
    samples = upstream.parent / "samples-only"
    _w(samples / "one/CMakeLists.txt", "cmake_minimum_required(VERSION 3.16)\nproject(One CXX)\n")
    _w(samples / "two/CMakeLists.txt", "cmake_minimum_required(VERSION 3.16)\nproject(Two CXX)\n")
    sids = [o.id for o in dt.detect(samples).ops]
    assert sorted(sids) == ["cmake_one.build", "cmake_one.configure", "cmake_two.build", "cmake_two.configure"]
    configure = next(o for o in plan.ops if o.id == "cmake.configure")
    assert "{target}/cmake-build" in configure.argv and "UPLIB_WITH_EXTRAS" in configure.summary


def test_an_overlay_leaves_the_checkout_untouched_and_abp_drives_it(upstream, tmp_path, monkeypatch):
    import shutil

    from abp_modkit import cli
    from bot.config import config
    from bot.modules import harness, registry
    overlays = tmp_path / "overlays"
    res = ad.adopt(upstream, mid="uplib", overlay=overlays / "uplib", record_checkout=True)
    assert res["overlay"] == str((overlays / "uplib").resolve())
    assert _git(upstream, "status", "--porcelain") == ""                 # nothing written into the checkout
    man = (overlays / "uplib" / "abp-module.toml").read_text(encoding="utf-8")
    assert '"{overlay}/abp-ops.toml"' in man and '"CMakeLists.txt"' in man
    assert f'dir = "{str(upstream.resolve()).replace(chr(92), "/")}"' in man
    assert cli.check(overlays / "uplib", verbose=False, project=upstream)["ok"]
    # ABP finds it through modules.overlay_dirs and runs its operations against the checkout
    cfg_file = tmp_path / "backends.yaml"
    shutil.copy(config.path, cfg_file)
    monkeypatch.setattr(config, "path", cfg_file)
    monkeypatch.setattr(config, "_data", dict(config._data))
    config.reload(actor="test")
    config.set_value(("modules", "overlay_dirs"), [str(overlays)], actor="test")
    monkeypatch.setattr(registry, "data_dir", lambda m: tmp_path / "data" / m.id)
    registry.modules(refresh=True)
    try:
        m = registry.get("uplib")
        assert m.overlay == str((overlays / "uplib").resolve()) and m.public()["overlay"]
        assert registry.install_dir(m) == upstream.resolve()
        assert registry.placeholders(m)["overlay"] == m.overlay
        assert harness.start_hub("uplib")["running"]
        out = harness.call("uplib", "script.stats", {"args": ["--n", "7"]})
        assert out["exit_code"] == 0 and "stats 7" in out["output"]
        assert _git(upstream, "status", "--porcelain") == ""
    finally:
        try:
            harness.stop_hub("uplib")
        except Exception:  # noqa: BLE001
            pass
        monkeypatch.undo()
        registry.modules(refresh=True)
