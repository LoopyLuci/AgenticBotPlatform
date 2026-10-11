"""Placing cluster jobs: filter the nodes that could run a job, score them, and ask the best one to take it.

The chosen node has the final word: it reserves the job's share of its offer atomically, or refuses with the reason
(then the next node is asked). So any number of machines can schedule at once without double-booking anyone.

    submit(spec)          one job:   {kind, spec, req, timeout_s, env, retries, node?, prefer?, name?}
    submit_group(spec)    a gang:    the same, plus replicas: N members on N distinct nodes, all or nothing, each told
                          its rank, the world size, every member's address and MASTER_ADDR/MASTER_PORT
    submit_array(spec)    an array:  the same, plus count: N tasks (CLUSTER_TASK_INDEX), max_parallel at a time,
                          placed as capacity frees up; results gathered on the group

A job with retries left is placed again elsewhere when its node goes down or loses it. Job ids are the placement id
plus the attempt ("abc123-a1"), so a node never runs the same attempt twice.
"""
from __future__ import annotations

import asyncio
import re
import shutil
import time
import uuid
import zlib
from pathlib import Path
from typing import Any, Optional

from bot.cluster import executor, membership, store
from bot.cluster.offer import Request, work_root


class ScheduleError(Exception):
    def __init__(self, message: str, reasons: Optional[dict] = None) -> None:
        super().__init__(message)
        self.reasons = reasons or {}


TERMINAL = ("done", "failed", "cancelled", "lost", "refused")


# ---- choosing nodes ---------------------------------------------------------------------------------------------------
def _fits_reported(info: dict, req: Request, kind: str) -> tuple[bool, str]:
    """Whether a node's own report says the job fits (the node re-checks for real when asked)."""
    if not info:
        return False, "no report from this node yet"
    if not info.get("available"):
        return False, info.get("unavailable_reason") or "not sharing"
    o = info.get("offer") or {}
    if kind not in (o.get("kinds") or []):
        return False, f"does not run {kind} jobs"
    st = info.get("static") or {}
    if req.os and req.os != st.get("os"):
        return False, f"runs {st.get('os')}, not {req.os}"
    have = set(st.get("hypervisors") or []) | set(st.get("toolchains") or [])
    missing = [n for n in req.needs if n not in have and not (n == "whp-or-kvm" and have & {"whp", "kvm"})]
    if missing:
        return False, "lacks " + ", ".join(missing)
    if req.module and req.module not in {m["id"] for m in (info.get("software") or {}).get("modules") or []}:
        return False, f"does not have the {req.module} module"
    f = info.get("free") or {}
    if f.get("slots", 0) <= 0:
        return False, "has no free job slot"
    if req.cpu > f.get("cpu", 0) + 1e-9:
        return False, f"{f.get('cpu', 0)} CPU threads free of its offer"
    if req.ram_gb > f.get("ram_gb", 0) + 1e-9:
        return False, f"{f.get('ram_gb', 0)} GB RAM free of its offer"
    if req.disk_gb > f.get("disk_gb", 0) + 1e-9:
        return False, f"{f.get('disk_gb', 0)} GB disk free of its offer"
    if req.gpus:
        vram = {int(k): v for k, v in (f.get("vram_gb") or {}).items()}
        usable = [i for i in f.get("gpus") or [] if (vram.get(int(i)) or 0) >= req.vram_gb]
        if len(usable) < req.gpus:
            return False, f"{len(usable)} suitable GPU(s) free of its offer"
    return True, ""


def _distance_ms(node: dict) -> float:
    """How far away a node is, in milliseconds.

    TransferDaemon's route metrics are the measure when there is a route (bot/peers.py keeps each peer's
    overlay route on its node entry): it knows the RTT of the actual path — direct, relayed, or whatever the
    overlay picked — and how lossy it is, which the cluster's own round-trip measurement cannot see. Without
    a route it falls back to the heartbeat's measured latency, exactly as before.

    Loss is charged in retransmissions, not in percent: a route dropping a fifth of its packets costs several
    times its RTT once every lost packet has to be sent again, which is why a low-RTT lossy route loses to a
    slower clean one."""
    from bot.transferdaemon import overlay

    route = node.get("route") or {}
    rtt = route.get("rtt_ms")
    if rtt is None:
        return float(node.get("latency_ms") or 0)
    return float(rtt) * (1 + 4 * overlay.loss_fraction(route.get("loss")))


def _score(node: dict, req: Request) -> float:
    """Higher is better: the most room left after placing (spreads load), a little for being this machine (no data to
    move), a little against distance — which is TransferDaemon's route RTT where there is one."""
    info = node["info"] or {}
    f, cap = info.get("free") or {}, info.get("capacity") or {}
    cpu_left = (f.get("cpu", 0) - req.cpu) / max(cap.get("cpu", 0) or 1, 1e-6)
    ram_left = (f.get("ram_gb", 0) - req.ram_gb) / max(cap.get("ram_gb", 0) or 1, 1e-6)
    load = float((info.get("live") or {}).get("cpu_pct") or 0) / 100
    return cpu_left + ram_left - 0.5 * load + (0.15 if node["peer"] is None else 0) - _distance_ms(node) / 2000


def candidates(req: Request, kind: str, *, exclude: tuple = (), only: Optional[str] = None,
               service_group: Optional[str] = None) -> tuple[list[dict], dict]:
    """The nodes that could take this job, best first, and why each other node could not. `service_group`
    narrows the choice to the TransferDaemon nodes in that service group (the overlay's own grouping, for a
    gang that wants one specific set of machines)."""
    group_nodes = _group_nodes(service_group) if service_group else None
    fits, why_not = [], {}
    for n in membership.nodes():
        if n["name"] in exclude or (only and n["name"].lower() != only.lower()):
            continue
        if n["health"] not in ("ok",):
            why_not[n["name"]] = {"unsupported": "runs an ABP without the cluster layer (update it)",
                                  "unknown": "not heard from yet"}.get(n["health"], n["health"])
            continue
        if group_nodes is not None and not (n["peer"] is None or (n.get("route") or {}).get("td_node_id") in group_nodes):
            why_not[n["name"]] = f"not in the TransferDaemon group {service_group!r}"
            continue
        ok, why = _fits_reported(n["info"], req, kind)
        if ok:
            fits.append(n)
        else:
            why_not[n["name"]] = why
    fits.sort(key=lambda n: _score(n, req), reverse=True)
    return fits, why_not


def _group_nodes(service_group: str) -> set:
    """Which overlay node ids a TransferDaemon service group covers. Empty when the overlay cannot say, which
    leaves the caller with no candidates and the reason spelled out rather than a silent wrong placement."""
    from bot.transferdaemon import overlay

    try:
        return overlay.group_nodes(service_group)
    except Exception:  # noqa: BLE001
        return set()


# ---- talking to a node ------------------------------------------------------------------------------------------------
async def _node_call(node_name: Optional[str], method: str, path: str, body: Any = None, timeout: float = 60) -> Any:
    """A node's cluster API: this machine directly, a peer over whatever route currently reaches it
    (TransferDaemon's overlay endpoint when there is one, the advertised address otherwise — waking it if it
    sleeps)."""
    if node_name is None or node_name == membership.inventory.static()["hostname"]:
        return await asyncio.to_thread(_local, method, path, body)
    row = membership.peer_row(node_name)
    if row is None:
        raise ScheduleError(f"{node_name} is no longer a linked server")
    from bot import peers
    return await peers.proxy(row, method, "/api/cluster" + path, body, timeout=timeout)


_SEND_FILE_RE = re.compile(r"^/runs/([^/]+)/files/(.+)/send$")


def _local(method: str, path: str, body: Any) -> Any:
    """The node half of the cluster API, called in-process. The result file's own name may contain slashes
    ({name:path} on the route), so the send route is matched whole rather than split apart."""
    path = "/" + path.strip("/")
    send = _SEND_FILE_RE.match(path)
    if send and method == "POST":
        return executor.send_result(send.group(1), send.group(2), str((body or {}).get("to") or ""))
    parts = path.strip("/").split("/")
    if parts[0] != "runs":
        raise ScheduleError(f"unknown local route {path}")
    if len(parts) == 1 and method == "POST":
        return executor.accept(body, peer=None)
    rid = parts[1]
    if len(parts) == 2:
        return executor.get(rid)
    if parts[2] == "start":
        return executor.start(rid)
    if parts[2] == "cancel":
        return executor.cancel(rid)
    if parts[2].startswith("logs"):
        off = int((body or {}).get("offset", 0))
        return executor.logs(rid, off)
    raise ScheduleError(f"unknown local route {path}")


def _node_key(n: dict) -> Optional[str]:
    return None if n["peer"] is None else n["name"]


def _address(n: dict) -> str:
    st = ((n.get("info") or {}).get("static") or {}).get("addresses") or {}
    return (st.get("tailscale") or st.get("lan") or [""])[0]


# ---- one job ----------------------------------------------------------------------------------------------------------
def _run_doc(p: dict, attempt: int, *, hold: bool = False, group: Optional[dict] = None,
             task: Optional[dict] = None) -> dict:
    return {"id": f"{p['id']}-a{attempt}", "kind": p["kind"], "spec": p["spec"], "req": p["req"],
            "timeout_s": p["timeout_s"], "env": p.get("env") or {}, "hold": hold, "group": group, "task": task,
            "submitter": {"node": membership.inventory.static()["hostname"], "placement": p["id"]}}


async def _place(p: dict, *, exclude: tuple = (), hold: bool = False, group: Optional[dict] = None,
                 task: Optional[dict] = None, only: Optional[str] = None,
                 service_group: Optional[str] = None) -> dict:
    req = Request.from_dict(p["req"])
    fits, why_not = await asyncio.to_thread(candidates, req, p["kind"], exclude=exclude, only=only,
                                            service_group=service_group)
    if p.get("prefer"):
        fits.sort(key=lambda n: n["name"].lower() != str(p["prefer"]).lower())
    attempt = len(p.get("attempts") or []) + 1
    for n in fits:
        doc = _run_doc(p, attempt, hold=hold, group=group, task=task)
        try:
            run = await _node_call(_node_key(n), "POST", "/runs", doc, timeout=60)
        except Exception as e:  # noqa: BLE001  (a refusal or an unreachable node: ask the next one)
            why_not[n["name"]] = str(getattr(e, "args", [e])[0])[:300]
            continue
        p.setdefault("attempts", []).append({"node": n["name"], "peer": n["peer"], "run_id": doc["id"], "at": time.time()})
        p.update(node=n["name"], peer=n["peer"], run_id=doc["id"], state=run.get("state", "accepted"), run=run)
        store.put("placements", p)
        return p
    raise ScheduleError("no node can take this job right now", why_not)


def _placement(spec: dict, *, name: str = "", group_id: Optional[str] = None) -> dict:
    kind = str(spec.get("kind") or "")
    probe = {**spec, "id": "probe"}
    executor._validate(probe)                       # the same checks a node makes, before asking any node
    return {"id": uuid.uuid4().hex[:12], "name": name or spec.get("name") or kind, "kind": kind,
            "spec": spec.get("spec") or {}, "req": Request.from_dict(spec.get("req")).public(),
            "timeout_s": float(spec.get("timeout_s") or 3600), "env": spec.get("env") or {},
            "retries": int(spec.get("retries") or 0), "prefer": spec.get("prefer"), "state": "placing",
            "group_id": group_id, "service_group": spec.get("service_group"),
            "created": time.time()}


async def submit(spec: dict) -> dict:
    p = _placement(spec)
    try:
        return await _place(p, only=spec.get("node"))
    except ScheduleError as e:
        p.update(state="refused", error=str(e), reasons=e.reasons)
        store.put("placements", p)
        raise


async def refresh(pid: str) -> dict:
    p = store.get("placements", pid)
    if p is None:
        raise ScheduleError(f"no job {pid}")
    if p.get("state") in TERMINAL or not p.get("run_id"):
        return p
    try:
        run = await _node_call(p.get("peer"), "GET", f"/runs/{p['run_id']}", timeout=20)
        p.update(state=run.get("state"), run=run, node_error=None)
    except Exception as e:  # noqa: BLE001
        p["node_error"] = str(e)[:300]
    return store.put("placements", p)


async def cancel(pid: str) -> dict:
    p = store.get("placements", pid)
    if p is None:
        raise ScheduleError(f"no job {pid}")
    if p.get("run_id") and p.get("state") not in TERMINAL:
        try:
            await _node_call(p.get("peer"), "POST", f"/runs/{p['run_id']}/cancel", {}, timeout=30)
        except Exception as e:  # noqa: BLE001
            p["node_error"] = str(e)[:300]
    p["state"] = "cancelled"
    p["retries"] = 0
    return store.put("placements", p)


async def logs(pid: str, offset: int = 0) -> dict:
    p = store.get("placements", pid)
    if p is None or not p.get("run_id"):
        raise ScheduleError(f"no job {pid}")
    if p.get("peer") is None:
        return await asyncio.to_thread(executor.logs, p["run_id"], offset)
    return await _node_call(p["peer"], "GET", f"/runs/{p['run_id']}/logs?offset={int(offset)}", timeout=30)


# ---- results big enough to want TransferDaemon --------------------------------------------------------------------------
# A result file over this size does not come back as an HTTP body: it is handed to TransferDaemon's own
# transfer, which is what the platform is for (chunked, resumable, checked end to end). Smaller files stay on
# the plain HTTP route, where setting up a transfer would cost more than it saves.
BIG_RESULT_BYTES = 8 * 1024 * 1024


async def collect(pid: str, *, min_bytes: int = BIG_RESULT_BYTES, wait_s: float = 600.0) -> dict:
    """Fetch a finished remote job's big result files with TransferDaemon instead of over the peer link.

    The node holding the file is what sends it (executor.send_result): TransferDaemon's transfer is a send,
    not a fetch, so the peer is asked to push the bytes to this machine's overlay node id. Each file is
    copied out of TransferDaemon's own downloads folder into this job's <work>/fetched/, and the placement
    records where it landed. Anything that goes wrong is reported per file rather than aborting the rest."""
    from bot.transferdaemon import overlay

    p = store.get("placements", pid)
    if p is None or not p.get("run_id"):
        raise ScheduleError(f"no job {pid}")
    files = [f for f in (p.get("run") or {}).get("files") or [] if int(f.get("size") or 0) >= min_bytes]
    if not files:
        return {"job": pid, "files": [], "errors": [],
                "note": f"no result file is {min_bytes} bytes or bigger, so none needs a file transfer"}
    if not p.get("peer"):
        return {"job": pid, "files": [], "errors": [],
                "note": "this job ran on this machine, so its results are already here"}
    me = overlay.node_id()
    if not me:
        return {"job": pid, "files": [], "errors": [],
                "note": "this machine has no TransferDaemon overlay identity, so there is nothing to send to"}
    dest = work_root() / p["run_id"] / "fetched"
    dest.mkdir(parents=True, exist_ok=True)
    fetched, errors = [], []
    for f in files:
        name = str(f["name"])
        try:
            sent = await _node_call(p["peer"], "POST", f"/runs/{p['run_id']}/files/{name}/send", {"to": me}, timeout=600)
        except Exception as e:  # noqa: BLE001
            errors.append({"name": name, "error": str(e)[:300]})
            continue
        target = dest / Path(str(f["name"])).name
        got = await asyncio.to_thread(overlay.await_download, target.name, wait_s)
        if not got:
            errors.append({"name": name, "error": "TransferDaemon reported no completed transfer for it",
                           "transfer": sent})
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(got, target)
        fetched.append({"name": name, "size": f.get("size"), "path": str(target), "transfer": sent})
    store.put("placements", {**store.get("placements", pid), "fetched": fetched, "fetch_errors": errors})
    return {"job": pid, "files": fetched, "errors": errors}


async def wait(pid: str, timeout_s: float = 600, poll_s: float = 2) -> dict:
    deadline = time.time() + timeout_s
    while True:
        p = await refresh(pid)
        if p.get("state") in TERMINAL or time.time() > deadline:
            return p
        await asyncio.sleep(poll_s)


# ---- gangs ------------------------------------------------------------------------------------------------------------
async def submit_group(spec: dict) -> dict:
    """N members on N distinct nodes, reserved first (held), then started together. All or nothing."""
    n = int(spec.get("replicas") or 0)
    if not 1 <= n <= 256:
        raise ScheduleError("replicas is 1-256")
    gid = uuid.uuid4().hex[:12]
    req = Request.from_dict(spec.get("req"))
    fits, why_not = await asyncio.to_thread(candidates, req, str(spec.get("kind")),
                                            service_group=spec.get("service_group"))
    if len(fits) < n:
        raise ScheduleError(f"{n} nodes are needed and {len(fits)} can take a member now", why_not)
    chosen = fits[:n]
    master_port = 29500 + zlib.crc32(gid.encode()) % 1000
    nodes = [{"rank": i, "node": c["name"], "address": _address(c)} for i, c in enumerate(chosen)]
    group = {"id": gid, "kind": "gang", "name": spec.get("name") or "gang", "state": "reserving", "replicas": n,
             "members": [], "created": time.time()}
    store.put("groups", group)
    held: list[dict] = []
    for i, c in enumerate(chosen):
        p = _placement(spec, name=f"{group['name']}[{i}]", group_id=gid)
        info = {"id": gid, "rank": i, "world": n, "nodes": nodes, "master_addr": nodes[0]["address"],
                "master_port": master_port}
        try:
            held.append(await _place(p, hold=True, group=info, only=c["name"]))
        except ScheduleError as e:
            for h in held:
                await cancel(h["id"])
            group.update(state="refused", error=f"{c['name']} refused its member: {e.reasons or e}")
            store.put("groups", group)
            raise ScheduleError(f"the gang could not be reserved: {c['name']} refused", e.reasons) from None
    for h in held:
        try:
            await _node_call(h.get("peer"), "POST", f"/runs/{h['run_id']}/start", {}, timeout=60)
        except Exception as e:  # noqa: BLE001
            h["node_error"] = str(e)[:300]
            store.put("placements", h)
    group.update(state="running", members=[h["id"] for h in held], master=nodes[0], master_port=master_port)
    return store.put("groups", group)


# ---- arrays -----------------------------------------------------------------------------------------------------------
async def submit_array(spec: dict) -> dict:
    count = int(spec.get("count") or 0)
    if not 1 <= count <= 10_000:
        raise ScheduleError("count is 1-10000")
    _placement(spec)                                  # validate once
    group = {"id": uuid.uuid4().hex[:12], "kind": "array", "name": spec.get("name") or "array", "state": "running",
             "count": count, "max_parallel": int(spec.get("max_parallel") or count), "spec": spec,
             "pending": list(range(count)), "members": {}, "created": time.time()}
    store.put("groups", group)
    await _dispatch_array(group)
    return store.get("groups", group["id"])


async def _dispatch_array(group: dict) -> None:
    active = 0
    for pid in list(group["members"].values()):
        p = await refresh(pid)
        if p.get("state") not in TERMINAL:
            active += 1
    while group["pending"] and active < group["max_parallel"]:
        idx = group["pending"][0]
        p = _placement(group["spec"], name=f"{group['name']}[{idx}]", group_id=group["id"])
        try:
            await _place(p, task={"index": idx, "count": group["count"]})
        except ScheduleError:
            break                                     # no room right now: the next supervision pass tries again
        group["pending"].pop(0)
        group["members"][str(idx)] = p["id"]
        active += 1
    states = [(store.get("placements", pid) or {}).get("state") for pid in group["members"].values()]
    if not group["pending"] and all(s in TERMINAL for s in states):
        group["state"] = "done" if all(s == "done" for s in states) else "finished with failures"
        group["finished"] = time.time()
    store.put("groups", group)


def group_results(gid: str) -> dict:
    g = store.get("groups", gid)
    if g is None:
        raise ScheduleError(f"no group {gid}")
    ids = g["members"].values() if isinstance(g["members"], dict) else g["members"]
    members = [store.get("placements", pid) or {"id": pid} for pid in ids]
    return {**{k: v for k, v in g.items() if k != "spec"},
            "results": [{"job": m["id"], "name": m.get("name"), "node": m.get("node"), "state": m.get("state"),
                         "exit_code": (m.get("run") or {}).get("exit_code"), "result": (m.get("run") or {}).get("result"),
                         "error": (m.get("run") or {}).get("error") or m.get("error")} for m in members]}


# ---- supervision (called by the heartbeat) ----------------------------------------------------------------------------
async def supervise_once() -> None:
    """Refresh running jobs; place again those whose node lost them or went down (when they have retries left);
    keep arrays moving."""
    health = {n["name"]: n["health"] for n in await asyncio.to_thread(membership.nodes)}  # probes module hubs
    for p in store.list_("placements", states=("accepted", "held", "starting", "running", "placing"), limit=500):
        p = await refresh(p["id"])
        node_down = p.get("peer") and health.get(p.get("node")) == "down"
        if (p.get("state") == "lost" or node_down) and int(p.get("retries") or 0) > 0 and p.get("kind") != "module_op":
            p["retries"] = int(p["retries"]) - 1
            p["previous"] = (p.get("previous") or []) + [{"node": p.get("node"), "state": p.get("state")}]
            try:
                await _place(p, exclude=(p.get("node"),))
            except ScheduleError as e:
                p.update(state="lost", error=f"its node went down and no other node can take it: {e.reasons}")
                store.put("placements", p)
        elif node_down:
            p.update(state="lost", error=f"{p.get('node')} went down while running it")
            store.put("placements", p)
    for g in store.list_("groups", states=("running",), limit=100):
        if g["kind"] == "array":
            await _dispatch_array(g)
