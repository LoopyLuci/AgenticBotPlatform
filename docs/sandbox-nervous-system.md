# The Sandbox Nervous System

ABP starts a lot of processes: agent shell commands, CLI agent backends, module builds, local
inference servers, daemons, training workers, git, browsers, language servers. Before
`bot/sandbox_ns/`, each one did its own thing, and three complaints came out of that:

* a blank console window popping up on the desktop (the #1 one),
* processes outliving whatever started them, with nothing left that knew they existed,
* the whole machine's CPUs being eaten by a build ABP kicked off - on this machine, whose
  logical CPUs 20 and 21 have thrown machine-check errors.

This page is what the package guarantees, what it deliberately does not, what differs per OS,
and how new code uses it.

## The seven modules

| Module | What it is |
|---|---|
| `bot/sandbox_ns/guard.py` | `install()` wraps `subprocess.Popen` so every process ABP starts is windowless; `visible()` is the one way a person gets a real console |
| `bot/sandbox_ns/policy.py` | `Policy`: memory, CPU rate, processor set, priority, lifetime, environment and network mode - with a named preset per kind of process |
| `bot/sandbox_ns/cell.py` | one sandbox: a policy plus the OS thing that enforces it (a Win32 Job Object, or a session/limits cell on POSIX) |
| `bot/sandbox_ns/spawn.py` | `spawn()` / `async_spawn()` / `run()`: how new code starts a process |
| `bot/sandbox_ns/registry.py` | what is running: a record per process (the spawn and everything it started), an event ring buffer, `data/sandbox_ns/live.json`, a CPU/memory sampler |
| `bot/sandbox_ns/reaper.py` | what the last run left behind, stopped at start-up |
| `bot/sandbox_ns/reflexes.py` | a cell over its memory cap is killed; a hot machine demotes builds and workers; the emergency stop stops the work |

`bot/sandbox_ns/` is on `bot/hotreload.py`'s denylist: the guard owns a process-wide
`subprocess.Popen` replacement and the registry owns live process state, so a reload would
either stack a second wrapper or silently stop being windowless.

## What it guarantees

**No console window on the desktop, ever.** Every entry point calls `guard.install()` before it
can start anything: `bot/main.py`, `abp_cli`, `abp_run`, `abp_acp`, `bot/tui`, `bot/mcp_server.py`,
`scripts/local_pipeline.py` (the pre-push hook's own process - `git.exe` started without a console
leaves the whole chain console-less), and the two worker scripts
(`bot/localai/train_worker.py`, `bot/neurallab/nn_worker.py`). Because
`subprocess.run`, `subprocess.call`, `subprocess.check_output` and asyncio's Windows subprocess
transport all go through `subprocess.Popen`, that one wrapper covers every one of them -
`tests/test_sandbox_ns.py` checks the asyncio case against a real child rather than assuming it.

The test session installs the guard too, from `tests/conftest.py` - the one file imported before
any test module. Otherwise every test outside `tests/test_sandbox_ns.py` spawns unguarded and a
blank window is one ordinary `subprocess` call away. `tests/no_windows_plugin.py` (registered from
the same conftest) then *checks* the promise instead of trusting it: on Windows a daemon thread
polls `EnumWindows` for new visible `ConsoleWindowClass` / `CASCADIA_HOSTING_WINDOW_CLASS` /
`PseudoConsoleWindow` windows - the same three classes `X:/Dev/swarm/popup_watch.ps1` watches - and
fails the test that was running when one appeared, with the process chain that opened it. Windows
that were already open when the session started are ignored, and so is a window whose chain does
not reach that pytest worker (a shared desktop has other things on it).
`ABP_ALLOW_WINDOWS=1` turns the watcher off for a person debugging a popup on purpose.

The two Windows flags are not interchangeable, which is the whole reason this is a wrapper:

| What the code asked for | What it gets | Why |
|---|---|---|
| nothing (`creationflags=0`) | `CREATE_NO_WINDOW` | a hidden console of its own; its children inherit it and stay invisible |
| `DETACHED_PROCESS` | `CREATE_NO_WINDOW` | measured on this machine, a `DETACHED_PROCESS` child produced a **visible** `ConsoleWindowClass` window (reproduced through a raw Win32 `CreateProcess` too). That is the popup, not the cure |
| `CREATE_NEW_CONSOLE` | `CREATE_NO_WINDOW`, **unless** inside `guard.visible()` | a console for a person is deliberate, not something to take away |
| `CREATE_NEW_PROCESS_GROUP` | kept | Ctrl+C in ABP's console must not reach the child |

The two deliberate exceptions in the codebase are `bot/modules/harness.py`'s `open_tui` and
`bot/tui/screens/infra.py`'s `shell_into`, which both wrap their spawn in `guard.visible()`: a
console a person types into (a module's TUI, and "Shell" handing the real terminal to
`docker exec -it`) is asked for, not taken away. That is the pattern; nothing else should need it.

**Every process is contained.** A `Cell` on Windows is a real Win32 Job Object
(`bot/agent_runtime/win_job.py`, extended here with CPU rate, affinity and priority limits). Every
process assigned to it, and every process those start, is in the job for the rest of its life, so
`Cell.kill()` - or simply closing the job's handle - takes the whole tree down. That is a guarantee
rather than the `taskkill /T /F` tree walk several call sites used before, which loses a race
against a process that forks quickly or detaches deliberately.

**Every process is accounted for.** `data/sandbox_ns/live.json` (under ABP's state root, so
`ABP_HOME` moves it) lists every live process ABP started: pid, the OS's own create time for it,
its argv with secret-looking arguments masked, cwd, which component asked for it, and the cell and
policy under it. The file is keyed by run, so a worker process writing its own entries cannot erase
the server's bookkeeping.

**And a record covers the tree, not just the pid `Popen` handed back.** The process ABP starts is
often not the process that does the work. On Windows a venv's `Scripts/python.exe` is a *launcher*:
it starts the base interpreter as a child and waits for it, and that child is what runs the
training worker, the module hub or the build step (measured here: ABP runs from a venv, so every
Python child it starts is two processes, and `sys.executable` *inside* the child is the base
interpreter). A `cargo` under `npm`, Playwright's browser under its driver and `ssh` under `git`
are the same shape. So the sampler walks each record's descendants and records each one under it -
same owner, same cell, same policy (a job object, a cgroup and a session are all inherited, so
containment is already true), with `parent_pid` saying which record started it. `record_for(pid)`,
`/api/sandbox/processes` and `abp sandbox ps` (an `of` column) answer for any of them.

The walk runs a moment after each spawn - the child of a launcher does not exist yet in the
microsecond after `CreateProcess` returns, and that is the only point in its life where it is seen
this promptly - and again on every sampling pass, one pass over the machine's process table for all
records rather than one per record. So a process that appeared and finished between two passes is
not recorded: the reaper only ever knows what a pass caught, same as before.

**Left-over processes are reaped.** At start-up `reaper.reap()` reads that file and kills what a
previous run left running - matching **pid *and* create time**, so a pid Windows has since handed to
something else is never touched. Daemons are recorded `persistent` and left alone: they are supposed
to outlive ABP, and their owner stops them.

**The machine is protected.** Every preset runs `below_normal`; nothing ABP starts runs on the
processors in `sandbox_ns.avoid_cpus` (this machine: `[20, 21]`) or on the ones the Neural Lab's
stability policy detected from real machine-check errors (`bot/neurallab/systune.py`) - the two sets
are unioned, so a machine nobody configured is still protected. The presets' caps are in
[the table](#the-presets).

## What it does not do

* **It is not a security sandbox.** A job object does not confine the filesystem or the network.
  That is `bot/agent_runtime/sandbox.py`'s job (AppContainer, `unshare`, `sandbox-exec`, Docker) and
  the two compose: the sandbox confines, the nervous system contains, limits, records and cleans up.
* **The assign race is real.** A process has to be assigned to the job *after* it is spawned, so
  anything it starts in those few milliseconds is not guaranteed to be in the job. It is milliseconds
  wide (spawn, read the pid, assign) and unlike a container's namespaces it is not airtight.
* **A cell's processes are what the OS says, plus a walk.** Containment is the job object, and the
  status page reports the limits Windows actually has; the *accounting* of which processes are in
  the tree is the sampler's walk of parent pids, not a query of the job. Two consequences, both
  measured rather than assumed: the walk costs one pass over the machine's process table (a few
  milliseconds here, seconds if psutil is asked one process at a time), and the console host
  Windows creates for a windowless console (`conhost.exe`) is recorded too, because it really is a
  process in the cell and it really does cost memory.
* **A cap that was not applied is reported, not assumed.** `Cell.status()` lists the limits Windows
  actually has (`QueryInformationJobObject`), plus `notes` explaining anything that could not be
  applied - a job affinity mask names at most 64 processors; `RLIMIT_NPROC` counts every process of
  the user, not of the tree; a cgroup v2 slice needs a writable `/sys/fs/cgroup`.
* **Job CPU rate is a ceiling, not a reservation.** It is a share of *all* processors (10 000 cycles
  per 10 000) applied in ~10 ms scheduling periods, and it is not a share of the processors left
  after affinity.
* **The server process itself is untouched.** It is not in a cell and keeps the OS default priority.
* **Reaping only knows about what was recorded.** A process ABP started through plain `subprocess`
  before this existed is not in `live.json`. It is still windowless (the guard) - just not reaped.
* **Not every call site is migrated.** The short, buffered helpers still call `subprocess` on
  purpose - see [Migration status](#migration-status) for what is in a cell and what is not.

## Per OS

| | Windows | Linux | macOS |
|---|---|---|---|
| Console windows | the guard's whole job; measured with real `GetConsoleWindow()`/`IsWindowVisible()` | n/a | n/a |
| Containment | Win32 Job Object (`kill_on_close`), guaranteed tree kill | `start_new_session` + `setrlimit`/`nice`/`sched_setaffinity`, and a cgroup v2 slice when `/sys/fs/cgroup` is writable | same as Linux (no cgroup v2) |
| CPU cap | Job Object hard cap, whole machine | cgroup `cpu.max` when available; nothing else enforces it | as Linux |
| Affinity | job mask (first 64 processors) or psutil, process by process | `sched_setaffinity` | as Linux |
| Memory cap | per-process and per-job | `RLIMIT_AS`/`RLIMIT_DATA` | as Linux |
| Priority | job priority class, one call for the whole cell | `nice` | as Linux |
| Sampling | psutil | psutil | psutil |

Everything except the Windows console behaviour is portable; the Windows-only tests
(`tests/test_sandbox_ns.py`) skip elsewhere rather than pretending to have measured it.

## The presets

| Preset | For | Memory | CPU rate | Processes | Priority | Timeout |
|---|---|---|---|---|---|---|
| `tool` | a command an agent ran | operator's setting (`native_agent.sandbox.windows_job`) | - | 256 | below normal | - |
| `agent` | a CLI agent backend | - | - | 256 | below normal | - |
| `build` | compilers and test suites | 6 GB | 60% | 512 | below normal | 2 h |
| `engine` | local inference servers | its own | 75% | 64 | below normal | - |
| `daemon` | a service that outlives ABP | - | - | 256 | below normal | - |
| `worker` | training / lab workers | 12 GB | 50% | 128 | below normal | - |

The light presets carry no memory cap on purpose: a cap too low for the work is worse than no cap.
Their cell still gives a guaranteed tree kill, the default processor set and below-normal priority.

Override any field per machine in `config/backends.yaml`:

```yaml
sandbox_ns:
  avoid_cpus: [20, 21]        # logical processor numbers; also the ones systune detected
  memory_margin: 0.15         # how far past its cap a cell may be before it is killed
  cpu_percent: 85             # the machine's CPU, sustained over cpu_samples samples
  cpu_samples: 3
  presets:
    build: {memory_mb: 8192}
    tool: {max_processes: 512}
```

## Using it in new code

```python
from bot.sandbox_ns.spawn import spawn          # synchronous
from bot.sandbox_ns.cell import cell_for        # when processes belong together

proc = spawn(["git", "fetch", "--quiet"], preset="tool", cwd=workspace, owner="git_stacks",
             stdout=subprocess.DEVNULL)
```

```python
with cell_for("build", name="module build", owner="modules.harness") as cell:
    for step in m.build_steps:
        spawn(cmd, cell=cell, cwd=ws, env=env, timeout_s=7200)     # cell.kill() stops the tree
```

```python
from bot.sandbox_ns.spawn import async_spawn     # asyncio
proc = await async_spawn(command, cell=cell, cwd=cwd, env=env, shell=True,
                         stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
```

Rules of thumb:

* Pass `owner=` - the component that asked - so a status page can say *who* is running what.
* One cell per thing that belongs together (a build and the tests it runs, a bridge and its
  children). A cell per process means `kill()` cannot stop the tree.
* `preset="daemon"` only for something that must keep running after ABP exits. It is recorded and
  windowless, but the reaper and `close_cells()` will not stop it.
* Only `bot/modules/harness.py`'s `open_tui` and `bot/tui/screens/infra.py`'s `shell_into` need
  `guard.visible()` - both are a person typing into a console on purpose.
* `Policy.environment()` delegates to `sandbox.build_env` and `Policy.offline()` to the sandbox's
  network launcher. Do not reimplement either; if you need a new mode, add it there.

## Asking what is running

`bot.sandbox_ns.registry.registry.status()` returns the guard's state, every record, every cell
with its measured CPU/memory and the limits the OS actually has, and the event ring buffer
(`spawn`, `descendant`, `exit`, `limit_hit`, `kill`, `reap`, `guard_converted`; `descendant` is a
process a recorded one started). Three surfaces read it, and all three are just this call:

| Surface | What |
|---|---|
| `GET /api/sandbox/{status,cells,processes,events}` | the data itself, plus `POST /api/sandbox/cells/{id}/kill` and `POST /api/sandbox/estop` - both audited, both behind the dashboard token (`bot/dashboard/sandbox_ns_api.py`) |
| `abp sandbox {status,cells,ps,events,kill,estop}` | the same API from a terminal (`abp_cli/sandbox.py`); `--json` for a script |
| the diagnostics page, in both UIs | a Processes panel with Cells / Processes / Events tabs and a Kill button per cell (`bot/dashboard/static/`, and the desktop app's own copy in `desktop-app/ui/`) |

## Reflexes

Run by the registry's sampler (psutil, every three seconds, only while something is registered). A
cell's numbers are its *tree's*, because every process under the spawn is a record: a build's
memory is the compilers as well as the build command, which is what the cap was always meant to
bound.

* **memory** - a cell past its own cap (plus `memory_margin`) is killed, `limit_hit` is logged.
* **CPU** - when the *machine* stays over `cpu_percent` for `cpu_samples` samples in a row, `build`
  and `worker` cells are demoted to idle priority. Not killed: a slow build beats no build, and the
  demotion is one-way so the machine cannot oscillate. Never a `tool` or `agent` cell - a person is
  waiting on those.
* **estop** - when ABP's own emergency stop is engaged (`bot/agent_runtime/estop.py`, the sentinel
  every agent entry point already checks), every non-persistent cell is killed. Daemons are left
  alone: the estop stops work, not somebody's running service.

## Migration status

**Migrated from the start:** `bot/agent_runtime/sandbox.py`'s `local` and `windows_job` backends
(`local` now gets a cell, so its tree is contained by Windows rather than by `taskkill`), the three
daemon harnesses (`bot/hermes_manager`, `bot/vm_harness`, `bot/transferdaemon` - `preset="daemon"`),
and `bot/localai/engine.py`'s `llama-server` (`preset="engine"`).

**Migrated with the surface** (the routes, `abp sandbox` and the dashboard's Processes panel):

| Call site | One cell per ... | Preset / policy |
|---|---|---|
| `bot/backends/cli_backend.py`, `external_agent_backend.py` (opencode, openclaw), `hermes_cli_backend.py` | run, so a timeout or a `/stop` takes the CLI's tool subprocesses too | `agent` |
| `bot/backends/hermes_gateway_backend.py` | the `hermes serve` process, which is meant to outlive ABP | `daemon` (persistent) |
| `bot/modules/harness.py` `_stream()` (build and pipeline steps) | build / pipeline run | `build` |
| `bot/modules/harness.py` `start_hub()` | hub, released by `stop_hub()`; killed if it never answered | `daemon` (persistent) |
| `bot/localai/train.py` `setup_env()` / `convert()` | setup or convert | `worker` |
| `bot/localai/train.py` and `bot/neurallab/lab.py` `start()` | run, held in `_worker_cells` until `stop()` | `worker` |
| `bot/hosting/procs.py` | (home, name), released by `stop()`, which falls back to `cell.kill()` | `daemon` (persistent) |
| `bot/cluster/executor.py` | job, with the job's own reservation as its policy (`memory_mb`, `job_memory_mb`, `cpu_rate_percent`) | `tool` + that policy |
| `bot/agent_runtime/code_intel.py` `LspClient` | language server, killed by `stop()` / `shutdown_all()` | `tool` |
| `bot/agent_runtime/browser.py` `Session` | session: Playwright's driver is admitted at `_adopt()`, so the browser it launches is in the cell too, and `close()` kills it | `tool` |
| `bot/git_stacks.py` `_git()` | - no cell: one buffered git call at a time, so the process *is* the tree | `tool` (through `spawn.run()`) |
Each has a test that starts a real process through that call site and checks the record's
`owner`/`cell`/`policy` and that stopping the cell stops the process (`tests/test_sandbox_migration.py`,
plus the site's own test file). Each of those also checks the *tree*: the process that does the
work - the interpreter behind the venv launcher, in every Python case here - is recorded under the
spawn, and the cell kill takes the whole of it.

**Still plain `subprocess`, deliberately.** Windowless either way (the guard wraps `Popen`), but not
in a cell and not in `live.json`:

* short, buffered helpers whose output is captured and which have no tree of their own:
  `bot/modules/harness.py`'s `_run()`/`_git()` (version probes, `rev-parse`, `git log`),
  `bot/fileserver/disks.py` (`smartctl`/`lsblk`/PowerShell probes), `bot/cluster/inventory.py`
  (CPU and memory probes), and `bot/hosting/{caddy,service,vps}.py`'s validate/reload/permission calls.
* `bot/modules/harness.py`'s `_launch_visible()` - `open_gui` and `open_tui`, the one place a
  console or window is for a person, spawned inside `guard.visible()`.
* `bot/hosting/deploy.py`'s `ssh`/`wrangler`/`git` runs: buffered `subprocess.run()` with their own
  timeouts, started from a person's click rather than by anything running on a timer. A cell each is
  the obvious next step, not done in this pass.
* `bot/fileserver/server.py` starts no process at all - uvicorn runs inside this one.
