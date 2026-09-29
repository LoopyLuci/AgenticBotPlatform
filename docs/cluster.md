# The ABP cluster

Every ABP machine you link (Peers page) is a node of one cluster. Each node:
- reports its hardware and live load: CPU, RAM, GPUs and their memory, disks, hypervisors, toolchains, modules,
  local models;
- shares exactly what its owner offers, and nothing until the owner turns sharing on;
- runs jobs for the others, inside hard limits.

The design and the phases still to come are in [modules/ROADMAP.md §5a](modules/ROADMAP.md).

## Sharing a machine

On the **Cluster** page, under *What this machine shares*, you choose:

| Setting | Meaning |
|---|---|
| Share this machine | Off by default. Until it's on, no job runs here, from anyone. |
| CPU share | The share of this machine's CPU threads that jobs may use together. Each job is also hard-capped at its own share. |
| RAM, disk | What jobs may use together. Each job's memory is hard-capped. |
| GPUs | Which GPUs jobs may use. A job gets whole GPUs, and `CUDA_VISIBLE_DEVICES`/`HIP_VISIBLE_DEVICES` point at them. |
| Jobs at once | How many jobs may run at the same time. |
| When | Always, or only after nobody has touched the machine for N minutes. |
| Job kinds | `command`, `python`, `module_op`, `module_build`, `inference`. |
| Linked servers allowed | Which linked servers may send jobs (`*` for all). |
| Work folder | Where job folders go (default `data/cluster/work`). Put it on fast storage. |

Only the desktop dashboard can change this, never a linked server or the agent. Every change is in the audit log.

## Running work

From the Cluster page, the agent (`cluster_run`, `cluster_group`, `cluster_jobs`, `cluster_cancel`), or
`POST /api/cluster/jobs`:

```json
{"kind": "python", "spec": {"code": "print('hi')"}, "req": {"cpu": 2, "ram_gb": 4, "gpus": 1, "vram_gb": 16},
 "timeout_s": 3600, "retries": 1}
```

- **Placement.** The best node that fits (the most room left, lightly loaded, close by) is asked to take the job.
  It reserves the job's share or refuses with the reason, and then the next node is asked. When nothing fits, you
  get each node's reason.
- **Each job:**
  - runs in its own folder, and anything written to `$CLUSTER_OUT_DIR` comes back as a result file;
  - gets a clean environment (ABP's own variables, which hold API keys, never reach it);
  - is stopped, with its whole process tree, on timeout or cancel.
- **Gangs** (`replicas: N`): N members on N distinct nodes, reserved all-or-nothing and then started together.
  Each gets `RANK`, `WORLD_SIZE`, `MASTER_ADDR` and `MASTER_PORT` (enough for `torch.distributed`), plus
  `CLUSTER_NODES` with every member's address.
- **Arrays** (`count: N, max_parallel: M`): N tasks, each told `CLUSTER_TASK_INDEX` and `CLUSTER_TASK_COUNT`, placed
  as room frees up. The results are gathered on the group.
- **Retries.** With `retries`, a job whose node goes down, or loses it, is placed again on another node.

## How it holds together

- **The heartbeat.** Every 15 seconds each node asks every linked node for its report. A node is *stale* after
  45 s without an answer and *down* after 3 minutes. The heartbeat never wakes a sleeping machine.
- **Waking and sleeping.** Submitting a job may wake a sleeping node (Power: Wake-on-LAN), and a node stays awake
  while it runs jobs.
- **No double-booking.** The node taking a job checks its budget and reserves in one step, so any number of
  machines can schedule at once.
- **Restarts.** Records live in `data/cluster/cluster.sqlite3`. If ABP stops, its jobs' processes stop with it,
  and they are marked *lost*. Retries then place them elsewhere.
- **How limits are enforced:**
  - On Windows, a job object gives a hard CPU-rate cap and a memory cap.
  - On Linux and macOS, jobs run under an address-space limit and a lower priority. Cgroup CPU quotas are still to
    come.
