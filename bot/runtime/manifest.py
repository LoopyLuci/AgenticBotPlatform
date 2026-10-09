"""The manifest: the whole desired state of ABP, content-addressed, plus the on-disk branch store.

A branch is a named manifest. `live` is what runs and `previous` is the manifest before the last commit (for
rollback). Branch files are written to a temp file and moved into place with `os.replace`, so a crash never leaves
half a branch.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from bot.runtime.unit import make_ref

LIVE, PREVIOUS = "live", "previous"
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


@dataclass(frozen=True)
class InstanceSpec:
    unit: str
    version: str = "1"
    settings: dict = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"unit": self.unit, "version": self.version, "settings": self.settings}


@dataclass(frozen=True)
class Manifest:
    config: dict = field(default_factory=dict)
    instances: dict = field(default_factory=dict)       # "unit@instance" -> InstanceSpec

    @property
    def id(self) -> str:
        blob = json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))
        return "m-" + hashlib.sha256(blob.encode()).hexdigest()[:12]

    def to_dict(self) -> dict[str, Any]:
        return {"config": self.config, "instances": {r: s.to_dict() for r, s in sorted(self.instances.items())}}

    @staticmethod
    def from_dict(data: dict) -> "Manifest":
        instances = {r: InstanceSpec(d["unit"], str(d.get("version", "1")), dict(d.get("settings") or {}))
                     for r, d in (data.get("instances") or {}).items()}
        return Manifest(dict(data.get("config") or {}), instances)

    def with_instance(self, unit: str, instance: str, spec: InstanceSpec) -> "Manifest":
        return Manifest(self.config, {**self.instances, make_ref(unit, instance): spec})

    def without(self, ref: str) -> "Manifest":
        return Manifest(self.config, {r: s for r, s in self.instances.items() if r != ref})

    def with_config(self, config: dict) -> "Manifest":
        return Manifest(dict(config), self.instances)


def valid_branch_name(name: str) -> bool:
    return bool(_NAME.match(name or ""))


class BranchStore:
    def __init__(self, directory: Path | str) -> None:
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)

    def _path(self, name: str) -> Path:
        if not valid_branch_name(name):
            raise ValueError(f"invalid branch name {name!r}")
        return self.dir / f"{name}.json"

    def names(self) -> list[str]:
        return sorted(p.stem for p in self.dir.glob("*.json") if valid_branch_name(p.stem))

    def get(self, name: str) -> Optional[Manifest]:
        path = self._path(name)
        try:
            return Manifest.from_dict(json.loads(path.read_text(encoding="utf-8")))
        except FileNotFoundError:
            return None

    def save(self, name: str, manifest: Manifest) -> None:
        path = self._path(name)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(manifest.to_dict(), indent=2, sort_keys=True), encoding="utf-8")
        os.replace(tmp, path)

    def delete(self, name: str) -> None:
        self._path(name).unlink(missing_ok=True)
