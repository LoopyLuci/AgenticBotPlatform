"""Support Bot training phrases, classifications and pending examples.

Part of bot.db (re-exported there); see bot/storage/__init__.py."""
from __future__ import annotations

import sqlite3
from typing import Any, Optional

from bot import db as _db


# User-added phrases layered on top of training_data.py's hand-authored
# EXAMPLES — see bot/support_bot/model.py's load_examples()/reload().
def list_support_bot_phrases() -> list[sqlite3.Row]:
    conn = _db.get_conn()
    return conn.execute("SELECT * FROM support_bot_phrases ORDER BY intent, id").fetchall()


def add_support_bot_phrase(phrase: str, intent: str, *, module_id: Optional[str] = None) -> int:
    phrase = (phrase or "").strip()
    intent = (intent or "").strip()
    if not phrase or not intent:
        raise ValueError("both phrase and intent are required")
    conn = _db.get_conn()
    with _db._lock:
        cur = conn.execute(
            "INSERT INTO support_bot_phrases (phrase, intent, created_at, module_id) VALUES (?, ?, ?, ?)",
            (phrase, intent, _db._now(), module_id),
        )
        conn.commit()
        return cur.lastrowid


def delete_support_bot_phrase(phrase_id: int) -> None:
    conn = _db.get_conn()
    with _db._lock:
        conn.execute("DELETE FROM support_bot_phrases WHERE id=?", (phrase_id,))
        conn.commit()


def log_support_bot_classification(
    text: str,
    tfidf_intent: str,
    tfidf_confidence: float,
    nn_intent: str,
    nn_confidence: float,
    final_intent: str,
    final_confidence: float,
    source: str,
    agreed: bool,
) -> None:
    conn = _db.get_conn()
    with _db._lock:
        conn.execute(
            "INSERT INTO support_bot_classifications "
            "(ts, text, tfidf_intent, tfidf_confidence, nn_intent, nn_confidence, final_intent, final_confidence, source, agreed) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (_db._now(), text, tfidf_intent, tfidf_confidence, nn_intent, nn_confidence, final_intent, final_confidence, source, 1 if agreed else 0),
        )
        conn.commit()


def get_support_bot_classification_stats(limit: int = 500) -> dict[str, Any]:
    """Self-monitoring summary for the Training tab — computed from the
    last `limit` real classifications, not a static guess. Empty/zeroed
    fields if nothing's been classified yet."""
    conn = _db.get_conn()
    rows = conn.execute(
        "SELECT * FROM support_bot_classifications ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
    total = len(rows)
    if total == 0:
        return {
            "total": 0, "agreement_rate": 0.0, "unknown_rate": 0.0,
            "avg_tfidf_confidence": 0.0, "avg_nn_confidence": 0.0, "avg_final_confidence": 0.0,
            "source_counts": {}, "recent": [],
        }
    agreed = sum(1 for r in rows if r["agreed"])
    unknown = sum(1 for r in rows if r["final_intent"] == "unknown")
    source_counts: dict[str, int] = {}
    for r in rows:
        source_counts[r["source"]] = source_counts.get(r["source"], 0) + 1
    return {
        "total": total,
        "agreement_rate": round(agreed / total, 3),
        "unknown_rate": round(unknown / total, 3),
        "avg_tfidf_confidence": round(sum(r["tfidf_confidence"] for r in rows) / total, 3),
        "avg_nn_confidence": round(sum(r["nn_confidence"] for r in rows) / total, 3),
        "avg_final_confidence": round(sum(r["final_confidence"] for r in rows) / total, 3),
        "source_counts": source_counts,
        "recent": [dict(r) for r in rows[:15]],
    }


def get_recent_misses(limit: int = 200, *, unreviewed_only: bool = False) -> list[sqlite3.Row]:
    """Real classifications the hybrid model got wrong or couldn't
    decide on — the active-learning signal both the synthetic-data swarm
    (bot/support_bot/synthetic_gen.py, unreviewed_only=False — it wants
    every recent miss regardless of review state) and the dashboard's
    "Recent misses" review panel (unreviewed_only=True, so an already-
    labeled row doesn't keep cluttering the list) consume, from one
    shared query."""
    conn = _db.get_conn()
    clause = "(agreed=0 OR final_intent='unknown')"
    if unreviewed_only:
        clause += " AND reviewed=0"
    return conn.execute(
        f"SELECT * FROM support_bot_classifications WHERE {clause} ORDER BY id DESC LIMIT ?",
        (limit,),
    ).fetchall()


def mark_support_bot_classification_reviewed(classification_id: int) -> None:
    conn = _db.get_conn()
    with _db._lock:
        conn.execute("UPDATE support_bot_classifications SET reviewed=1 WHERE id=?", (classification_id,))
        conn.commit()


def add_support_bot_pending_example(
    phrase: str, intent: str, *, source_provider: str, source_model: str,
    source_kind: Optional[str] = None, status: str = "pending", approved_by: Optional[str] = None,
    resulting_phrase_id: Optional[int] = None,
) -> int:
    """`source_kind` distinguishes a synthetic-gen batch phrase (None,
    the default — every existing call site) from a real production miss
    Tier 2 just labeled live (`"llm_fallback_live"`, see
    bot/support_bot/llm_fallback.py) — purely informational, the
    approve/reject/retrain flow is identical either way. `status`/
    `approved_by` let a caller insert an already-approved row directly
    (synthetic_gen.py's auto-approve rule, Phase 7) without a separate
    resolve_support_bot_pending_example() call — the row still lands in
    this same table, just pre-resolved, so it's never invisible to a
    human reviewing the pending list filtered to status=approved."""
    conn = _db.get_conn()
    now = _db._now()
    with _db._lock:
        cur = conn.execute(
            "INSERT INTO support_bot_pending_examples "
            "(phrase, intent, source_provider, source_model, created_at, source_kind, status, resolved_at, approved_by, resulting_phrase_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (phrase, intent, source_provider, source_model, now, source_kind, status,
             now if status != "pending" else None, approved_by, resulting_phrase_id),
        )
        conn.commit()
        return cur.lastrowid


def list_support_bot_pending_examples(status: Optional[str] = None) -> list[sqlite3.Row]:
    conn = _db.get_conn()
    if status is not None:
        return conn.execute(
            "SELECT * FROM support_bot_pending_examples WHERE status=? ORDER BY id DESC", (status,)
        ).fetchall()
    return conn.execute("SELECT * FROM support_bot_pending_examples ORDER BY id DESC").fetchall()


def get_support_bot_pending_example(pending_id: int) -> Optional[sqlite3.Row]:
    conn = _db.get_conn()
    return conn.execute("SELECT * FROM support_bot_pending_examples WHERE id=?", (pending_id,)).fetchone()


def resolve_support_bot_pending_example(
    pending_id: int, status: str, *, resulting_phrase_id: Optional[int] = None,
) -> None:
    conn = _db.get_conn()
    with _db._lock:
        conn.execute(
            "UPDATE support_bot_pending_examples SET status=?, resolved_at=?, resulting_phrase_id=? WHERE id=?",
            (status, _db._now(), resulting_phrase_id, pending_id),
        )
        conn.commit()


def revert_support_bot_pending_example(pending_id: int) -> Optional[int]:
    """Undoes a previously-approved pending example: deletes the exact
    live phrase it created (via resulting_phrase_id, never a text match)
    and marks the pending row 'reverted' — distinct from 'rejected'
    (a human/auto approval that turned out wrong) so the pending list can
    show an accurate history rather than making a reverted approval look
    like it was simply never approved. Returns the deleted phrase_id, or
    None if this pending row was never actually approved (nothing to
    revert) — callers should treat None as a no-op, not necessarily an
    error, since a double-click on "revert" must be safe."""
    conn = _db.get_conn()
    row = _db.get_support_bot_pending_example(pending_id)
    if row is None or row["resulting_phrase_id"] is None:
        return None
    phrase_id = row["resulting_phrase_id"]
    with _db._lock:
        conn.execute("DELETE FROM support_bot_phrases WHERE id=?", (phrase_id,))
        conn.execute(
            "UPDATE support_bot_pending_examples SET status=?, resolved_at=? WHERE id=?",
            ("reverted", _db._now(), pending_id),
        )
        conn.commit()
    return phrase_id
