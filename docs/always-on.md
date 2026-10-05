# ABP is never offline

`abp_gate` is one small process that owns port **8787** — the port every other
program, phone and agent already talks to — and reverse-proxies to whichever
ABP instance is currently active. Behind it, code can be replaced while the
port never goes quiet, and every agent that needs its own ABP gets one, on its
own state, without touching the running one.

```text
   everything else ──▶ 8787 (abp_gate) ──┬──▶ instance "prod"     real data, holds the lease
   the desktop app ──▶ 8788 (control)    ├──▶ instance "swap-…"   new code, pre-warmed
                                        └──▶ instance "agent-x"  a copy of the data, sandboxed
```

Two commands cover almost everything:

```bash
abp gate start          # the gate, windowless and detached - it outlives this terminal
abp gate status         # what is running, where traffic goes, what the gate has given up on
```

## The pieces

| file                 | what it is                                                              |
|----------------------|-------------------------------------------------------------------------|
| `abp_gate/paths.py`  | where files go and which ports are owned                                |
| `abp_gate/limits.py` | the numbers that bound what the gate may do to this machine             |
| `abp_gate/registry.py` | the one JSON file that says what exists (names, ports, pids, health)  |
| `abp_gate/procs.py`  | windowless start, job-per-instance tree stop, health probes, log tails   |
| `abp_gate/proxy.py`  | the ASGI reverse proxy: HTTP (streaming/SSE/chunked) and WebSockets      |
| `abp_gate/manager.py`| the operations: start, swap, rollback, sandbox, stop                    |
| `abp_gate/control.py`| the control API, authenticated with the same `DASHBOARD_TOKEN`         |
| `bot/lease.py`       | the leader lease that makes sure singletons run exactly once            |

`abp_gate` imports no part of the ABP runtime. Two late, deliberate exceptions:
the dashboard token (a `.env` read) and `bot/agent_runtime/win_job.py` (stdlib
`ctypes` only). That is what lets it keep serving while the code it fronts is
being replaced underneath it.

## The commands

### The gate

```bash
abp gate start [--no-start] [--foreground] [--instance-lifetime gate|detached]
abp gate status
abp gate stop                     # stops every instance, then the gate itself
```

`--foreground` runs the same daemon attached to the terminal, which is how you
watch it. `abp gate stop` is the graceful exit; it is also what the dashboard's
**Restart** uses.

### Instances

```bash
abp instance list
abp instance swap <code-root> [--name N] [--data-root P]   # zero-downtime, auto-rollback
abp instance rollback                                     # back to what the last swap replaced
abp instance sandbox <code-root> [--name N] [--keep-state]
abp instance stop <name> [--forget-state]
abp instance logs <name> [--lines N] [--follow]
```

### For an agent working on ABP itself

```bash
abp dev up                       # a git worktree + a sandboxed ABP running on it
abp dev status
abp dev down --name <name>
```

`dev up` prints the `ABP_URL` and the **name** of the variable holding the token
(never the token itself) to point a client or a test at it. The sandbox runs
with `ABP_SANDBOX_INSTANCE=1`: its state is a *copy*, it never holds the leader
lease, and no outward connector is ever started - see
[What a sandbox may not do](#what-a-sandbox-may-not-do).

## How a swap works

1. the new instance starts on the **same data** as a standby, so it holds zero
   singletons while it boots;
2. it answers `/healthz`;
3. the outgoing instance is told to release the leader lease (it stops its
   pollers, its scheduler, the rest — and keeps serving);
4. the incoming one takes the lease, starts those services, and then has to
   answer `/healthz` **again** — taking the lease is what makes it busy, and
   routing the front door into an ABP that cannot answer for the next few
   seconds is the outage the swap exists to prevent. The outgoing instance is
   still serving throughout, so this waits for free;
5. routing flips — one pointer, so the next request goes to the new instance and
   nothing is closed underneath anybody;
6. the old instance is drained and stopped, whole tree and all.

A failure at 1, 3 or 4 leaves the outgoing instance serving, untouched. A failure
at 4 is the interesting one: the new code is healthy but cannot lead, so the
rollback is "tell the outgoing instance to take the lease again" — a few seconds
of services and no downtime. The reason is reported either way.

## The leader lease

Only the lease holder runs the services that must not run twice on one data
root: platform pollers (one bot token, one poller), the scheduler, the Support
Bot warm-up, config watchers, hot reload, memory/retention jobs, and the rest —
`GET /api/lease` on any instance lists them (`gated_services`).

`bot/main.py --standby` (or `ABP_STANDBY=1`) serves the API without ever taking
the lease, which is what a swap target and a fresh sandbox are.

## What a sandbox may not do

`ABP_SANDBOX_INSTANCE=1` blocks outward connectors whatever its config says,
because a sandbox's config is a *copy* of the real one and therefore carries the
**real** bot tokens — two pollers on one token breaks the real bot:

- no platform pollers are started (`platform_pollers`);
- no scheduled job fires (`scheduler`);
- `bot/outbox.py` refuses every send (`outbox_send`);
- module hubs are not auto-started (`module_hubs`).

`GET /api/lease` says so on the instance itself (`outward_blocked`,
`sandbox_blocked`).

## What the gate will not do to your machine

This is the part that matters most, and it exists because the first version of
the gate did the opposite and filled this machine with six thousand orphaned
`python.exe` processes in one test run.

**An instance is more than one process.** A venv's `python.exe` is a *launcher*:
on Windows it starts the real interpreter as a child and waits for it. Signalling
the launcher leaves the interpreter running, holding the instance's port and its
database open. So:

- every instance gets a **Windows job object**
  (`bot/agent_runtime/win_job.py`, `KILL_ON_JOB_CLOSE`). It holds the launcher,
  the interpreter and everything either of them starts afterwards;
- every replacement **kills the old tree first**, then starts the new one —
  never "starts, then stops";
- `procs.stop()` is the backstop: psutil tree-kill, then `taskkill /F /T`.

**A dead gate takes its instances with it.** The gate holds the job handles, so
its death closes them:

| gate lifetime | what survives the gate being killed                                     |
|---------------|--------------------------------------------------------------------------|
| `gate`        | nothing. Every instance is in a kill-on-close job. The default.          |
| `detached`    | the **active** instance only — deliberately started outside a job, so the next gate re-adopts it from the registry and ABP is up in seconds. Standbys and sandboxes still die. |

`abp gate start` asks for `detached`; `python -m abp_gate` defaults to `gate`.

**The watcher cannot start processes forever.** Every restart of one instance is
counted, in the registry, so the budget survives a restart of the gate itself:

- an instance that **never answered `/healthz` is never restarted** — a build
  that cannot start is not a process that crashed;
- **a crash and a slow answer are not the same question.** "The process is gone"
  is the OS's answer and the gate acts on it at once. "It did not answer a probe
  within three seconds" is answered "no" by a busy instance just as readily as
  by a sick one — a booting ABP stalls its own `/healthz` for 2.5–3.0s while it
  starts its lease-gated services (measured here with the machine deliberately
  loaded), which is the edge of the probe timeout on a quiet day and past it on a
  busy one. So an instance that is **running but unanswerable** has to stay that
  way for `ABP_GATE_UNHEALTHY_GRACE_S` (30s) before it counts as replaceable, and
  the gate says so in its log while it waits. Replacing a working instance over
  one slow probe would turn a three-second hiccup into a thirty-second outage, and
  hand every reader of the registry a pid that stops meaning anything;
- at most **3 restarts in 10 minutes** (`ABP_GATE_RESTART_LIMIT`,
  `ABP_GATE_RESTART_WINDOW_S`), with an exponential backoff between them
  (`ABP_GATE_RESTART_BACKOFF_S`, default 5s, 20s, 60s…);
- each restart is at most 3 spawns, each stopped tree-first before the next, so
  the worst case in a window is nine processes;
- then the gate **stops**, marks the instance `failed`, drops routing (the public
  port answers 503 rather than proxying to a corpse) and says so in
  `abp gate status`. Restarting it again is a decision somebody has to make.
- **the same budget covers "there is no instance at all".** A gate whose own
  first start failed retries it, because a boot that lost a race for a database
  is worth another go — and stops after the same three tries, saying so in its
  log. A gate started with `--no-start` never starts anything, watcher included.
  That also means stopping the active instance through `abp instance stop` gets
  you a fresh one a moment later: keeping ABP down is `abp gate stop`.

**A ceiling on how many instances can exist.** `ABP_GATE_MAX_INSTANCES` (8 by
default); a start, swap or sandbox beyond it is refused with a reason. A bug
that starts instances without stopping them becomes a refusal instead of an
out-of-memory machine.

**Nothing slow ever runs on the loop that serves the public port.** Starting a
process, stopping a tree, waiting for a port to close, probing an instance's
`/healthz` and copying the database into a sandbox all wait on the operating
system, and every one of them happens while the gate is serving traffic. All of
it runs in threads, because the whole point of the gate is that a swap, a
restart or a status page never makes the front door go quiet — a stall measured
at 2.5s before this, against a budget of nothing at all.

Every knob is in `abp_gate/limits.py`, and every one of them is an environment
variable, so a test can run the same code with a small budget.

## The desktop app

If something is already answering 8787 — the gate, or a `bot.main` somebody
else left running — the app **attaches** to it instead of starting a second ABP
that could only fail to bind. When the thing it attached to is the gate, it says
so in the log panel, and closing the window does not stop it: closing the app
must not take ABP offline.

## What is *not* covered

- **Only one gate per data root.** It owns 8787/8788 in the name of one
  `ABP_HOME`; a second gate with a different `ABP_HOME` is a second front door on
  the same port, which the second one will lose.
- **The gate does not manage the desktop app's bundled venv**, and does not
  install, upgrade or migrate anything.
- **A swap is not a database migration.** Both instances run the same schema
  code path; a release needing a real migration is a different piece of work.
- **`rollback` only works while the previous instance is still alive.** A swap
  stops what it replaced (that is what makes it a clean swap), so an explicit
  rollback afterwards correctly refuses and says so rather than silently
  re-running the newest code.
- **Sandboxes get a copy of the state, not a shared view of it.** A write made
  through a sandbox never reaches the real data, by design; bringing one back is
  a deliberate operation, not automatic.
- **No Windows service / launchd unit.** "Always on" means a detached,
  windowless process that survives closing the terminal; it does not survive a
  reboot on its own.

## Where to look when something is wrong

```bash
abp gate status                     # limits, restart budget, per-instance errors
abp instance list                   # role, port, pid, health
abp instance logs <name> --follow   # that instance's own stdout/stderr
```

Files, under `ABP_INSTANCES_DIR` (default `<data root>/instances`):

```text
instances/gate/registry.json    every instance: name, code root, data root, port, pid, role, health
instances/gate/gate.log         the gate's own log
instances/gate/gate.json        the gate's pid and the ports it owns (no secrets)
instances/<name>.log            that instance's stdout/stderr
instances/<name>/               a sandbox's copied state
```

The control API (8788) answers `GET /api/gate`, `GET /api/instance`,
`GET /api/instance/<name>/logs`, `GET /api/instance/<name>/lease`, and an
unauthenticated `GET /healthz` for supervisors. Everything that changes what is
running (`/api/gate/start`, `/api/gate/stop`, `/api/instance/swap`,
`/api/instance/rollback`, `/api/instance/sandbox`, `/api/instance/stop`)
requires `X-Dashboard-Token`, the same token the dashboard uses. The token is
never logged, never echoed in a response, and never written to the registry.