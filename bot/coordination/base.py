"""The Board interface: every operation of the work board protocol, synchronous.

LocalBoard (SQLite in ABP's own data folder) and SentinelBoard (AgentSentinel's /api/v1/board over HTTP) implement it
with the same names, shapes and error codes, so a caller never needs to know which one it got. Operations raise
BoardError (code TAKEN / LOCKED / DEPS / VERSION / STALE_FENCE / NOT_OWNER / NOT_FOUND / BAD_REQUEST) for a refusal and
Unavailable when the board cannot be reached. Task and lock values are plain dicts shaped as in the protocol.
`blocking` is True when a call may take long enough (network) that async callers should run it in a thread.
"""
from __future__ import annotations

from typing import Any, Optional


def vault_note(event: str, task: Any) -> None:
    """Hand a board event (a brief created, a review, a result) to the context vault's passive capture. Queued off
    this thread, a no-op while the vault is off, and never raises."""
    try:
        from bot.context_vault import capture

        capture.on_board_event(event, task)
    except Exception:  # noqa: BLE001
        pass


class Board:
    name = "board"
    blocking = False

    # ---- tasks ----
    def create_task(self, board: str, title: str, *, key: Optional[str] = None, detail: str = "", priority: int = 0,
                    needs: Optional[list[str]] = None, deps: Optional[list[str]] = None, scope: Optional[dict] = None,
                    max_attempts: int = 3, dedupe_done: bool = False, created_by: str = "") -> tuple[dict, bool]:
        """(task, deduped)."""
        raise NotImplementedError

    def get_task(self, task_id: str) -> dict:
        raise NotImplementedError

    def list_tasks(self, board: Optional[str] = None, *, state: Optional[str] = None, owner: Optional[str] = None,
                   limit: int = 200) -> list[dict]:
        raise NotImplementedError

    def next(self, board: str, agent: str, *, capabilities: Optional[list[str]] = None,
             lease_s: Optional[float] = None) -> Optional[dict]:
        """{task, fence, locks} for the best runnable task, already claimed, or None when nothing is runnable."""
        raise NotImplementedError

    def claim(self, task_id: str, agent: str, *, lease_s: Optional[float] = None,
              expect_version: Optional[int] = None) -> dict:
        raise NotImplementedError

    def heartbeat(self, task_id: str, agent: str, fence: int, *, lease_s: Optional[float] = None,
                  progress: Any = None) -> dict:
        """{lease_until}."""
        raise NotImplementedError

    def set_state(self, task_id: str, agent: str, fence: int, state: str, *, result: Any = None,
                  note: str = "") -> dict:
        raise NotImplementedError

    def cancel(self, task_id: str, *, agent: str = "", note: str = "") -> dict:
        raise NotImplementedError

    def unblock(self, task_id: str, *, by: str, note: str = "") -> dict:
        """Put a `blocked` task back to `open` after a decision. A board without a blocked state refuses."""
        from bot.coordination.model import BoardError

        raise BoardError("NOT_BLOCKED", f"the {self.name} board has no blocked state (a blocked task there is failed with a BLOCKED: note)",
                         status=400)

    # ---- locks ----
    def acquire(self, agent: str, resources: list, *, task: Optional[str] = None,
                lease_s: Optional[float] = None) -> dict:
        """{locks, fence}; BoardError LOCKED (with holders) and nothing taken when any resource is held."""
        raise NotImplementedError

    def lock_heartbeat(self, agent: str, fence: int, *, lease_s: Optional[float] = None) -> dict:
        raise NotImplementedError

    def release_locks(self, agent: str, fence: int, resources: Optional[list[str]] = None) -> dict:
        raise NotImplementedError

    def list_locks(self, *, resource_prefix: str = "", owner: str = "") -> list[dict]:
        raise NotImplementedError

    # ---- presence, visibility ----
    def presence(self, agent: str, status: str, *, task: Optional[str] = None,
                 capabilities: Optional[list[str]] = None) -> dict:
        raise NotImplementedError

    def list_presence(self) -> list[dict]:
        raise NotImplementedError

    def summary(self, board: Optional[str] = None) -> dict:
        raise NotImplementedError

    def ping(self) -> bool:
        """Is the board answering? (LocalBoard: always.)"""
        return True

    def close(self) -> None:
        pass
