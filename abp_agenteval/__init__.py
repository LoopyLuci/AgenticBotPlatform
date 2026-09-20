"""Agent evaluation harness: a task is a workspace fixture, a prompt and
deterministic graders; a run is one agent turn in a throwaway workspace, graded
from what is on disk, what the agent said and what its trace shows it did.

Two modes share everything but the model:

* **scripted** — a golden trajectory replaces the model, so the run is
  deterministic and free. It proves the harness, the tool layer, the approval
  path and the graders, and it is what CI runs on every push.
* **live** — a real model, opt-in, for measuring the agent itself and comparing
  it with other agents on the same tasks.

Reports are plain JSON; `--baseline` turns a report into a regression gate.
"""

SCHEMA_VERSION = 1
