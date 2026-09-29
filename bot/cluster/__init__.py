"""ABP Cluster: paired ABP machines sharing resources and running work for each other (docs/modules/ROADMAP.md §5a).

    inventory.py   what this machine has: CPU, RAM, GPUs, disks, hypervisors, toolchains, modules, models, live load
    offer.py       what its owner shares with the cluster, and the budget that tracks reservations against it
    store.py       the job records (SQLite): survive restarts, audited
    executor.py    runs jobs here, inside the offer's hard limits, each in its own work folder
    membership.py  every node (this one and linked peers), kept current by a heartbeat
    scheduler.py   places jobs: filter, score, reserve (the chosen node accepts or refuses); groups and arrays
    tools.py       the agent's cluster tools
"""
