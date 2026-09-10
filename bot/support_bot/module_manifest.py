"""Runtime state for Knowledge Modules — which modules are currently
enabled, and what version (training-data hash) each one is at.

This is the ONE file that separates "unload" from "remove": unloading a
module flips its `enabled` flag here (instant, reversible, the module's
own trained-model JSON under data/support_bot_models/modules/<id>/ is
left untouched on disk) while removing a module is a separate, explicit,
irreversible deletion of that directory entirely — see
bot/support_bot/knowledge_modules.py's module docstring for why the two
must never be conflated.

Same atomic write-to-temp-then-replace pattern as model_io.py, for the
same reason: a manifest write must never be observed half-written.

Every function below takes `path: Optional[Path] = None` and resolves it
against the CURRENT value of module-level MANIFEST_PATH inside the
function body — never `path: Path = MANIFEST_PATH` as the parameter
default. A default value is bound once, at function-definition time
(module import), so a test's `monkeypatch.setattr(module_manifest,
"MANIFEST_PATH", tmp_path / "manifest.json")` would silently have no
effect on a function whose default already captured the old value —
exactly the hazard bot/support_bot/model_io.py's own CURRENT_PATH
docstring warns about, confirmed live in this module's own test suite
before this became the rule.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from bot.support_bot import knowledge_modules
from bot.support_bot.model_io import MODELS_DIR

# Same env-var-override rationale as bot/support_bot/model_io.py's
# CURRENT_PATH: tests/conftest.py sets these BEFORE bot.support_bot is
# ever imported, so a real data/support_bot_models/ left behind by live
# desktop testing in this same checkout can never leak into (or be
# overwritten by) a test run.
MODULES_DIR = Path(os.environ.get("BOTSERVER_SUPPORT_BOT_MODULES_DIR") or (MODELS_DIR / "modules"))
MANIFEST_PATH = Path(os.environ.get("BOTSERVER_SUPPORT_BOT_MANIFEST_PATH") or (MODELS_DIR / "manifest.json"))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _default_entry() -> dict[str, Any]:
    return {"enabled": True, "version": None, "updated_at": _now()}


def _resolve_path(path: Optional[Path]) -> Path:
    return path if path is not None else MANIFEST_PATH


def _load_raw(path: Optional[Path] = None) -> dict[str, dict[str, Any]]:
    resolved = _resolve_path(path)
    if not resolved.exists():
        return {}
    try:
        data = json.loads(resolved.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_raw(data: dict[str, dict[str, Any]], path: Optional[Path] = None) -> None:
    resolved = _resolve_path(path)
    resolved.parent.mkdir(parents=True, exist_ok=True)
    tmp = resolved.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(resolved)


def load_manifest(path: Optional[Path] = None) -> dict[str, dict[str, Any]]:
    """Every registered module's runtime state, filling in a default
    (enabled, no version yet) for any module in the registry that hasn't
    been written to the manifest yet — a fresh install or a newly-added
    module in knowledge_modules.py both "just work" as enabled-by-default
    without needing an explicit manifest entry first."""
    raw = _load_raw(path)
    result: dict[str, dict[str, Any]] = {}
    for module_id in knowledge_modules.all_module_ids():
        result[module_id] = dict(raw.get(module_id) or _default_entry())
    return result


def is_enabled(module_id: str, *, path: Optional[Path] = None) -> bool:
    spec = knowledge_modules.MODULE_REGISTRY.get(module_id)
    if spec is not None and not spec.unloadable:
        return True  # core_status can never actually be disabled
    entry = load_manifest(path).get(module_id)
    return bool(entry["enabled"]) if entry else True


def set_enabled(module_id: str, enabled: bool, *, path: Optional[Path] = None) -> None:
    spec = knowledge_modules.MODULE_REGISTRY.get(module_id)
    if spec is None:
        raise ValueError(f"unknown module: {module_id!r}")
    if not spec.unloadable and not enabled:
        raise ValueError(f"module {module_id!r} is always-resident and cannot be disabled")
    raw = _load_raw(path)
    entry = dict(raw.get(module_id) or _default_entry())
    entry["enabled"] = enabled
    entry["updated_at"] = _now()
    raw[module_id] = entry
    _save_raw(raw, path)


def set_version(module_id: str, version: Optional[str], *, path: Optional[Path] = None) -> None:
    """Called after a module is (re)trained and saved — records the
    training_data_hash of whatever's now on disk for that module, so
    list_modules() can show a human whether a module's live model
    actually reflects its current training data or is stale."""
    if knowledge_modules.MODULE_REGISTRY.get(module_id) is None:
        raise ValueError(f"unknown module: {module_id!r}")
    raw = _load_raw(path)
    entry = dict(raw.get(module_id) or _default_entry())
    entry["version"] = version
    entry["updated_at"] = _now()
    raw[module_id] = entry
    _save_raw(raw, path)


def enabled_module_ids(*, path: Optional[Path] = None) -> list[str]:
    manifest = load_manifest(path)
    return [mid for mid in knowledge_modules.all_module_ids() if manifest[mid]["enabled"]]


def module_path(module_id: str) -> Path:
    """Where a module's model_io-shaped current.json lives —
    data/support_bot_models/modules/<module_id>/current.json."""
    return MODULES_DIR / module_id / "current.json"
