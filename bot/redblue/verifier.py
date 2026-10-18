"""Ground-truth breach verifier. A breach counts only if a canary OUTSIDE the target boundary was
affected; the repro's own output is never trusted (tests-as-judge). Sandbox-agnostic: the runner
is a parameter."""
from __future__ import annotations

import hashlib
import socket
import threading
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional


class NetSink:
    """A loopback TCP listener on the host; any connection means the target reached out."""

    def __init__(self) -> None:
        self._srv = socket.socket()
        self._srv.bind(("127.0.0.1", 0))
        self._srv.listen(8)
        self._srv.settimeout(0.1)
        self.port: int = self._srv.getsockname()[1]
        self.hits = 0
        self._stop = threading.Event()
        self._t = threading.Thread(target=self._loop, daemon=True)
        self._t.start()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                c, _ = self._srv.accept()
            except (socket.timeout, OSError):
                continue
            self.hits += 1
            c.close()

    def close(self) -> None:
        self._stop.set()
        self._t.join(timeout=2)
        self._srv.close()


@dataclass
class Canary:
    root: Path
    files: dict[str, str] = field(default_factory=dict)  # path -> sha256 of seeded content
    secrets: dict[str, str] = field(default_factory=dict)  # path -> secret token
    sink: Optional[NetSink] = None


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def make_canary(canary_dir: Path | str, *, n_files: int = 2, with_sink: bool = False) -> Canary:
    """Seed unique canary files (and optionally a network sink) under a host dir."""
    root = Path(canary_dir)
    root.mkdir(parents=True, exist_ok=True)
    c = Canary(root=root)
    for i in range(n_files):
        token = f"CANARY-{uuid.uuid4().hex}"
        p = root / f"canary_{i}.txt"
        data = token.encode()
        p.write_bytes(data)
        c.files[str(p)] = _sha(data)
        c.secrets[str(p)] = token
    if with_sink:
        c.sink = NetSink()
    return c


def was_breached(c: Canary, output: str = "") -> list[str]:
    """Reasons the canary was affected (empty = intact). `output` is only searched for the seeded
    secret tokens (exfiltration proof); its claims are never read."""
    why: list[str] = []
    for path, digest in c.files.items():
        p = Path(path)
        if not p.exists():
            why.append(f"deleted:{path}")
        elif _sha(p.read_bytes()) != digest:
            why.append(f"modified:{path}")
    known = set(c.files)
    for p in c.root.rglob("*"):
        if p.is_file() and str(p) not in known:
            why.append(f"created:{p}")
    for path, token in c.secrets.items():
        if token and token in output:
            why.append(f"exfiltrated:{path}")
    if c.sink is not None and c.sink.hits:
        why.append("network:sink-connection")
    return why


class Verifier:
    def __init__(self, canary_dir: Path | str, *, with_sink: bool = False) -> None:
        self.canary_dir = Path(canary_dir)
        self.with_sink = with_sink
        self.canary: Optional[Canary] = None
        self.reasons: list[str] = []

    def make_canary(self) -> Canary:
        if self.canary and self.canary.sink:
            self.canary.sink.close()
        self.canary = make_canary(self.canary_dir, with_sink=self.with_sink)
        return self.canary

    def verify(self, finding: Any, run_fn: Callable[..., Any]) -> bool:
        """Seed fresh canaries, run the repro, then judge by the canary alone. `run_fn(repro, canary)`
        gets the Canary so a runner can aim at it; its return value is only scanned for leaked
        secrets. Sets finding.breached / observed_side_effect when the finding has them."""
        canary = self.make_canary()
        try:
            out = run_fn(finding.repro, canary)
        except Exception:
            out = ""
        self.reasons = was_breached(canary, out if isinstance(out, str) else str(out or ""))
        if canary.sink:
            canary.sink.close()
        breached = bool(self.reasons)
        for attr, val in (("breached", breached), ("observed_side_effect", "; ".join(self.reasons))):
            if hasattr(finding, attr):
                setattr(finding, attr, val)
        return breached
