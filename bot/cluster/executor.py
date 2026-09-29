"""Running cluster jobs on this machine, inside what its owner offers.

A job arrives (from this machine's scheduler or a linked peer's) as a document:

    {"id", "kind", "spec", "req", "timeout_s", "env", "group"?, "task"?, "submitter": {"node"}, "hold"?}

accept() reserves its share of the offer (or refuses, with the reason) and records it; with hold=true it waits for
start() (gang jobs start together once every member is reserved), otherwise it starts at once.

Each job gets its own folder (<work_dir>/<id>/, results go in its out/ folder) and a clean environment: a short
allowlist of system variables plus the job's own, never ABP's (which holds API keys). On Windows it runs in a job
object with a hard CPU-rate cap (its share of the machine) and a memory cap; on Linux and macOS, under an address
space limit and a lower priority. A timeout, a cancel, or ABP stopping kills the whole process tree.

Kinds:
    command       spec: {argv: [...], cwd?: a folder inside the job folder, stdin?: text}
    python        spec: {code: "...", args?: [...]}  (run with ABP's Python in isolated mode)
    module_op     spec: {module, operation, args?}    (an operation of a module installed here)
    module_build  spec: {module}                      (build a module installed here)
    inference     spec: {model, messages, max_tokens?, temperature?}  (a model served here: Ollama for now)
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Optional

from bot.cluster import inventory, store
from bot.cluster.offer import Request, budget, work_root

NO_WINDOW = 0x08000000 if os.name == "nt" else 0
ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,100}$")
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,80}$")
SAFE_ENV = ("PATH", "PATHEXT", "SYSTEMROOT", "SystemRoot", "SYSTEMDRIVE", "WINDIR", "COMSPEC", "OS", "PROCESSOR_ARCHITECTURE",
            "PROCESSOR_IDENTIFIER", "NUMBER_OF_PROCESSORS", "USERPROFILE", "HOMEDRIVE", "HOMEPATH", "HOME", "USER",
            "USERNAME", "LOCALAPPDATA", "APPDATA", "PROGRAMDATA", "ProgramFiles", "ProgramFiles(x86)", "ProgramW6432",
            "CommonProgramFiles", "LANG", "LC_ALL", "TZ", "SHELL", "CARGO_HOME", "RUSTUP_HOME", "JAVA_HOME",
            "ANDROID_HOME", "ANDROID_SDK_ROOT", "GOPATH", "DOTNET_ROOT", "NODE_PATH", "XDG_RUNTIME_DIR")
MAX_TIMEOUT_S = 7 * 24 * 3600
_procs: dict[str, dict] = {}          # job id -> {"proc", "job_handle", "timer", "cancelled"}
_lock = threading.Lock()


class JobError(Exception):
    def __init__(self, message: str, code: str = "error", status: int = 400) -> None:
        super().__init__(message)
        self.code, self.status = code, status


def _job_dir(job_id: str) -> Path:
    return work_root() / job_id


def _clean_env(job: dict, d: Path) -> dict:
    env = {k: os.environ[k] for k in SAFE_ENV if k in os.environ}
    tmp = d / "tmp"
    tmp.mkdir(exist_ok=True)
    env.update(TEMP=str(tmp), TMP=str(tmp), TMPDIR=str(tmp), CLUSTER_JOB_ID=job["id"], CLUSTER_WORK_DIR=str(d),
               CLUSTER_OUT_DIR=str(d / "out"), CLUSTER_NODE=inventory.static()["hostname"],
               CLUSTER_CPU=str(job["reserved"]["cpu"]), CLUSTER_RAM_GB=str(job["reserved"]["ram_gb"]),
               PYTHONUNBUFFERED="1")
    if job["reserved"]["gpus"]:
        ids = ",".join(str(i) for i in job["reserved"]["gpus"])
        env.update(CLUSTER_GPUS=ids, CUDA_VISIBLE_DEVICES=ids, HIP_VISIBLE_DEVICES=ids, ROCR_VISIBLE_DEVICES=ids)
    g = job.get("group")
    if g:
        env.update(CLUSTER_GROUP_ID=str(g["id"]), CLUSTER_RANK=str(g["rank"]), CLUSTER_WORLD_SIZE=str(g["world"]),
                   CLUSTER_NODES=json.dumps(g.get("nodes") or []), RANK=str(g["rank"]), WORLD_SIZE=str(g["world"]),
                   MASTER_ADDR=str(g.get("master_addr") or ""), MASTER_PORT=str(g.get("master_port") or ""))
    t = job.get("task")
    if t:
        env.update(CLUSTER_TASK_INDEX=str(t["index"]), CLUSTER_TASK_COUNT=str(t["count"]))
    for k, v in (job.get("env") or {}).items():
        env[k] = str(v)
    return env


def _validate(job: dict) -> dict:
    jid = str(job.get("id") or "")
    if not ID_RE.match(jid):
        raise JobError("a job id is letters, digits, '.', '_' and '-' (up to 81 characters)")
    kind = str(job.get("kind") or "")
    spec = job.get("spec") if isinstance(job.get("spec"), dict) else {}
    if kind == "command":
        argv = spec.get("argv")
        if not isinstance(argv, list) or not argv or not all(isinstance(a, str) for a in argv):
            raise JobError("a command job needs spec.argv: a non-empty list of strings (no shell)")
    elif kind == "python":
        if not isinstance(spec.get("code"), str) or not spec["code"].strip():
            raise JobError("a python job needs spec.code")
    elif kind in ("module_op", "module_build"):
        if not spec.get("module"):
            raise JobError(f"a {kind} job needs spec.module")
        if kind == "module_op" and not spec.get("operation"):
            raise JobError("a module_op job needs spec.operation")
    elif kind == "inference":
        if not spec.get("model") or not isinstance(spec.get("messages"), list):
            raise JobError("an inference job needs spec.model and spec.messages")
    else:
        raise JobError(f"unknown job kind {kind!r} (command, python, module_op, module_build, inference)")
    env = job.get("env") or {}
    if not isinstance(env, dict) or not all(ENV_KEY_RE.match(str(k)) for k in env):
        raise JobError("env is a map of variable names to values")
    timeout = float(job.get("timeout_s") or 3600)
    if not 1 <= timeout <= MAX_TIMEOUT_S:
        raise JobError(f"timeout_s is 1-{MAX_TIMEOUT_S}")
    return {"id": jid, "kind": kind, "spec": spec, "req": Request.from_dict(job.get("req")).public(),
            "timeout_s": timeout, "env": {str(k): str(v) for k, v in env.items()}, "group": job.get("group"),
            "task": job.get("task"), "submitter": job.get("submitter") or {}, "hold": bool(job.get("hold"))}


def accept(job: dict, peer: Optional[str] = None) -> dict:
    """Reserve this job's share and record it (start it too, unless hold). Refusals raise JobError(code="refused")."""
    j = _validate(job)
    existing = store.get("runs", j["id"])
    if existing and existing["state"] not in ("refused",):
        return existing                                # the same job sent twice: idempotent
    ok, why, taken = budget.try_reserve(j["id"], Request.from_dict(j["req"]), peer, j["kind"])
    if not ok:
        raise JobError(why, code="refused", status=409)
    j.update(state="held" if j["hold"] else "accepted", reserved=taken, peer=peer, node=inventory.static()["hostname"])
    store.put("runs", j)
    try:
        from bot import db
        db.log_audit(actor=f"peer:{peer}" if peer else "cluster", action="cluster_job_accepted",
                     detail=f"{j['id']} {j['kind']} cpu={taken['cpu']} ram={taken['ram_gb']}GB gpus={taken['gpus']}"[:500])
    except Exception:  # noqa: BLE001
        pass
    if not j["hold"]:
        start(j["id"])
    return store.get("runs", j["id"]) or j


def start(job_id: str) -> dict:
    j = store.get("runs", job_id)
    if j is None:
        raise JobError(f"no job {job_id}", code="not_found", status=404)
    if j["state"] not in ("held", "accepted"):
        return j
    d = _job_dir(job_id)
    (d / "out").mkdir(parents=True, exist_ok=True)
    store.update("runs", job_id, state="starting", started=time.time(), work_dir=str(d))
    try:
        from bot import power
        power.keeper.hold(f"cluster-{job_id}", "a cluster job is running", minutes=j["timeout_s"] / 60 + 5, by="cluster")
    except Exception:  # noqa: BLE001
        pass
    threading.Thread(target=_run, args=(job_id,), name=f"cluster-job-{job_id}", daemon=True).start()
    return store.get("runs", job_id)


def _finish(job_id: str, state: str, **extra: Any) -> None:
    d = _job_dir(job_id)
    files = []
    out = d / "out"
    if out.is_dir():
        for p in sorted(out.rglob("*")):
            if p.is_file():
                files.append({"name": p.relative_to(out).as_posix(), "size": p.stat().st_size})
    with _lock:
        rec = _procs.pop(job_id, None)
    if rec and rec.get("cancelled") and state != "done":
        state = "cancelled"
    store.update("runs", job_id, state=state, finished=time.time(), files=files[:500], **extra)
    budget.release(job_id)
    try:
        from bot import power
        power.keeper.release(f"cluster-{job_id}")
    except Exception:  # noqa: BLE001
        pass


def _run(job_id: str) -> None:
    j = store.get("runs", job_id)
    if j is None:
        return
    try:
        if j["kind"] in ("command", "python"):
            _run_process(j)
        elif j["kind"] == "module_op":
            _run_inline(j, lambda: _module_op(j))
        elif j["kind"] == "module_build":
            _run_inline(j, lambda: _module_build(j))
        elif j["kind"] == "inference":
            _run_inline(j, lambda: _inference(j))
    except Exception as e:  # noqa: BLE001
        _log(job_id, f"[cluster] failed: {e}")
        _finish(job_id, "failed", error=str(e)[:2000])


def _log(job_id: str, line: str) -> None:
    d = _job_dir(job_id)
    d.mkdir(parents=True, exist_ok=True)
    with open(d / "job.log", "a", encoding="utf-8") as f:
        f.write(line.rstrip("\n") + "\n")


def _run_process(j: dict) -> None:
    d = _job_dir(j["id"])
    env = _clean_env(j, d)
    spec = j["spec"]
    if j["kind"] == "python":
        (d / "job.py").write_text(spec["code"], encoding="utf-8")
        argv = [sys.executable, "-I", str(d / "job.py"), *[str(a) for a in spec.get("args") or []]]
    else:
        argv = list(spec["argv"])
        exe = shutil.which(argv[0], path=env.get("PATH"))
        if exe is None and not Path(argv[0]).is_file():
            raise JobError(f"{argv[0]} is not installed on {inventory.static()['hostname']}")
        argv[0] = exe or argv[0]
        if os.name == "nt" and argv[0].lower().endswith((".cmd", ".bat")):
            argv = ["cmd", "/c", *argv]
    cwd = d
    if spec.get("cwd"):
        cwd = (d / str(spec["cwd"])).resolve()
        if d.resolve() not in cwd.parents and cwd != d.resolve():
            raise JobError("spec.cwd must be inside the job's folder")
        cwd.mkdir(parents=True, exist_ok=True)
    res = j["reserved"]
    threads = inventory.static()["cpu"]["threads"] or 1
    preexec = None
    if os.name != "nt":
        ram_bytes = int(res["ram_gb"] * 1024 ** 3)

        def preexec() -> None:  # noqa: F811
            import resource
            os.nice(10)
            if ram_bytes > 0:
                resource.setrlimit(resource.RLIMIT_AS, (ram_bytes, ram_bytes))
    _log(j["id"], f"[cluster] {' '.join(argv)}  (cpu {res['cpu']}, ram {res['ram_gb']} GB, gpus {res['gpus'] or '-'})")
    log = open(d / "job.log", "ab")
    try:
        proc = subprocess.Popen(argv, cwd=str(cwd), env=env, stdout=log, stderr=subprocess.STDOUT,
                                stdin=subprocess.PIPE if spec.get("stdin") else subprocess.DEVNULL,
                                creationflags=NO_WINDOW, preexec_fn=preexec)
    except OSError as e:
        log.close()
        raise JobError(f"could not start {argv[0]}: {e}") from None
    handle = 0
    if os.name == "nt":
        try:
            from bot.agent_runtime import win_job
            handle = win_job.create(job_memory_mb=int(res["ram_gb"] * 1024),
                                    cpu_rate_percent=min(100.0, res["cpu"] / threads * 100))
            win_job.assign(handle, proc.pid)
        except OSError as e:
            _log(j["id"], f"[cluster] warning: could not apply the CPU/memory caps: {e}")
    timer = threading.Timer(j["timeout_s"], lambda: _kill(j["id"], "timed out"))
    with _lock:
        _procs[j["id"]] = {"proc": proc, "job_handle": handle, "timer": timer, "cancelled": False}
    timer.start()
    store.update("runs", j["id"], state="running", pid=proc.pid)
    if spec.get("stdin"):
        try:
            proc.stdin.write(str(spec["stdin"]).encode("utf-8"))
            proc.stdin.close()
        except OSError:
            pass
    code = proc.wait()
    timer.cancel()
    log.close()
    with _lock:
        rec = _procs.get(j["id"]) or {}
        handle, rec["job_handle"] = rec.get("job_handle", 0), 0      # _kill may already have closed it
        why = rec.get("reason")
    if handle:
        from bot.agent_runtime import win_job
        win_job.terminate(handle, 0)
    _finish(j["id"], "done" if code == 0 else "failed", exit_code=code,
            **({"error": why} if why else {} if code == 0 else {"error": f"exit code {code}"}))


def _run_inline(j: dict, fn) -> None:
    store.update("runs", j["id"], state="running")
    box: dict[str, Any] = {}

    def go() -> None:
        try:
            box["result"] = fn()
        except Exception as e:  # noqa: BLE001
            box["error"] = str(e)
    t = threading.Thread(target=go, daemon=True)
    with _lock:
        _procs[j["id"]] = {"proc": None, "job_handle": 0, "timer": None, "cancelled": False}
    t.start()
    t.join(j["timeout_s"])
    if t.is_alive():
        _finish(j["id"], "failed", error=f"timed out after {j['timeout_s']:.0f}s")
    elif "error" in box:
        _log(j["id"], f"[cluster] {box['error']}")
        _finish(j["id"], "failed", error=box["error"][:2000])
    else:
        result = box.get("result")
        text = json.dumps(result, default=str)
        if len(text) > 200_000:
            (_job_dir(j["id"]) / "out" / "result.json").write_text(text, encoding="utf-8")
            result = {"note": "large result: see the file result.json"}
        _finish(j["id"], "done", result=result)


def _module_op(j: dict) -> Any:
    from bot.modules import harness
    s = j["spec"]
    _log(j["id"], f"[cluster] {s['module']}: {s['operation']}")
    return harness.call(str(s["module"]), str(s["operation"]), dict(s.get("args") or {}), timeout=j["timeout_s"])


def _module_build(j: dict) -> Any:
    from bot.modules import harness
    mid = str(j["spec"]["module"])
    started = harness.build(mid)
    seen = 0
    while True:
        # the module's own job, by id (adapter modules keep their jobs in their own harness; jobs() lists both)
        cur = next((x for x in harness.jobs(mid) if x.get("id") == started.get("id")), started)
        for line in (cur.get("log") or [])[seen:]:
            _log(j["id"], line)
        seen = len(cur.get("log") or [])
        if cur.get("state") != "running":
            if cur.get("state") == "failed":
                raise RuntimeError(cur.get("error") or "the build failed")
            return harness.install_info(mid)
        time.sleep(2)


def _inference(j: dict) -> Any:
    import httpx
    s = j["spec"]
    body = {"model": s["model"], "messages": s["messages"], "stream": False}
    for k in ("max_tokens", "temperature", "top_p"):
        if k in s:
            body[k] = s[k]
    _log(j["id"], f"[cluster] inference on {s['model']}")
    r = httpx.post("http://127.0.0.1:11434/v1/chat/completions", json=body, timeout=j["timeout_s"])
    if r.status_code >= 400:
        raise RuntimeError(f"the local model server answered {r.status_code}: {r.text[:500]}")
    return r.json()


def _kill(job_id: str, reason: str) -> None:
    with _lock:
        rec = _procs.get(job_id)
        if rec is None:
            return
        rec["reason"] = reason
        proc, handle = rec.get("proc"), rec.get("job_handle")
        rec["job_handle"] = 0
    _log(job_id, f"[cluster] {reason}")
    if handle:
        from bot.agent_runtime import win_job
        win_job.terminate(handle, 1)
    if proc is not None:
        try:
            import psutil
            p = psutil.Process(proc.pid)
            for c in p.children(recursive=True):
                c.kill()
            p.kill()
        except Exception:  # noqa: BLE001
            pass


def cancel(job_id: str) -> dict:
    j = store.get("runs", job_id)
    if j is None:
        raise JobError(f"no job {job_id}", code="not_found", status=404)
    if j["state"] in ("held", "accepted"):
        budget.release(job_id)
        return store.update("runs", job_id, state="cancelled", finished=time.time())
    with _lock:
        rec = _procs.get(job_id)
        if rec:
            rec["cancelled"] = True
    if rec:
        _kill(job_id, "cancelled")
    return store.get("runs", job_id)


def get(job_id: str) -> dict:
    j = store.get("runs", job_id)
    if j is None:
        raise JobError(f"no job {job_id}", code="not_found", status=404)
    return j


def logs(job_id: str, offset: int = 0, limit: int = 65536) -> dict:
    get(job_id)
    p = _job_dir(job_id) / "job.log"
    if not p.is_file():
        return {"text": "", "offset": 0, "size": 0}
    size = p.stat().st_size
    offset = max(0, min(int(offset), size))
    if offset == 0 and size > limit:
        offset = size - limit
    with open(p, "rb") as f:
        f.seek(offset)
        data = f.read(limit)
    return {"text": data.decode("utf-8", errors="replace"), "offset": offset + len(data), "size": size}


def file_path(job_id: str, name: str) -> Path:
    get(job_id)
    out = (_job_dir(job_id) / "out").resolve()
    p = (out / name).resolve()
    if out not in p.parents or not p.is_file():
        raise JobError(f"no file {name!r} in that job's results", code="not_found", status=404)
    return p


def cleanup(older_than_days: float = 7) -> int:
    """Delete the folders of jobs that finished more than older_than_days ago (their records stay)."""
    root = work_root()
    if not root.is_dir():
        return 0
    removed = 0
    cutoff = time.time() - older_than_days * 86400
    for d in root.iterdir():
        j = store.get("runs", d.name)
        if j and j["state"] not in store.ACTIVE and float(j.get("finished") or 0) < cutoff:
            shutil.rmtree(d, ignore_errors=True)
            removed += 1
    return removed
