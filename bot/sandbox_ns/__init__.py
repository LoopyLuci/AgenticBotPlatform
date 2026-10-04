"""ABP's Sandbox Nervous System: every process ABP starts is windowless, contained, accounted
for, limited and cleaned up.

ABP starts a lot of processes - agent shell commands, CLI agent backends, module builds, local
inference servers, daemons, training workers, git - and before this each one did its own thing.
The three complaints that came out of that are what this package answers:

* a blank console window popping up on the desktop (the #1 one), because something started a
  child with no console flag, or with `DETACHED_PROCESS`, which gives a process no console at
  all and makes every console program it later starts pop a window of its own;
* processes outliving the thing that started them, with nothing left that knows they exist;
* the whole machine's CPUs being eaten by a build ABP kicked off, on a machine whose cores 20
  and 21 have thrown machine-check errors.

    guard      install() makes every subprocess windowless; visible() is the one way a person
               gets a real console (a module's TUI window)
    policy     what a sandbox may do - memory, CPU share, processor set, priority, lifetime -
               with named presets for each kind of process ABP starts
    cell       one sandbox: a policy plus the OS thing that enforces it (a Win32 Job Object, or
               a session/limits cell on POSIX)
    spawn      spawn() / async_spawn(): how new code starts a process
    registry   what is running: records, an event ring buffer, `data/sandbox_ns/live.json`
    reaper     what the last run left behind, stopped at start-up
    reflexes   memory over the cap kills a cell; a hot machine demotes builds; the emergency
               stop stops the work

New code starts processes with `bot.sandbox_ns.spawn.spawn()` (or `async_spawn()`), and every
entry point calls `bot.sandbox_ns.guard.install()` before anything else. What it does and does
not guarantee, per OS, is in docs/sandbox-nervous-system.md.
"""
