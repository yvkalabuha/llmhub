"""Thin aliases of lifecycle_manager.safety for the budget/cost modules (one safety API)."""

from __future__ import annotations

import sqlite3

from llmhub.lifecycle_manager.safety import get_key, join_or_open

__all__ = ["add_incident", "join_or_open", "state_get", "state_set"]


def add_incident(
    conn: sqlite3.Connection,
    now: float,
    kind: str,
    severity: str,
    *,
    ref: str | None = None,
    dedup_key: str | None = None,
    details_json: str = "{}",
) -> bool:
    """INSERT OR IGNORE by dedup_key; True when a new incident row was written."""
    cur = conn.execute(
        "INSERT OR IGNORE INTO incidents(ts, kind, severity, ref, dedup_key, details_json)"
        " VALUES (?,?,?,?,?,?)",
        (now, kind, severity, ref, dedup_key, details_json),
    )
    return cur.rowcount > 0


state_get = get_key


def state_set(conn: sqlite3.Connection, key: str, value: str, now: float) -> None:
    conn.execute(
        "INSERT INTO controller_state(key, value, updated_at) VALUES (?,?,?)"
        " ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
        (key, value, now),
    )
