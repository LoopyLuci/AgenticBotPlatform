"""ABP CI/CD telemetry core: an append-only, tamper-evident event store, a
recorder for instrumenting scripts, read queries, and a CLI.

Deliberately standard-library only. The release and pipeline scripts run
standalone (no FastAPI, no `bot` package) and must be able to record events, and
the server, CLI, TUI and GUI must all read the same data the same way. See
docs/cicd/README.md for the design.
"""
from __future__ import annotations

SCHEMA_VERSION = 1

__all__ = ["SCHEMA_VERSION"]
