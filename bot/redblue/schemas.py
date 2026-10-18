"""Typed Finding / Incident records with JSON-safe round trips and JSON Schemas."""
from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Optional


class SchemaError(ValueError):
    pass


class VectorClass(str, Enum):
    FILESYSTEM = "filesystem"
    NETWORK = "network"
    PROCESS = "process"
    VM = "vm"
    RESOURCE = "resource"
    AGENT_RUNTIME = "agent_runtime"


FOUND_BY = ("secret", "wild", "shared")
GRANT_RESULTS = ("allowed", "denied", "na")
VERDICTS = ("benign", "suspicious", "escape")

_NUM = (int, float)
_S = (str,)
_OS = (str, type(None))


def _check(d: Any, name: str, spec: dict[str, tuple]) -> None:
    """spec: field -> (types, required). Rejects non-dicts, unknown keys, missing keys, bad types."""
    if not isinstance(d, dict):
        raise SchemaError(f"{name}: expected an object")
    extra = set(d) - set(spec)
    if extra:
        raise SchemaError(f"{name}: unknown fields {sorted(extra)}")
    for key, (types, required) in spec.items():
        if key not in d:
            if required:
                raise SchemaError(f"{name}: missing field {key!r}")
            continue
        v = d[key]
        if isinstance(v, bool) and bool not in types:
            raise SchemaError(f"{name}.{key}: bad type bool")
        if not isinstance(v, types):
            raise SchemaError(f"{name}.{key}: bad type {type(v).__name__}")


def _unit(name: str, v: float, hi: float) -> None:
    if not 0 <= v <= hi:
        raise SchemaError(f"{name} must be within 0..{hi}")


@dataclass
class Finding:
    vector_class: VectorClass
    hypothesis: str
    repro: str
    predicted_blue_signal: str
    observed_side_effect: str = ""
    breached: bool = False
    severity: float = 0.0
    novelty: float = 0.0
    confidence: float = 0.0
    suggested_patch_class: str = ""
    found_by: str = "secret"
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["vector_class"] = self.vector_class.value
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Finding":
        validate_finding(d)
        d = dict(d)
        d["vector_class"] = VectorClass(d["vector_class"])
        return cls(**d)


@dataclass
class Incident:
    timestamp: float
    detector: str
    actor: str
    action: str
    target: str
    grant_result: str = "na"
    verdict: str = "benign"
    matched_signature: Optional[str] = None
    containment_action: Optional[str] = None
    closed_by: Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Incident":
        validate_incident(d)
        return cls(**d)


_FINDING_SPEC = {
    "vector_class": (_S, True), "hypothesis": (_S, True), "repro": (_S, True),
    "predicted_blue_signal": (_S, True), "observed_side_effect": (_S, False),
    "breached": ((bool,), False), "severity": (_NUM, False), "novelty": (_NUM, False),
    "confidence": (_NUM, False), "suggested_patch_class": (_S, False), "found_by": (_S, False),
    "timestamp": (_NUM, False),
}
_INCIDENT_SPEC = {
    "timestamp": (_NUM, True), "detector": (_S, True), "actor": (_S, True), "action": (_S, True),
    "target": (_S, True), "grant_result": (_S, False), "verdict": (_S, False),
    "matched_signature": (_OS, False), "containment_action": (_OS, False), "closed_by": (_OS, False),
}


def validate_finding(d: Any) -> None:
    _check(d, "Finding", _FINDING_SPEC)
    if d["vector_class"] not in {v.value for v in VectorClass}:
        raise SchemaError("Finding.vector_class: unknown value")
    if d.get("found_by", "secret") not in FOUND_BY:
        raise SchemaError("Finding.found_by: unknown value")
    _unit("severity", d.get("severity", 0), 10)
    _unit("novelty", d.get("novelty", 0), 1)
    _unit("confidence", d.get("confidence", 0), 1)


def validate_incident(d: Any) -> None:
    _check(d, "Incident", _INCIDENT_SPEC)
    if d.get("grant_result", "na") not in GRANT_RESULTS:
        raise SchemaError("Incident.grant_result: unknown value")
    if d.get("verdict", "benign") not in VERDICTS:
        raise SchemaError("Incident.verdict: unknown value")


def _json_schema(title: str, spec: dict, enums: dict[str, tuple], bounds: dict[str, tuple]) -> dict:
    props: dict[str, Any] = {}
    for k, (types, _req) in spec.items():
        if bool in types:
            p: dict[str, Any] = {"type": "boolean"}
        elif int in types:
            p = {"type": "number"}
        elif type(None) in types:
            p = {"type": ["string", "null"]}
        else:
            p = {"type": "string"}
        if k in enums:
            p["enum"] = list(enums[k]) + ([None] if type(None) in types else [])
        if k in bounds:
            p["minimum"], p["maximum"] = bounds[k]
        props[k] = p
    return {"$schema": "https://json-schema.org/draft/2020-12/schema", "title": title, "type": "object",
            "properties": props, "required": [k for k, (_t, r) in spec.items() if r],
            "additionalProperties": False}


FINDING_SCHEMA = _json_schema(
    "Finding", _FINDING_SPEC,
    {"vector_class": tuple(v.value for v in VectorClass), "found_by": FOUND_BY},
    {"severity": (0, 10), "novelty": (0, 1), "confidence": (0, 1)})
INCIDENT_SCHEMA = _json_schema(
    "Incident", _INCIDENT_SPEC, {"grant_result": GRANT_RESULTS, "verdict": VERDICTS}, {})

__all__ = ["Finding", "Incident", "VectorClass", "SchemaError", "FINDING_SCHEMA", "INCIDENT_SCHEMA",
           "validate_finding", "validate_incident"]
