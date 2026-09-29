"""The agent's cluster tools. Reading is free; running work on the cluster asks first. The agent can never change
what a machine offers: that is its owner's choice, on the Cluster page.

    cluster_status   every node: health, hardware (CPU, RAM, GPUs, disks), what it offers, what is free, load
    cluster_run      run a job on the best node that fits (command, python, module_op, module_build, inference),
                     optionally waiting for it to finish
    cluster_group    a gang (replicas on distinct nodes, with rank / world size / MASTER_ADDR) or an array (count
                     tasks spread over the nodes)
    cluster_jobs     list jobs, show one (with its result), follow its log
    cluster_cancel   cancel a job
"""
from __future__ import annotations

import json
from typing import Any

from bot.cluster import membership, scheduler, store
from bot.cluster.executor import JobError
from bot.cluster.scheduler import ScheduleError

MAX_OUT = 12_000
REQ = {"type": "object", "description": "what the job needs: cpu (threads), ram_gb, gpus, vram_gb (per GPU), disk_gb, "
                                        "os (windows/linux/macos), needs (e.g. [cargo] or [kvm]), module (installed "
                                        "there)",
       "properties": {"cpu": {"type": "number"}, "ram_gb": {"type": "number"}, "gpus": {"type": "integer"},
                      "vram_gb": {"type": "number"}, "disk_gb": {"type": "number"}, "os": {"type": "string"},
                      "needs": {"type": "array", "items": {"type": "string"}}, "module": {"type": "string"}}}
SPEC = {"type": "object", "description": "by kind: command {argv: [...], cwd?, stdin?}; python {code, args?}; "
                                         "module_op {module, operation, args?}; module_build {module}; "
                                         "inference {model, messages, max_tokens?}. Results written to $CLUSTER_OUT_DIR "
                                         "come back as files."}
KIND = {"type": "string", "enum": ["command", "python", "module_op", "module_build", "inference"]}


def _out(value: Any) -> str:
    text = value if isinstance(value, str) else json.dumps(value, indent=1, default=str)
    return text if len(text) <= MAX_OUT else text[:MAX_OUT] + f"\n... ({len(text) - MAX_OUT} more characters)"


def _node_summary(n: dict) -> dict:
    info = n.get("info") or {}
    st, live = info.get("static") or {}, info.get("live") or {}
    return {"name": n["name"], "this_machine": n["peer"] is None, "health": n["health"], "latency_ms": n.get("latency_ms"),
            "os": st.get("os"), "cpu": (st.get("cpu") or {}).get("model"), "threads": (st.get("cpu") or {}).get("threads"),
            "ram_gb": st.get("ram_gb"), "gpus": [f"{g.get('name')} ({g.get('vram_gb')} GB)" for g in st.get("gpus") or []],
            "cpu_load_pct": live.get("cpu_pct"), "sharing": info.get("available"),
            "not_sharing_because": info.get("unavailable_reason") or None, "offer_kinds": (info.get("offer") or {}).get("kinds"),
            "free": info.get("free"), "jobs": info.get("jobs"), "models": (info.get("software") or {}).get("models"),
            "modules": [m["id"] for m in (info.get("software") or {}).get("modules") or []], "error": n.get("error")}


def _job_summary(p: dict) -> dict:
    run = p.get("run") or {}
    return {"id": p["id"], "name": p.get("name"), "kind": p.get("kind"), "node": p.get("node"), "state": p.get("state"),
            "exit_code": run.get("exit_code"), "result": run.get("result"), "files": run.get("files"),
            "error": run.get("error") or p.get("error"), "reasons": p.get("reasons"), "retries_left": p.get("retries")}


def _enabled() -> bool:
    return True


def register_tools() -> None:
    from bot.agent_runtime import toolspec

    def reg(name, description, props, required, handler, *, permission, read_only):
        toolspec.register(
            {"name": name, "description": description,
             "input_schema": {"type": "object", "properties": props, "required": required}},
            toolspec.ToolSpec(name, permission, read_only=read_only, concurrency_safe=read_only, origin="registered"),
            handler, enabled=_enabled)

    async def status(inp, **_):
        if inp.get("refresh"):
            await membership.poll_all()
        return _out([_node_summary(n) for n in membership.nodes()])

    async def run(inp, **_):
        spec = {k: inp.get(k) for k in ("kind", "spec", "req", "timeout_s", "env", "retries", "node", "prefer", "name")
                if inp.get(k) is not None}
        try:
            p = await scheduler.submit(spec)
            if inp.get("wait"):
                p = await scheduler.wait(p["id"], timeout_s=float(inp.get("wait_s") or 600))
                tail = (await scheduler.logs(p["id"])).get("text", "")[-3000:]
                return _out({**_job_summary(p), "log_tail": tail})
            return _out(_job_summary(p))
        except (ScheduleError, JobError) as e:
            return _out({"error": str(e), "why_each_node_cannot": getattr(e, "reasons", None)})

    async def group(inp, **_):
        spec = {k: inp.get(k) for k in ("kind", "spec", "req", "timeout_s", "env", "name", "replicas", "count",
                                         "max_parallel") if inp.get(k) is not None}
        if not spec.get("replicas") and not spec.get("count"):
            return "Error: give replicas (a gang) or count (an array)"
        try:
            g = await (scheduler.submit_group(spec) if spec.get("replicas") else scheduler.submit_array(spec))
            return _out({k: v for k, v in g.items() if k != "spec"})
        except (ScheduleError, JobError) as e:
            return _out({"error": str(e), "why_each_node_cannot": getattr(e, "reasons", None)})

    async def jobs(inp, **_):
        action = str(inp.get("action") or "list")
        try:
            if action == "list":
                return _out([_job_summary(p) for p in store.list_("placements", limit=int(inp.get("limit") or 20))])
            if action == "groups":
                return _out([{k: v for k, v in g.items() if k != "spec"} for g in store.list_("groups", limit=20)])
            if action == "group":
                return _out(scheduler.group_results(str(inp.get("id") or "")))
            jid = str(inp.get("id") or "")
            if action == "show":
                return _out(_job_summary(await scheduler.refresh(jid)))
            if action == "logs":
                return _out((await scheduler.logs(jid, int(inp.get("offset") or 0))).get("text", ""))
        except (ScheduleError, JobError) as e:
            return f"Error: {e}"
        return "Error: action is list, show, logs, groups or group"

    async def cancel(inp, **_):
        try:
            return _out(_job_summary(await scheduler.cancel(str(inp.get("id") or ""))))
        except (ScheduleError, JobError) as e:
            return f"Error: {e}"

    reg("cluster_status", "The cluster: this machine and every linked ABP server as nodes. For each: health, OS, CPU "
        "(model, threads), RAM, GPUs with memory, load, whether it shares resources and what it offers (job kinds, "
        "free CPU/RAM/GPUs/disk), its jobs, local models and modules. refresh=true asks every node now.",
        {"refresh": {"type": "boolean"}}, [], status, permission="read", read_only=True)
    reg("cluster_run", "Run one job on the cluster: the best node that fits its needs (and shares them) takes it, "
        "with hard CPU and memory caps and its own folder. kind: command (argv, no shell), python (code), module_op, "
        "module_build, inference (a model on that node). node pins it to a node; prefer favours one; retries places "
        "it again elsewhere if its node goes down. wait=true waits for it and returns its result and log tail.",
        {"kind": KIND, "spec": SPEC, "req": REQ, "timeout_s": {"type": "number"}, "env": {"type": "object"},
         "retries": {"type": "integer"}, "node": {"type": "string"}, "prefer": {"type": "string"},
         "name": {"type": "string"}, "wait": {"type": "boolean"}, "wait_s": {"type": "number"}},
        ["kind", "spec"], run, permission="external", read_only=False)
    reg("cluster_group", "Run work across several nodes at once. replicas=N: a gang of N members on N distinct nodes, "
        "reserved all-or-nothing and started together; each gets CLUSTER_RANK/RANK, CLUSTER_WORLD_SIZE/WORLD_SIZE, "
        "CLUSTER_NODES (every member's address) and MASTER_ADDR/MASTER_PORT (for torch.distributed and the like). "
        "count=N: an array of N tasks (CLUSTER_TASK_INDEX) spread over the nodes, max_parallel at a time, results "
        "gathered (see cluster_jobs action=group).",
        {"kind": KIND, "spec": SPEC, "req": REQ, "replicas": {"type": "integer"}, "count": {"type": "integer"},
         "max_parallel": {"type": "integer"}, "timeout_s": {"type": "number"}, "env": {"type": "object"},
         "name": {"type": "string"}}, ["kind", "spec"], group, permission="external", read_only=False)
    reg("cluster_cancel", "Cancel a cluster job (id from cluster_run or cluster_jobs): its processes are stopped on "
        "whichever node runs it.", {"id": {"type": "string"}}, ["id"], cancel, permission="external", read_only=False)
    reg("cluster_jobs", "Cluster jobs submitted from here. action: list, show (id: state, node, exit code, result, "
        "files), logs (id, offset: its output), groups (gangs and arrays), group (id: every member's result).",
        {"action": {"type": "string", "enum": ["list", "show", "logs", "groups", "group"]},
                     "id": {"type": "string"}, "offset": {"type": "integer"}, "limit": {"type": "integer"}},
        [], jobs, permission="read", read_only=True)


register_tools()
