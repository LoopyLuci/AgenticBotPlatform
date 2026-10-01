"""The module hub: a project's routes and commands as ABP operations (ABP's module contract, v1).

Listens on 127.0.0.1 (a random free port unless one is given), makes a random token and writes
`<home>/control.json` = {url, token, pid, version, api}. Everything but health needs `Authorization: Bearer <token>`.

    GET  /v1/health          {ok, pid, version, uptime_s, service}
    GET  /v1/operations      every operation with a JSON Schema for its input
    POST /v1/call/{op}       run one; the answer is {"result": ...}
    POST /v1/service/stop    stop (and stop the project's server, if this hub started it)

Built in: service.status / start / stop / logs / set_secret, jobs.list / get / cancel, project.info, and api.request
when the project has an HTTP API. The rest come from abp-ops.toml.
"""
from __future__ import annotations

import hmac
import json
import os
import re
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from abp_modkit import __version__
from abp_modkit.spec import Op, Spec

MAX_BODY = 8 << 20
MAX_OUT = 64 << 10
_PH = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")
WIN = os.name == "nt"


class Fail(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


def _schema(props: dict, required: list[str]) -> dict:
    return {"type": "object", "properties": props, "required": required, "additionalProperties": False}


def _new_group() -> dict:
    """Popen arguments that give the child its own process group/session, so it can be stopped with all it started."""
    if WIN:
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | getattr(subprocess, "CREATE_NO_WINDOW", 0)}
    return {"start_new_session": True}


def kill_tree(pid: int) -> None:
    if WIN:
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, timeout=30)
        return
    try:
        pgid = os.getpgid(pid)
        if pgid != os.getpgid(0):
            os.killpg(pgid, signal.SIGTERM)
            time.sleep(1.5)
            try:
                os.killpg(pgid, signal.SIGKILL)
            except OSError:
                pass
            return
        os.kill(pid, signal.SIGTERM)
    except OSError:
        pass


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if WIN:
        r = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True, timeout=15)
        return str(pid) in r.stdout
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


class Tail:
    """The last MAX_OUT bytes of a stream (a command's combined output)."""

    def __init__(self) -> None:
        self.chunks: deque[str] = deque()
        self.size = 0
        self.dropped = 0

    def add(self, s: str) -> None:
        self.chunks.append(s)
        self.size += len(s)
        while self.size > MAX_OUT and len(self.chunks) > 1:
            d = self.chunks.popleft()
            self.size -= len(d)
            self.dropped += len(d)

    def text(self) -> str:
        t = "".join(self.chunks)
        return (f"[... {self.dropped} earlier characters dropped]\n" if self.dropped else "") + t[-MAX_OUT:]


class Hub:
    def __init__(self, spec: Spec, project: Path, home: Path, base_url: str | None = None,
                 variables: dict[str, str] | None = None) -> None:
        self.spec = spec
        self.variables = dict(variables or {})     # placeholders ABP passes (--var target=...)
        self.project = project.resolve()
        self.home = home
        self.home.mkdir(parents=True, exist_ok=True)
        self.base_url = (base_url or os.environ.get("ABP_MODKIT_BASE_URL") or spec.service.base_url).rstrip("/")
        self.token = secrets.token_hex(32)
        self.started = time.time()
        self.stats = {"calls": 0, "errors": 0}
        self.jobs: dict[str, dict] = {}
        self._lock = threading.Lock()
        self._svc_started_here = False

    # ---- placeholders ---------------------------------------------------------------------------------------
    def _venv(self) -> Path | None:
        # the project's own venv, else one in the module's data folder (project.python_setup makes it: an overlay
        # module's dependencies live there, never in an upstream checkout nor in ABP's own environment)
        for d in (self.project / ".venv", self.project / "venv", self.home / "venv"):
            if (d / ("Scripts/python.exe" if WIN else "bin/python")).is_file():
                return d
        return None

    def values(self) -> dict[str, str]:
        v = self._venv()
        port = urllib.parse.urlsplit(self.base_url).port if self.base_url else None
        return {"project": str(self.project), "exe": ".exe" if WIN else "", "data": str(self.home),
                "python": str(v / ("Scripts/python.exe" if WIN else "bin/python")) if v else sys.executable,
                "venv_bin": str(v / ("Scripts" if WIN else "bin")) if v else str(Path(sys.executable).parent),
                "port": str(port or ""), "bat": ".bat" if WIN else "",
                "target": str(self.project / "target"),
                # ABP's model store (Ollama layout; ABP exports it when it starts): modules that run models read it in place
                "abp_models": os.environ.get("ABP_MODELS_DIR", ""),
                "pwsh": shutil.which("pwsh") or shutil.which("powershell") or "pwsh", **self.variables}

    def _fill(self, s: str, extra: dict[str, str]) -> str:
        vals = {**self.values(), **extra}
        return _PH.sub(lambda m: vals.get(m.group(1), m.group(0)), s)

    @staticmethod
    def _resolve(argv: list[str]) -> list[str]:
        """npm, npx, gradle... are .cmd files on Windows: Popen needs their full path."""
        return [shutil.which(argv[0]) or argv[0], *argv[1:]] if argv else argv

    def _cwd(self, rel: str, inputs: dict[str, str] | None = None) -> Path:
        p = (self.project / self._fill(rel, inputs or {})).resolve()
        if p != self.project and self.project not in p.parents:
            raise Fail(400, "invalid", f"cwd {rel} is outside the project")
        return p

    # ---- secrets (tokens the project's API wants); never returned ------------------------------------------
    @property
    def _secrets_file(self) -> Path:
        return self.home / "secrets.json"

    def _secrets(self) -> dict:
        try:
            return json.loads(self._secrets_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def set_secret(self, name: str, value: str) -> None:
        d = self._secrets()
        if value:
            d[name] = value
        else:
            d.pop(name, None)
        tmp = self._secrets_file.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(d, fh)
        os.replace(tmp, self._secrets_file)

    def _env_value(self, v: str) -> str:
        """A service env value: {secret:NAME} becomes $NAME or the stored secret NAME ("" when neither exists)."""
        def sec(m: "re.Match") -> str:
            return os.environ.get(m.group(2)) or self._secrets().get(m.group(2), "")
        return self._fill(re.sub(r"\{secret(\??):([A-Za-z_][A-Za-z0-9_]*)\}", sec, v), {})

    def _missing_secrets(self, env: dict[str, str]) -> list[str]:
        out = []
        for v in env.values():
            for opt, name in re.findall(r"\{secret(\??):([A-Za-z_][A-Za-z0-9_]*)\}", v):
                if not opt and not (os.environ.get(name) or self._secrets().get(name)):
                    out.append(name)
        return out

    def secret_names(self) -> list[str]:
        envs = [*self.spec.service.env.values(), *(v for o in self.spec.ops for v in o.env.values())]
        return sorted(set(re.findall(r"\{secret\??:([A-Za-z_][A-Za-z0-9_]*)\}", " ".join(envs))
                          + ([self.spec.service.auth.split(":", 1)[1]] if self.spec.service.auth.startswith("bearer-env:")
                             else [])))

    def _bearer(self) -> str:
        a = self.spec.service.auth
        if not a.startswith("bearer-env:"):
            return ""
        name = a.split(":", 1)[1]
        return os.environ.get(name) or self._secrets().get(name, "")

    # ---- the project's own server ---------------------------------------------------------------------------
    @property
    def _pid_file(self) -> Path:
        return self.home / "service.pid"

    def _svc_pid(self) -> int:
        try:
            return int(self._pid_file.read_text().strip())
        except (OSError, ValueError):
            return 0

    def _answers(self) -> bool:
        if not self.base_url:                  # a process with no HTTP side (a chat bot): alive is answering
            pid = self._svc_pid()
            return bool(pid) and pid_alive(pid)
        if self.spec.service.health:
            try:
                return self._http("GET", self.base_url + self.spec.service.health, None, timeout=3)[0] < 500
            except Fail:
                return False
        u = urllib.parse.urlsplit(self.base_url)
        try:
            with socket.create_connection((u.hostname or "127.0.0.1", u.port or 80), timeout=2):
                return True
        except OSError:
            return False

    def service_status(self) -> dict:
        pid = self._svc_pid()
        running = pid_alive(pid) if pid else False
        if pid and not running:
            self._pid_file.unlink(missing_ok=True)
        t0 = time.time()
        up = self._answers()
        stored = self._secrets()
        needs = {n: bool(os.environ.get(n) or stored.get(n)) for n in self.secret_names()}
        return {"managed": bool(self.spec.service.start), "pid": pid if running else None, "running": running,
                **({"secrets": needs} if needs else {}),
                "answers": up, "ms": round((time.time() - t0) * 1000) if up else None, "base_url": self.base_url or None,
                "web": (self.base_url + self.spec.service.web) if self.base_url and self.spec.service.web else None,
                "openai": (self.base_url + self.spec.service.openai) if self.base_url and self.spec.service.openai else None}

    def service_start(self) -> dict:
        s = self.spec.service
        if not s.start:
            raise Fail(409, "not_managed", f"{s.name} has no server for ABP to start (service.start is empty)")
        with self._lock:
            if self._svc_pid() and pid_alive(self._svc_pid()):
                return self.service_status()
            if self._answers():
                return {**self.service_status(), "note": "something already answers at base_url; not starting another"}
            argv = [self._fill(x, {}) for x in s.start]
            env = {**os.environ, **{k: self._env_value(v) for k, v in s.env.items()}}
            missing = self._missing_secrets(s.env)
            if missing:
                raise Fail(409, "needs_secret", f"{s.name} needs {', '.join(missing)}: store each with "
                                                "service.set_secret (or set it in the environment)")
            log = open(self.home / "service.log", "ab")  # noqa: SIM115 - handed to the child
            log.write(f"\n==== {time.strftime('%Y-%m-%d %H:%M:%S')} starting: {' '.join(argv)}\n".encode())
            log.flush()
            try:
                p = subprocess.Popen(self._resolve(argv), cwd=self._cwd(s.cwd), env=env, stdout=log, stderr=subprocess.STDOUT,
                                     stdin=subprocess.DEVNULL, **_new_group())
            except OSError as e:
                raise Fail(500, "start_failed", f"could not start {s.name}: {e}") from e
            finally:
                log.close()
            self._pid_file.write_text(str(p.pid))
            self._svc_started_here = True
        deadline = time.time() + (s.ready_timeout_s if self.base_url else 3)   # a bare process: it stays up 3 s
        while time.time() < deadline:
            if not self.base_url:
                time.sleep(0.5)
                if p.poll() is None:
                    continue
            if p.poll() is not None:
                self._pid_file.unlink(missing_ok=True)
                raise Fail(500, "exited", f"{s.name} exited with code {p.returncode}:\n" + self.logs(40))
            if self._answers():
                return self.service_status()
            time.sleep(0.5)
        if not self.base_url:
            return self.service_status()
        return {**self.service_status(), "note": f"started, but it did not answer within {int(s.ready_timeout_s)} s"}

    def service_stop(self) -> dict:
        pid = self._svc_pid()
        if pid and pid_alive(pid):
            kill_tree(pid)
        self._pid_file.unlink(missing_ok=True)
        self._svc_started_here = False
        return self.service_status()

    def logs(self, lines: int = 200) -> str:
        try:
            data = (self.home / "service.log").read_bytes()[-MAX_OUT:]
        except OSError:
            return ""
        return "\n".join(data.decode("utf-8", "replace").splitlines()[-max(1, min(lines, 2000)):])

    # ---- commands --------------------------------------------------------------------------------------------
    @staticmethod
    def _given(op: Op, a: dict) -> dict:
        """The inputs a call gave, with each missing one's `default` (from abp-ops.toml) filled in."""
        given = {k: v for k, v in a.items() if k != "args" and v is not None and v != ""}
        for k, spec in op.inputs.items():
            if k not in given and spec.get("default") not in (None, ""):
                given[k] = spec["default"]
        return given

    def _argv(self, op: Op, a: dict) -> list[str]:
        given = self._given(op, a)
        for k in op.params:
            if k not in given:
                raise Fail(400, "invalid", f"{k} is required")
        for k in given:
            if k not in op.inputs:
                raise Fail(400, "invalid", f"unknown input {k} (inputs: {', '.join(op.inputs) or 'none'})")
        strs = {k: (json.dumps(v) if isinstance(v, (dict, list)) else str(v)) for k, v in given.items()
                if not op.inputs[k].get("flag")}
        out: list[str] = []
        for x in op.argv:
            names = _PH.findall(x)
            missing = [n for n in names if n in op.inputs and n not in strs]
            if missing:
                if out and x.startswith("{") and x.endswith("}") and out[-1].startswith("-"):
                    out.pop()                                  # "--name", "{name}" with no name: drop both
                continue
            out.append(self._fill(x, strs))
        for k, spec in op.inputs.items():
            if spec.get("flag") and given.get(k):
                out.append(str(spec["flag"]))
                if not isinstance(given[k], bool):
                    out.append(str(given[k]))
        for k, v in op.inputs.items():                         # an enum input (a folder to run in) must be one of them
            if v.get("enum") and k in given and str(given[k]) not in [str(x) for x in v["enum"]]:
                raise Fail(400, "invalid", f"{k} must be one of the listed values")
        extra = a.get("args")
        if extra is not None:
            if not op.extra_args:
                raise Fail(400, "invalid", f"{op.id} takes no extra arguments")
            if not isinstance(extra, list) or not all(isinstance(x, (str, int, float)) for x in extra):
                raise Fail(400, "invalid", "args must be a list of strings")
            out += [str(x) for x in extra]
        return out

    def _run(self, op: Op, argv: list[str], a: dict, job: dict | None) -> dict:
        strs = {k: str(v) for k, v in self._given(op, a).items() if isinstance(v, (str, int, float))}
        missing = self._missing_secrets(op.env)
        if missing:
            raise Fail(409, "needs_secret", f"{op.id} needs {', '.join(missing)}: store each with service.set_secret")
        # env values may use the call's inputs too ({name}), like argv does
        env = {**os.environ, **{k: self._fill(self._env_value(v), strs) for k, v in op.env.items()},
               "ABP_OP_ARGS": json.dumps({k: v for k, v in a.items() if k != "args"}), "PYTHONIOENCODING": "utf-8"}
        tail = Tail()
        t0 = time.time()
        try:
            p = subprocess.Popen(self._resolve(argv), cwd=self._cwd(op.cwd, strs), env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                 stdin=subprocess.DEVNULL, **_new_group())
        except OSError as e:
            raise Fail(500, "start_failed", f"could not run {argv[0]}: {e}") from e
        if job is not None:
            job.update(pid=p.pid, tail=tail)

        def pump():
            for raw in iter(lambda: p.stdout.read1(4096) if hasattr(p.stdout, "read1") else p.stdout.read(4096), b""):
                tail.add(raw.decode("utf-8", "replace"))
        t = threading.Thread(target=pump, daemon=True)
        t.start()
        timed_out = False
        try:
            p.wait(timeout=op.timeout_s)
        except subprocess.TimeoutExpired:
            timed_out = True
            kill_tree(p.pid)
            p.wait(10)
        t.join(5)
        return {"exit_code": p.returncode, "ok": p.returncode == 0 and not timed_out, "timed_out": timed_out,
                "seconds": round(time.time() - t0, 2), "output": tail.text(), "argv": argv}

    def _start_job(self, op: Op, argv: list[str], a: dict) -> dict:
        jid = uuid.uuid4().hex[:12]
        job = {"id": jid, "op": op.id, "state": "running", "started": time.time(), "argv": argv}
        self.jobs[jid] = job

        def go():
            try:
                job["result"] = self._run(op, argv, a, job)
                job["state"] = "done" if job["result"]["ok"] else "failed"
            except Fail as e:
                job.update(state="failed", error=e.message)
            job["ended"] = time.time()
        threading.Thread(target=go, daemon=True).start()
        return self._job_view(job)

    def _job_view(self, job: dict, full: bool = False) -> dict:
        out = {k: job.get(k) for k in ("id", "op", "state", "started", "ended", "error")}
        res = job.get("result")
        if res:
            out.update(exit_code=res["exit_code"], seconds=res["seconds"], timed_out=res["timed_out"])
        tail = res["output"] if res else (job["tail"].text() if job.get("tail") else "")
        out["output"] = tail if full else tail[-4000:]
        return out

    # ---- operations ------------------------------------------------------------------------------------------
    def operations(self) -> list[dict]:
        s = self.spec.service
        empty = _schema({}, [])
        out = [
            {"id": "service.status", "group": "service", "summary": f"{s.name}: its server (running? answering?), "
             "operations, jobs and calls", "mutating": False, "destructive": False, "input_schema": empty},
            {"id": "project.info", "group": "project", "summary": "The project: folder, git branch and commit, "
             "uncommitted files, README", "mutating": False, "destructive": False, "input_schema": empty},
            {"id": "project.python_setup", "group": "project", "summary": "A Python environment for the project in "
             "the module's data folder (never in the checkout): create it, install requirement files and packages "
             "(a background job); commands' {python} use it from then on", "mutating": True, "destructive": False,
             "input_schema": _schema({"requirements": {"type": "array", "items": {"type": "string"},
                                                       "description": "requirement files, relative to the project "
                                                                      "(default: requirements.txt if it has one)"},
                                      "packages": {"type": "array", "items": {"type": "string"},
                                                   "description": "more packages to install"}}, [])},
            {"id": "jobs.list", "group": "jobs", "summary": "Commands running or finished in the background",
             "mutating": False, "destructive": False, "input_schema": empty},
            {"id": "jobs.get", "group": "jobs", "summary": "One background command: its state and output",
             "mutating": False, "destructive": False,
             "input_schema": _schema({"id": {"type": "string"}}, ["id"])},
            {"id": "jobs.cancel", "group": "jobs", "summary": "Stop a background command", "mutating": True,
             "destructive": False, "input_schema": _schema({"id": {"type": "string"}}, ["id"])},
            {"id": "service.set_secret", "group": "service", "summary": "Store a token the project's API needs "
             "(never returned)", "mutating": True, "destructive": False,
             "input_schema": _schema({"name": {"type": "string"}, "value": {"type": "string"}}, ["name", "value"])},
        ]
        if s.start:
            out += [
                {"id": "service.start", "group": "service", "summary": f"Start {s.name}'s server and wait until it "
                 "answers", "mutating": True, "destructive": False, "input_schema": empty},
                {"id": "service.stop", "group": "service", "summary": f"Stop {s.name}'s server (and all it started)",
                 "mutating": True, "destructive": False, "input_schema": empty},
                {"id": "service.logs", "group": "service", "summary": f"The end of {s.name}'s server log",
                 "mutating": False, "destructive": False,
                 "input_schema": _schema({"lines": {"type": "integer", "minimum": 1, "maximum": 2000}}, [])},
            ]
        if self.base_url:
            out.append({"id": "api.request", "group": "api", "summary": f"Any request to {s.name}'s HTTP API",
                        "mutating": True, "destructive": True,
                        "input_schema": _schema({"method": {"type": "string", "enum": ["GET", "POST", "PUT", "PATCH",
                                                                                      "DELETE"]},
                                                 "path": {"type": "string", "description": "starts with /"},
                                                 "query": {"type": "object"}, "body": {}}, ["method", "path"])})
        for o in self.spec.ops:
            if o.kind == "http":
                props: dict[str, Any] = {p: {"type": "string"} for p in o.params}
                props["query"] = {"type": "object", "description": "query-string parameters"}
                if o.method != "GET":
                    props["body"] = {"description": "the JSON body"}
                extra = {"http": {"method": o.method, "path": o.path}}
            else:
                props = {}
                for k, v in o.inputs.items():
                    t = v.get("type") or ("boolean" if v.get("flag") and "value" not in v else "string")
                    props[k] = {"type": t, **({"description": v["description"]} if v.get("description") else {}),
                                **({"enum": v["enum"]} if v.get("enum") else {}),
                                **({"default": v["default"]} if "default" in v else {})}
                if o.extra_args:
                    props["args"] = {"type": "array", "items": {"type": "string"},
                                     "description": "more arguments, appended to the command"}
                extra = {"command": o.argv, "background": o.background}
            out.append({"id": o.id, "group": o.id.split(".")[0], "summary": o.summary or o.id, "kind": o.kind,
                        "mutating": o.mutating, "destructive": o.destructive, **extra,
                        "input_schema": _schema(props, o.params)})
        return out

    def call(self, op_id: str, a: dict) -> Any:
        s = self.spec.service
        if op_id == "service.status":
            return {"module": s.id, "name": s.name, "project": str(self.project), "service": self.service_status(),
                    "operations": len(self.spec.ops), "jobs": sum(1 for j in self.jobs.values() if j["state"] == "running"),
                    "stats": self.stats, "version": __version__}
        if op_id == "project.info":
            return self._project_info()
        if op_id == "project.python_setup":
            return self._python_setup(a)
        if op_id == "jobs.list":
            return {"jobs": [self._job_view(j) for j in sorted(self.jobs.values(), key=lambda j: -j["started"])][:50]}
        if op_id in ("jobs.get", "jobs.cancel"):
            job = self.jobs.get(str(a.get("id") or ""))
            if job is None:
                raise Fail(404, "not_found", f"no job {a.get('id')}")
            if op_id == "jobs.cancel" and job["state"] == "running" and job.get("pid"):
                kill_tree(job["pid"])
                job["state"] = "cancelled"
            return self._job_view(job, full=True)
        if op_id == "service.set_secret":
            name = str(a.get("name") or "")
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,63}", name) or not isinstance(a.get("value"), str):
                raise Fail(400, "invalid", "name (letters, digits, _) and value (a string) are required")
            self.set_secret(name, a["value"])
            return {"stored": name}
        if op_id == "service.start":
            return self.service_start()
        if op_id == "service.stop":
            return self.service_stop()
        if op_id == "service.logs":
            return {"log": self.logs(int(a.get("lines") or 200))}
        if op_id == "api.request" and self.base_url:
            method = str(a.get("method", "")).upper()
            path = str(a.get("path", ""))
            if method not in ("GET", "POST", "PUT", "PATCH", "DELETE") or not path.startswith("/") or "//" in path \
                    or ".." in path:
                raise Fail(400, "invalid", "method must be GET/POST/PUT/PATCH/DELETE and path must start with / "
                                           "(no .. or //)")
            return self._request(method, path, a.get("query"), a.get("body"))
        op = self.spec.op(op_id)
        if op is None:
            raise Fail(404, "not_found", f"no operation {op_id}")
        self.stats["calls"] += 1
        if op.kind == "http":
            return self._request(op.method, self._path(op, a), a.get("query"), a.get("body"))
        argv = self._argv(op, a)
        if op.background:
            return self._start_job(op, argv, a)
        res = self._run(op, argv, a, None)
        if not res["ok"]:
            self.stats["errors"] += 1
        return res

    _SETUP = ("import json, os, subprocess, sys, venv\n"
              "d, reqs, pkgs = sys.argv[1], json.loads(sys.argv[2]), json.loads(sys.argv[3])\n"
              "py = os.path.join(d, 'Scripts' if os.name == 'nt' else 'bin', 'python' + ('.exe' if os.name == 'nt' else ''))\n"
              "if not os.path.isfile(py):\n"
              "    print('creating', d, flush=True); venv.create(d, with_pip=True)\n"
              "args = [x for r in reqs for x in ('-r', r)] + pkgs\n"
              "if args:\n"
              "    sys.exit(subprocess.call([py, '-m', 'pip', 'install', '--disable-pip-version-check', *args]))\n"
              "print('ready:', py)\n")

    def _python_setup(self, a: dict) -> dict:
        reqs = a.get("requirements")
        if reqs is None:
            reqs = ["requirements.txt"] if (self.project / "requirements.txt").is_file() else []
        pkgs = a.get("packages") or []
        if not isinstance(reqs, list) or not isinstance(pkgs, list) or \
                not all(isinstance(x, str) for x in [*reqs, *pkgs]):
            raise Fail(400, "invalid", "requirements and packages are lists of strings")
        root = self.project.resolve()
        files = []
        for r in reqs:
            f = (root / r).resolve()
            if root not in f.parents or not f.is_file():
                raise Fail(400, "invalid", f"{r}: not a file inside the project")
            files.append(str(f))
        if any(p.startswith("-") for p in pkgs):
            raise Fail(400, "invalid", "packages are names (with versions), not pip options")
        op = Op(id="project.python_setup", kind="cmd", background=True, timeout_s=3600)
        return self._start_job(op, [sys.executable, "-c", self._SETUP, str(self.home / "venv"), json.dumps(files),
                                    json.dumps(pkgs)], {})

    def _project_info(self) -> dict:
        def git(*args):
            try:
                r = subprocess.run(["git", "-C", str(self.project), *args], capture_output=True, text=True, timeout=15)
                return r.stdout.strip() if r.returncode == 0 else ""
            except (OSError, subprocess.TimeoutExpired):
                return ""
        readme = next((self.project / n for n in ("README.md", "readme.md", "README.txt", "README")
                       if (self.project / n).is_file()), None)
        return {"path": str(self.project), "branch": git("rev-parse", "--abbrev-ref", "HEAD") or None,
                "commit": git("log", "--oneline", "-1") or None, "remote": git("remote", "get-url", "origin") or None,
                "uncommitted": len(git("status", "--porcelain").splitlines()),
                "readme": readme.read_text(encoding="utf-8", errors="replace")[:6000] if readme else None}

    def _path(self, op: Op, a: dict) -> str:
        path = op.path
        for p in op.params:
            v = a.get(p)
            if v is None or str(v) == "":
                raise Fail(400, "invalid", f"{p} is required")
            qv = urllib.parse.quote(str(v), safe="")
            for pat in (f":{p}?", f":{p}", "{" + p + "}", f"[{p}]", f"<{p}>"):
                path = path.replace(pat, qv)
            path = re.sub(r"<\w+:" + re.escape(p) + ">", qv, path)
        return path

    def _request(self, method: str, path: str, query: Any, body: Any) -> dict:
        url = self.base_url + path
        if isinstance(query, dict) and query:
            url += ("&" if "?" in url else "?") + urllib.parse.urlencode(
                {k: v for k, v in query.items() if v is not None}, doseq=True)
        status, ctype, data = self._http(method, url, body)
        parsed: Any
        if "json" in ctype:
            try:
                parsed = json.loads(data or b"null")
            except ValueError:
                parsed = data.decode("utf-8", "replace")[:20000]
        else:
            parsed = data.decode("utf-8", "replace")[:20000]
        if status >= 400:
            self.stats["errors"] += 1
            raise Fail(502 if status >= 500 else status, "upstream", f"{self.spec.service.name} answered HTTP {status}: "
                       + (json.dumps(parsed)[:500] if not isinstance(parsed, str) else parsed[:500]))
        return {"status": status, "content_type": ctype, "data": parsed}

    def _http(self, method: str, url: str, body: Any, timeout: float = 120) -> tuple[int, str, bytes]:
        headers = {"User-Agent": f"abp-modkit/{__version__}", "Accept": "application/json, */*"}
        data = None
        if body is not None and method != "GET":
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        tok = self._bearer()
        if tok:
            headers["Authorization"] = f"Bearer {tok}"
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 - the project's own loopback API
                return r.status, r.headers.get("content-type", ""), r.read(MAX_BODY)
        except urllib.error.HTTPError as e:
            return e.code, e.headers.get("content-type", "") if e.headers else "", e.read(MAX_BODY) if e.fp else b""
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            hint = " (start it with service.start)" if self.spec.service.start else ""
            raise Fail(502, "unreachable", f"{self.spec.service.name} does not answer at {self.base_url}{hint}: "
                                           f"{getattr(e, 'reason', e)}") from e

    def shutdown(self) -> None:
        for j in self.jobs.values():
            if j["state"] == "running" and j.get("pid"):
                kill_tree(j["pid"])
        if self._svc_started_here:
            self.service_stop()


def serve(hub: Hub, host: str = "127.0.0.1", port: int = 0) -> None:
    stop = threading.Event()

    class H(BaseHTTPRequestHandler):
        server_version = f"abp-modkit/{__version__}"

        def log_message(self, fmt, *args):  # quiet
            pass

        def _send(self, status: int, obj: Any) -> None:
            b = json.dumps(obj, default=str).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)

        def _authed(self) -> bool:
            h = self.headers.get("Authorization", "")
            return h.startswith("Bearer ") and hmac.compare_digest(h[7:].encode(), hub.token.encode())

        def do_GET(self):  # noqa: N802
            if self.path == "/v1/health":
                return self._send(200, {"ok": True, "pid": os.getpid(), "version": __version__,
                                        "uptime_s": int(time.time() - hub.started), "service": hub.spec.id})
            if not self._authed():
                return self._send(401, {"error": {"code": "unauthorized", "message": "this needs the hub's token"}})
            if self.path == "/v1/operations":
                return self._send(200, {"api": 1, "operations": hub.operations()})
            self._send(404, {"error": {"code": "not_found", "message": self.path}})

        def do_POST(self):  # noqa: N802
            if not self._authed():
                return self._send(401, {"error": {"code": "unauthorized", "message": "this needs the hub's token"}})
            n = int(self.headers.get("Content-Length") or 0)
            if n > MAX_BODY:
                return self._send(413, {"error": {"code": "too_large", "message": "body too large"}})
            raw = self.rfile.read(n) if n else b"{}"
            if self.path == "/v1/service/stop":
                self._send(200, {"result": {"stopping": True}})
                stop.set()
                return
            if not self.path.startswith("/v1/call/"):
                return self._send(404, {"error": {"code": "not_found", "message": self.path}})
            try:
                args = json.loads(raw or b"{}")
                if not isinstance(args, dict):
                    raise ValueError
            except ValueError:
                return self._send(400, {"error": {"code": "invalid", "message": "the body must be a JSON object"}})
            try:
                self._send(200, {"result": hub.call(urllib.parse.unquote(self.path[len("/v1/call/"):]), args)})
            except Fail as e:
                self._send(e.status, {"error": {"code": e.code, "message": e.message}})
            except Exception as e:  # noqa: BLE001 - one bad call must not take the hub down
                self._send(500, {"error": {"code": "internal", "message": f"{type(e).__name__}: {e}"}})

    srv = ThreadingHTTPServer((host, port), H)
    srv.daemon_threads = True
    addr = srv.server_address
    control = hub.home / "control.json"
    shown = "127.0.0.1" if host in ("0.0.0.0", "") else host
    tmp = control.with_suffix(".tmp")
    tmp.write_text(json.dumps({"url": f"http://{shown}:{addr[1]}", "token": hub.token, "pid": os.getpid(),
                               "version": __version__, "api": 1}, indent=1), encoding="utf-8")
    os.replace(tmp, control)
    print(f"abp-modkit {__version__}: {hub.spec.service.name} ({len(hub.spec.ops)} operations) on "
          f"http://{shown}:{addr[1]}", flush=True)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    if not WIN:
        signal.signal(signal.SIGTERM, lambda *_: stop.set())
    try:
        while not stop.wait(0.5):
            pass
    except KeyboardInterrupt:
        pass
    srv.shutdown()
    hub.shutdown()
    control.unlink(missing_ok=True)
