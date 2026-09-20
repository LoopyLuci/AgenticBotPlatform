"""How a client reaches the data: straight from the local event store, or over
the control-plane HTTP API. Both expose exactly `service.CAPABILITIES`."""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Optional

from .service import Service
from .store import EventStore, default_db_path

DEFAULT_URL = "http://127.0.0.1:8787"


class TransportError(RuntimeError):
    pass


class LocalTransport(Service):
    """Reads the event store directly — works with no server running."""

    def __init__(self, db: Optional[Path | str] = None):
        super().__init__(EventStore(db or default_db_path()))


class HttpTransport:
    def __init__(self, url: Optional[str] = None, token: Optional[str] = None, timeout: float = 10.0):
        self.url = (url or os.environ.get("ABP_URL") or DEFAULT_URL).rstrip("/")
        self.token = token or os.environ.get("DASHBOARD_TOKEN", "")
        self.timeout = timeout

    def _get(self, path: str, **params: Any) -> Any:
        query = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
        req = urllib.request.Request(f"{self.url}/api/cicd{path}" + (f"?{query}" if query else ""),
                                     headers={"X-Dashboard-Token": self.token, "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None
            detail = ""
            try:
                detail = json.loads(exc.read().decode("utf-8")).get("detail", "")
            except Exception:  # noqa: BLE001
                pass
            raise TransportError(f"{self.url} answered HTTP {exc.code}" + (f": {detail}" if detail else "")) from exc
        except (urllib.error.URLError, OSError) as exc:
            raise TransportError(f"can't reach {self.url}: {getattr(exc, 'reason', exc)}") from exc

    # The methods below mirror service.Service exactly.
    def summary(self) -> dict:
        return self._get("/summary")

    def runs(self, limit: int = 50, kind: Optional[str] = None) -> dict:
        return self._get("/runs", limit=limit, kind=kind)

    def run(self, run_id: str) -> Optional[dict]:
        return self._get(f"/runs/{urllib.parse.quote(run_id)}")

    def explain(self, run_id: str) -> Optional[dict]:
        return self._get(f"/runs/{urllib.parse.quote(run_id)}/explain")

    def step_stats(self, name: Optional[str] = None, run_kind: Optional[str] = None, last_n: int = 50) -> dict:
        return self._get("/steps/stats", name=name, run_kind=run_kind, last_n=last_n)

    def decisions(self, limit: int = 100, run_id: Optional[str] = None) -> dict:
        return self._get("/decisions", limit=limit, run_id=run_id)

    def workers(self) -> dict:
        return self._get("/workers")

    def events(self, since: int = 0, limit: int = 200, kind: Optional[str] = None, run_id: Optional[str] = None) -> dict:
        return self._get("/events", since=since, limit=limit, kind=kind, run_id=run_id)

    def chain(self) -> dict:
        return self._get("/chain")


def choose(source: str = "auto", db: Optional[str] = None, url: Optional[str] = None,
           token: Optional[str] = None):
    """auto: the local store if it exists and no --url was given, else HTTP."""
    if source == "http" or (source == "auto" and (url or not Path(db or default_db_path()).exists())):
        return HttpTransport(url, token)
    return LocalTransport(db)
