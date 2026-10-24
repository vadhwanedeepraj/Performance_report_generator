"""
generation_guard.py
-------------------
Prevents accidental duplicate report generation.

Works by computing a SHA-256 hash of:
  - The DataFrame content (sorted by first column for stability)
  - The report title

Stores each generation record in `generation_history.json`.
Before a new batch run, callers can check if this hash was already used.

Performance note: history is cached in memory after the first disk read.
Subsequent calls within the same process do NOT hit the disk again.
"""

import os
import json
import hashlib
from datetime import datetime

HISTORY_FILE = "generation_history.json"

# ── In-memory cache so we only read from disk once per process ───────────────
_history_cache: list | None = None   # None = not yet loaded


def _compute_hash(df, report_title: str) -> str:
    """Compute a stable SHA-256 fingerprint of the dataset + report title."""
    try:
        # Sort rows by first column to make hash order-independent
        csv_str = df.sort_values(by=df.columns[0]).to_csv(index=False)
    except Exception:
        csv_str = df.to_csv(index=False)
    raw = f"{report_title.strip()}||{csv_str}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]  # 16-char prefix, readable


def _load_history() -> list:
    """Return history from memory cache; fall back to disk on first call."""
    global _history_cache
    if _history_cache is not None:
        return _history_cache
    # First call — read from disk
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, "r", encoding="utf-8") as f:
                _history_cache = json.load(f)
                return _history_cache
        except Exception:
            pass
    _history_cache = []
    return _history_cache


def _save_history(history: list):
    """Persist history to disk and update the in-memory cache."""
    global _history_cache
    _history_cache = history   # keep cache in sync
    try:
        with open(HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump(history, f, indent=2)
    except Exception as e:
        print(f"[GenerationGuard] Could not save history: {e}")


def invalidate_cache():
    """Force a fresh disk read on the next call. Call if external processes may have modified the file."""
    global _history_cache
    _history_cache = None


def check_duplicate(df, report_title: str):
    """
    Check if this dataset + report_title was already generated.

    Returns:
        (is_duplicate: bool, record: dict | None)
        record contains: hash, report_title, generated_at, student_count
    """
    batch_hash = _compute_hash(df, report_title)
    history = _load_history()
    for record in history:
        if record.get("hash") == batch_hash:
            return True, record
    return False, None


def record_generation(df, report_title: str, student_count: int, ok_count: int):
    """
    Record a completed generation run into the history file.
    Call this AFTER a successful batch generation.
    """
    batch_hash = _compute_hash(df, report_title)
    history = _load_history()

    # Update existing record if same hash, else append
    for record in history:
        if record.get("hash") == batch_hash:
            record["generated_at"]  = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            record["student_count"] = student_count
            record["ok_count"]      = ok_count
            _save_history(history)
            return

    history.append({
        "hash":          batch_hash,
        "report_title":  report_title,
        "generated_at":  datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "student_count": student_count,
        "ok_count":      ok_count,
    })
    _save_history(history)


def get_all_history() -> list:
    """Return all past generation records (newest first)."""
    return list(reversed(_load_history()))
