"""Join an open transaction or open a short one (same convention as lifecycle_manager)."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager

from llmhub.persistence.db import tx


@contextmanager
def join_or_open(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    if conn.in_transaction:
        yield conn
    else:
        with tx(conn):
            yield conn


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


def state_get(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM controller_state WHERE key=?", (key,)).fetchone()
    return None if row is None else str(row["value"])


def state_set(conn: sqlite3.Connection, key: str, value: str, now: float) -> None:
    conn.execute(
        "INSERT INTO controller_state(key, value, updated_at) VALUES (?,?,?)"
        " ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
        (key, value, now),
    )
