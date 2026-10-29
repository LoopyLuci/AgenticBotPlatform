"""Hardware guard: watch this machine's health and keep heavy work inside safe limits (docs/hwguard.md).

    sensors.py   CPU load, temperatures where the OS exposes them, GPU VRAM/load from OS counters, RAM, commit, disk free
    whea.py      Windows WHEA-Logger events (corrected and fatal hardware errors), parsed from the System log
    governor.py  settings, the pure decision (ok | caution | pause, worker caps per kind) and the shared verdict file
    slot.py      the one machine-wide "heavy work" slot: a render, a full test run, a model load and a build never stack
    monitor.py   the sampling thread in ABP: history, alerts through the Sentinel journal, the verdict file
    __main__.py  `python -m bot.hwguard status|workers|acquire|release|watch` for tools outside ABP (pipeline, render harness)

What this covers and what it does not: software can lower load, refuse to stack heavy jobs, pause guarded jobs and react
to the first corrected error. It cannot make a faulty CPU stop raising machine checks; a fatal one still crashes the
machine. Temperatures are only read where Windows or a sensor tool exposes them.
"""
