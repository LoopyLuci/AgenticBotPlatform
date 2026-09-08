"""Versioned, portable JSON persistence for the Support Bot's two
classifiers — see the "Support Bot NLU upgrade" plan. Both classifiers
are pure functions of (their exported state, input text), so this file
format is the single artifact a future Kotlin engine (Android) loads to
run the exact same math natively, with no shared runtime between the two
platforms — only a shared file format.

`current.json` is an atomic pointer: accepting a newly-trained model is
a write-to-temp-then-replace, so a bad model is never partially visible,
and the previous version stays on disk (never deleted here) for the same
"keep the last known-good state" reversibility bot/snapshots.py already
established elsewhere in this codebase.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any, Optional

from bot.envfile import PROJECT_ROOT

FORMAT_VERSION = 1
MODELS_DIR = PROJECT_ROOT / "data" / "support_bot_models"
# hybrid.py loads this path automatically at MODULE IMPORT TIME (not
# per-call), so unlike every other real-file dependency in this
# codebase, per-test monkeypatching of model_io.CURRENT_PATH can't help
# a test that runs after the module has already been imported once in
# the same process — the persisted-state load already happened. The env
# var override lets tests/conftest.py redirect this to a throwaway path
# BEFORE bot.support_bot.hybrid is ever imported anywhere in the test
# session, so a real production data/support_bot_models/current.json
# (e.g. from live desktop testing sharing this same checkout) can never
# silently override what a test expects to be trained fresh from the
# current bot/support_bot/training_data.py.
CURRENT_PATH = Path(os.environ.get("BOTSERVER_SUPPORT_BOT_MODEL_PATH") or (MODELS_DIR / "current.json"))


def compute_training_hash(examples: list[tuple[str, str]]) -> str:
    """sha256 over the sorted (phrase, intent) pairs — traces a deployed
    model back to exactly what trained it, independent of example order
    (two runs over the same set, added in a different order, hash the
    same)."""
    payload = json.dumps(sorted(examples), sort_keys=True).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def save_model(
    tfidf_state: dict[str, Any], nn_state: dict[str, Any], intents: list[str], *,
    training_hash: str, eval_result: Optional[dict[str, Any]] = None, path: Path = CURRENT_PATH,
) -> None:
    data = {
        "format_version": FORMAT_VERSION,
        "training_data_hash": training_hash,
        "intents": sorted(intents),
        "tfidf": tfidf_state,
        "nn": nn_state,
        "eval": eval_result or {},
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data), encoding="utf-8")
    tmp.replace(path)  # atomic on the same filesystem


def load_model(path: Path = CURRENT_PATH) -> Optional[dict[str, Any]]:
    """Returns the parsed model file, or None if it doesn't exist or is
    corrupt — a missing/broken file is never an error here, callers fall
    back to training fresh from bot/support_bot/training_data.py, same as
    every process start before this persistence layer existed."""
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(data, dict) or data.get("format_version") != FORMAT_VERSION:
        return None
    return data
