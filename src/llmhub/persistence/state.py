"""controller_state key/value helpers: halt flag, heartbeat, safe mode."""

from __future__ import annotations

import sqlite3

from llmhub.clock import Clock
from llmhub.persistence.db import tx


class ReconcileRequired(Exception):
    """HALTED after DB corruption cannot be lifted before the billing reconciliation."""


def get(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM controller_state WHERE key=?", (key,)).fetchone()
    return None if row is None else str(row["value"])


def _put(conn: sqlite3.Connection, key: str, value: str, now: float) -> None:
    conn.execute(
        "INSERT INTO controller_state(key, value, updated_at) VALUES (?,?,?)"
        " ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
        (key, value, now),
    )


def set(conn: sqlite3.Connection, key: str, value: str, now: float = 0.0) -> None:
    """Own short transaction; `now` is stored as updated_at."""
    with tx(conn):
        _put(conn, key, value, now)


def is_halted(conn: sqlite3.Connection) -> bool:
    return get(conn, "halted") == "1"


def halt(conn: sqlite3.Connection, reason: str, clock: Clock) -> None:
    now = clock.now()
    with tx(conn):
        _put(conn, "halted", "1", now)
        _put(conn, "halt_reason", reason, now)


def mark_reconciled(conn: sqlite3.Connection, clock: Clock) -> None:
    """Called by the billing reconciliation after provider ledger rows are written."""
    set(conn, "db_reconciled", "1", clock.now())


def unhalt(conn: sqlite3.Connection, clock: Clock) -> None:
    """Lift HALTED. After `db_corrupt` this requires a finished billing reconciliation."""
    reason = get(conn, "halt_reason")
    if reason == "db_corrupt" and get(conn, "db_reconciled") != "1":
        raise ReconcileRequired("billing reconciliation required before resume")
    now = clock.now()
    with tx(conn):
        _put(conn, "halted", "0", now)
        _put(conn, "halt_reason", "", now)
        if reason == "db_corrupt":
            _put(conn, "safe_mode", "0", now)


def heartbeat(conn: sqlite3.Connection, clock: Clock) -> None:
    now = clock.now()
    set(conn, "heartbeat", repr(now), now)
